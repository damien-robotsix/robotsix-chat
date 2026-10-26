"""Direct-repo HTTP client — GitHub App-authenticated branch push + PR open.

Talks to the GitHub API as a GitHub App installation (JWT → installation
token) for core repo operations (push branches, open/merge PRs, manage
security settings) and installation management (list repos, create repos).

Board/ticket API operations have moved to
:mod:`robotsix_chat.repo.direct.board_client`.  GitHub Actions workflow
operations have moved to
:mod:`robotsix_chat.repo.direct.actions_client`.  The unified-diff
applicator has moved to :mod:`robotsix_chat.common.unified_diff`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from robotsix_chat.common.http import safe_http_request
from robotsix_chat.repo.direct.client_pr_operations import PROperationsMixin

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

logger = logging.getLogger(__name__)


def _b64decode(data: str) -> bytes:
    """Decode a base64 string, adding padding if necessary."""
    return base64.b64decode(data + "=" * (-len(data) % 4))


def _b64encode(data: bytes) -> str:
    """Encode bytes as a base64 string without padding (GitHub API convention)."""
    return base64.b64encode(data).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# GitHub App authentication helpers
# ---------------------------------------------------------------------------


# All token minting, installation resolution, and token caching are owned by
# robotsix-github-auth's public ``mint_installation_token`` — the same path
# the refdocs/version-check/repo-study clients use via
# ``common.github_app_token``.  This client keeps no local token/installation
# caches; routing through the public API is exactly what the consolidation
# removed the private ``robotsix_github_auth._auth`` reach for.


def _owner_repo_from_path(path: str) -> tuple[str, str] | None:
    """Extract ``(owner, repo)`` from a ``/repos/{owner}/{repo}/...`` API path.

    Returns ``None`` for non-repo-scoped paths (``/search/...``,
    ``/app/...``, ``/installation/...``, ``/orgs/...``) so the caller can
    supply the repository context explicitly (or fall back).
    """
    parts = path.lstrip("/").split("/")
    if len(parts) >= 3 and parts[0] == "repos" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def _require_app_credentials(settings: DirectRepoSettings) -> tuple[str, str]:
    """Return ``(app_id, private_key)`` or raise when either is missing."""
    app_id = settings.github_app_id
    private_key = settings.github_app_private_key.get_secret_value()
    if not (app_id and private_key):
        raise RuntimeError(
            "Failed to mint GitHub App installation token. "
            "Check that github_app_id and github_app_private_key are set."
        )
    return app_id, private_key


async def _get_installation_token(
    settings: DirectRepoSettings,
    *,
    owner: str | None = None,
    repo: str | None = None,
) -> str:
    """Mint a short-lived GitHub App installation access token.

    Delegates to robotsix-github-auth's public ``mint_installation_token`` —
    the same verified path the refdocs/version-check/repo-study clients use
    via ``common.github_app_token``.  The library owns all installation
    resolution and token caching, so this wrapper only selects the mode:

    - **set** (``github_app_installation_id``): the pinned id is minted
      as-is.  If it returns HTTP 404 (the installation no longer exists,
      e.g. after a re-install) and a repository is available, the token is
      re-minted via per-repository resolution instead.
    - **empty** (recommended): the installation is resolved per repository
      from ``owner``/``repo`` (``GET /repos/{owner}/{repo}/installation``),
      exactly as robotsix-github-auth does when no id is pinned — so a
      GitHub App re-install (which mints a *new* installation id) needs no
      config edit.
    """
    app_id, private_key = _require_app_credentials(settings)
    pinned = settings.github_app_installation_id

    from robotsix_github_auth import mint_installation_token

    def _mint(**kwargs: Any) -> str:
        return mint_installation_token(
            app_id=app_id,
            private_key=private_key,
            **kwargs,
        ).token

    if not (pinned or (owner and repo)):
        raise RuntimeError(
            "Cannot resolve a GitHub App installation: "
            "github_app_installation_id is empty and this operation was "
            "invoked without a repository. Perform a repository-scoped "
            "operation first, or set github_app_installation_id."
        )

    # -- override mode: a live pinned id -----------------------------------
    if pinned:
        try:
            return await asyncio.to_thread(_mint, installation_id=pinned)
        except Exception as exc:  # 404 handled below; other errors re-raised
            if "404" not in str(exc) or not (owner and repo):
                raise
            logger.warning(
                "direct_repo: configured github_app_installation_id %s "
                "returned HTTP 404 when minting a token (the installation no "
                "longer exists); falling back to per-repository resolution "
                "for %s/%s.",
                pinned,
                owner,
                repo,
            )

    # -- per-repo resolution: empty id, or a dead pinned id ----------------
    return await asyncio.to_thread(_mint, owner=owner, repo=repo)


# ---------------------------------------------------------------------------
# Shared cycle-counting helper (used by both roster and direct API paths)
# ---------------------------------------------------------------------------


def _count_cycles_from_data(data: dict[str, Any]) -> int:
    """Count implement cycles from parsed ticket JSON (pure, no I/O).

    Inspects ``events``, ``history``, or a direct ``cycle_count`` field
    — same logic used by both the roster-based and direct board-API paths.
    Returns 0 when the data carries no discernible cycle information.
    """
    # 1. Try the events array
    events: list[dict[str, Any]] = data.get("events", [])
    if events:
        count = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            event_type = str(ev.get("type", ev.get("action", ""))).lower()
            if "implement" in event_type:
                count += 1
            elif "resume" in event_type or "unblock" in event_type:
                # A resume/unblock event indicates the ticket was
                # previously BLOCKED (mill-exhausted) and later
                # resumed — treat this as evidence of prior exhaustion
                # (the original cycle count was ≥3 before the resume
                # reset it).  Count each resume event as 3 so the
                # exhaustion gate is satisfied even when the board API
                # clears the implement events on resume.
                # Note: a resume can fire for non-exhaustion reasons
                # (e.g. manual operator unblock).  The 'count as 3'
                # heuristic over-approximates the gate to avoid
                # blocking legitimate direct-fix access.
                count += 3
        return count

    # 2. Fall back to state-transition history
    history: list[dict[str, Any]] = data.get("history", [])
    if history:
        count = 0
        for entry in history:
            if not isinstance(entry, dict):
                continue
            st = str(entry.get("state", entry.get("to", ""))).lower()
            act = str(entry.get("action", entry.get("type", ""))).lower()
            if "implement_complete" in st or "implement" in act:
                count += 1
        return count

    # 3. No events/history — try a direct cycle_count field
    cycle_count = data.get("cycle_count")
    if isinstance(cycle_count, int):
        return cycle_count

    # 4. Can't determine — return 0 (not an error; the board may not
    #    expose cycle counts)
    return 0


# ---------------------------------------------------------------------------
# DirectRepoClient
# ---------------------------------------------------------------------------


class DirectRepoClient(PROperationsMixin):
    """GitHub App-authenticated client for core repo operations.

    Handles push-branch, open-PR, merge-PR, auto-merge, file-content
    retrieval, repo creation, installation-scope listing, and security
    settings.  Board/ticket API, GitHub Actions workflow, and unified-diff
    concerns have been extracted to dedicated modules.
    """

    def __init__(
        self,
        settings: DirectRepoSettings,
        *,
        file_hub_work_dir: str | None = None,
    ) -> None:
        """Store settings; tokens are fetched lazily.

        *file_hub_work_dir* is the file-hub download directory (from
        ``file_hub_tools.working_dir``).  It is the ONLY directory from
        which ``local_path`` file entries may be read when creating blobs —
        see :meth:`_git_create_tree_items`.  When ``None``, ``local_path``
        entries are rejected.
        """
        self._s = settings
        self._base_url = settings.github_api_base_url.rstrip("/")
        self._file_hub_work_dir = file_hub_work_dir

    # -- helpers -----------------------------------------------------------

    async def _token(self, *, owner: str | None = None, repo: str | None = None) -> str:
        """Return a valid installation access token (cached).

        *owner*/*repo* let the token be minted via per-repository
        installation resolution when no fixed installation id is pinned.
        """
        return await _get_installation_token(self._s, owner=owner, repo=repo)

    def _invalidate_token(
        self, *, owner: str | None = None, repo: str | None = None
    ) -> None:
        """Clear the cached installation token so the next call re-fetches it.

        Delegates to robotsix-github-auth's public token-cache invalidation.
        In per-repository mode (or after a pinned id was abandoned on a 404)
        the effective installation id is only known inside the library, so
        the whole library token cache is cleared — harmless for this single
        App/installation client, and the next mint simply re-resolves from
        the library's short-TTL resolution cache.
        """
        from robotsix_github_auth import clear_token_cache

        clear_token_cache()

    async def _gh_headers(
        self, *, owner: str | None = None, repo: str | None = None
    ) -> dict[str, str]:
        """Return headers for a GitHub API call (with installation token)."""
        token = await self._token(owner=owner, repo=repo)
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _http_with_retry(
        self,
        method: str,
        url: str,
        *,
        owner: str | None = None,
        repo: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Make an HTTP request with retry on transient errors, 401, 429, and rate-limit 403.

        - **Transient errors** (502, 503, 504, timeouts, connection errors):
          exponential backoff retry (1s, 2s, 4s, 8s) up to 4 retries.
        - **401** (expired installation token): invalidate cache, refresh,
          retry exactly once.
        - **429** (rate-limited) or **403** with a rate-limit message:
          sleep 60 s, then retry once.
        - All other responses are returned as-is.

        Returns the ``safe_http_request`` ``HttpResult``.
        """
        # -- Transient error retry with exponential backoff ---------------------
        max_retries = 4
        backoff_delays = [1, 2, 4, 8]  # seconds

        for attempt in range(max_retries + 1):  # +1 for initial attempt
            result = await safe_http_request(method, url, **kwargs)

            # Check for transient errors that warrant retry
            is_transient_error = (
                result.status_code in (502, 503, 504)
                or (result.status_code is None and result.error is not None)  # Connection error
            )

            if not is_transient_error or attempt == max_retries:
                # Not a transient error, or we've exhausted retries — proceed to
                # the 401/429/rate-limit logic below.
                break

            # Transient error; back off and retry
            delay = backoff_delays[attempt]
            logger.warning(
                "GitHub API returned transient error (%s on %s %s) — "
                "waiting %.0f s before retry (attempt %d/%d).",
                result.status_code or result.error,
                method,
                url,
                delay,
                attempt + 1,
                max_retries,
            )
            await asyncio.sleep(delay)

        # -- 401: installation token expired ---------------------------------
        if result.status_code == 401:
            logger.info(
                "GitHub API returned 401 — refreshing installation token and retrying"
            )
            self._invalidate_token(owner=owner, repo=repo)
            if "headers" in kwargs:
                # Preserve any caller-supplied Accept header (e.g. the
                # diff media type set by get_pr_diff) when refreshing
                # the installation token, so the retry carries the same
                # media type as the original request.
                caller_accept = kwargs["headers"].get("Accept")
                kwargs["headers"] = await self._gh_headers(owner=owner, repo=repo)
                if caller_accept:
                    kwargs["headers"]["Accept"] = caller_accept
            return await safe_http_request(method, url, **kwargs)

        # -- 429 / rate-limit 403: back off and retry once -------------------
        status = result.status_code
        if status is not None and status in (429, 403):
            # Only retry when the response body indicates a rate-limit
            # condition (not a genuine authZ 403).
            # safe_http_request returns text=None for HTTP errors; the
            # body is embedded in result.error.  Combine both so we
            # catch rate-limit wording regardless of where it lands.
            body_text = (result.text or "") + " " + (result.error or "")
            is_rate_limit = status == 429 or (
                status == 403
                and (
                    "rate limit" in body_text.lower()
                    or "secondary rate limit" in body_text.lower()
                )
            )
            if is_rate_limit:
                # safe_http_request doesn't expose response headers, so
                # we can't read Retry-After.  60 s is a reasonable default
                # for GitHub secondary rate limits.
                retry_after: float = 60.0
                logger.warning(
                    "GitHub API returned %d (rate-limited) on %s %s — "
                    "waiting %.0f s before single retry.",
                    status,
                    method,
                    url,
                    retry_after,
                )
                await asyncio.sleep(retry_after)
                result = await safe_http_request(method, url, **kwargs)
                # Re-check rate-limit status after the retry so the log
                # message is accurate.
                retry_body = (result.text or "") + " " + (result.error or "")
                still_rate_limited = result.status_code == 429 or (
                    result.status_code == 403
                    and (
                        "rate limit" in retry_body.lower()
                        or "secondary rate limit" in retry_body.lower()
                    )
                )
                if still_rate_limited:
                    logger.warning(
                        "GitHub API still rate-limited after backoff "
                        "(%d on %s %s) — giving up.",
                        result.status_code,
                        method,
                        url,
                    )

        return result
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
        """Make an HTTP request with retry on 401 / 429 / rate-limit 403.

        - **401** (expired installation token): invalidate cache, refresh,
          retry exactly once.
        - **429** (rate-limited) or **403** with a rate-limit message:
          sleep 60 s, then retry once.
        - All other responses are returned as-is.

        Returns the ``safe_http_request`` ``HttpResult``.
        """
        result = await safe_http_request(method, url, **kwargs)

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

    async def _get_json(
        self,
        path: str,
        *,
        owner: str | None = None,
        repo: str | None = None,
    ) -> Any:
        """GET *path* on the GitHub API and return the parsed JSON body.

        Raises RuntimeError on any failure (never returns error strings —
        callers catch and format).  *owner*/*repo* default to the values
        parsed from a ``/repos/{owner}/{repo}/...`` path so the installation
        token can be resolved per repository when no id is pinned.
        """
        if owner is None and repo is None:
            derived = _owner_repo_from_path(path)
            if derived is not None:
                owner, repo = derived
        url = f"{self._base_url}{path}"
        result = await self._http_with_retry(
            "GET",
            url,
            owner=owner,
            repo=repo,
            headers=await self._gh_headers(owner=owner, repo=repo),
            timeout=self._s.timeout,
            label="GitHub API",
        )
        if result.error:
            raise RuntimeError(f"GitHub API GET {path}: {result.error}")
        try:
            return json.loads(result.text or "")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"GitHub API GET {path}: invalid JSON: {exc}") from exc

    async def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        *,
        owner: str | None = None,
        repo: str | None = None,
    ) -> Any:
        """Issue *method* on the GitHub API and return the parsed JSON body.

        Returns an empty dict for HTTP 204 No Content (used by
        ``set_actions_secret`` and ``dispatch_workflow``).  *owner*/*repo*
        default to the values parsed from a ``/repos/{owner}/{repo}/...``
        path so the installation token can be resolved per repository when
        no id is pinned.
        """
        if owner is None and repo is None:
            derived = _owner_repo_from_path(path)
            if derived is not None:
                owner, repo = derived
        url = f"{self._base_url}{path}"
        result = await self._http_with_retry(
            method,
            url,
            owner=owner,
            repo=repo,
            headers=await self._gh_headers(owner=owner, repo=repo),
            timeout=self._s.timeout,
            json_body=body,
            label="GitHub API",
        )
        if result.error:
            raise RuntimeError(f"GitHub API {method} {path}: {result.error}")
        if result.status_code == 204:
            return {}
        try:
            return json.loads(result.text or "")
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"GitHub API {method} {path}: invalid JSON: {exc}"
            ) from exc

    async def _post_json(self, path: str, body: dict[str, Any]) -> Any:
        """POST *path* on the GitHub API and return the parsed JSON body."""
        return await self._request_json("POST", path, body)

    async def _patch_json(self, path: str, body: dict[str, Any]) -> Any:
        """PATCH *path* on the GitHub API and return the parsed JSON body."""
        return await self._request_json("PATCH", path, body)

    # -- shared git helpers ------------------------------------------------

    def _resolve_local_path(self, path: str, local_path: str) -> Path:
        """Resolve *local_path* and confine it to the file-hub work dir.

        The file-hub work directory is the only location the agent may
        read bytes from — the chat container's ``/data`` also holds config
        and secrets that must never be committable.  ``Path.resolve()``
        collapses ``..`` segments and follows symlinks, so a traversal or
        symlink escape resolves to a path that is not relative to the root
        and is rejected.

        Raises ValueError when no work dir is configured or the resolved
        path lands outside it.
        """
        if not self._file_hub_work_dir:
            raise ValueError(
                f"local_path for '{path}' is not allowed: the file-hub work "
                "directory (file_hub_tools.working_dir) is not configured."
            )
        root = Path(self._file_hub_work_dir).resolve()
        resolved = Path(local_path).resolve()
        if resolved != root and not resolved.is_relative_to(root):
            raise ValueError(
                f"local_path for '{path}' resolves outside the allowed "
                f"file-hub work directory ({root}): {local_path}"
            )
        return resolved

    def _blob_body_for_entry(self, path: str, f: dict[str, str]) -> dict[str, str]:
        """Return the ``/git/blobs`` POST body for one file entry.

        Selects the encoding from the entry's content source: ``local_path``
        and ``content_b64`` produce base64 blobs; plain ``content`` (default)
        produces a utf-8 blob.
        """
        if "local_path" in f:
            resolved = self._resolve_local_path(path, f["local_path"])
            try:
                data = resolved.read_bytes()
            except FileNotFoundError as exc:
                raise ValueError(
                    f"local_path for '{path}' does not exist: {f['local_path']}"
                ) from exc
            except OSError as exc:
                raise ValueError(
                    f"Could not read local_path for '{path}': {exc}"
                ) from exc
            return {
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
            }
        if "content_b64" in f:
            return {"content": f["content_b64"], "encoding": "base64"}
        return {"content": f.get("content", ""), "encoding": "utf-8"}

    async def _git_create_tree_items(
        self,
        repo_full_name: str,
        files: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Create a blob for each entry in *files*; return create-tree items.

        Each entry MUST carry a ``path`` and exactly one content source:

        - ``content`` — text, uploaded with ``encoding: utf-8``.
        - ``content_b64`` — base64 bytes, uploaded with ``encoding: base64``.
        - ``local_path`` — an absolute path INSIDE the file-hub work
          directory; its bytes are read, base64-encoded, and uploaded with
          ``encoding: base64``.

        Normalizes changelog-fragment trailing newlines (text ``content``
        entries only) and validates that every entry carries a ``path``
        field (before any blob is uploaded).

        Raises ValueError if any file entry is missing a ``path`` field,
        carries a ``local_path`` that resolves outside the file-hub work
        directory, or names a ``local_path`` that cannot be read.
        Raises RuntimeError on GitHub API failures.
        """
        # Normalize changelog fragment trailing newlines (text content only)
        for f in files:
            if (
                "content" in f
                and f.get("path", "").startswith("changelog.d/")
                and f["path"].endswith(".md")
                and not f.get("content", "").endswith("\n")
            ):
                f["content"] = f["content"] + "\n"

        tree_items: list[dict[str, Any]] = []
        for f in files:
            path = f.get("path", "")
            if not path:
                raise ValueError("Each file entry must have a 'path' field.")
            blob_body = self._blob_body_for_entry(path, f)
            blob_data = await self._post_json(
                f"/repos/{repo_full_name}/git/blobs",
                blob_body,
            )
            tree_items.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_data["sha"],
                }
            )
        return tree_items

    async def _git_create_tree(
        self,
        repo_full_name: str,
        base_tree_sha: str,
        files: list[dict[str, str]],
    ) -> str:
        """Create blobs for *files* and a tree based on *base_tree_sha*; return its SHA.

        Each entry in *files* is ``{"path": ..., "content": ...}``; the blob
        contents are overlaid onto *base_tree_sha* — paths not present in
        *files* keep their content from the base tree.

        Raises ValueError if any file entry is missing a ``path`` field.
        Raises RuntimeError on GitHub API failures.
        """
        tree_items = await self._git_create_tree_items(repo_full_name, files)
        tree_data = await self._post_json(
            f"/repos/{repo_full_name}/git/trees",
            {
                "base_tree": base_tree_sha,
                "tree": tree_items,
            },
        )
        return str(tree_data["sha"])

    async def _git_push_files(
        self,
        repo_full_name: str,
        base_sha: str,
        files: list[dict[str, str]],
        commit_message: str,
    ) -> str:
        """Create blobs, tree, and commit on *repo_full_name*; return the commit SHA.

        Raises ValueError if any file entry is missing a ``path`` field.
        Raises RuntimeError on GitHub API failures.
        """
        # 1. Create a blob for each file (validates paths first)
        tree_items = await self._git_create_tree_items(repo_full_name, files)

        # 2. Create a tree from the blobs, based on the base commit's tree
        base_commit = await self._get_json(
            f"/repos/{repo_full_name}/git/commits/{base_sha}"
        )
        tree_data = await self._post_json(
            f"/repos/{repo_full_name}/git/trees",
            {
                "base_tree": base_commit["tree"]["sha"],
                "tree": tree_items,
            },
        )

        # 3. Create a commit
        commit_data = await self._post_json(
            f"/repos/{repo_full_name}/git/commits",
            {
                "message": commit_message,
                "tree": tree_data["sha"],
                "parents": [base_sha],
            },
        )

        return str(commit_data["sha"])

    # -- public API --------------------------------------------------------

    async def create_repo(
        self,
        *,
        org_name: str,
        repo_name: str,
        auto_init: bool = True,
    ) -> str:
        """Create a new repository under the GitHub organisation.

        Calls ``POST /orgs/{org}/repos``.  When *auto_init* is ``True``
        (the default), seeds the new repo with an initial ``README.md``
        so that workflows and branch pushes can proceed immediately
        — an empty repo has no default branch to push against, which
        creates a deadlock for any automated bootstrap process.

        Never raises — returns a success/error message string.
        """
        try:
            data = await self._post_json(
                f"/orgs/{org_name}/repos",
                {
                    "name": repo_name,
                    "auto_init": auto_init,
                },
            )
            html_url = data.get("html_url", "")
            return f"Repository '{org_name}/{repo_name}' created successfully.\n" + (
                f"URL: {html_url}" if html_url else ""
            )
        except RuntimeError as exc:
            return f"Error creating repo: {exc}"
        except Exception as exc:
            return f"Error creating repo: {exc}"

    async def list_installation_repos(
        self, *, owner: str | None = None, repo: str | None = None
    ) -> list[str]:
        """Return the set of ``owner/name`` repos in the installation scope.

        Resolved dynamically from the GitHub App installation — NOT a static
        allowlist — so adding/removing repos from the app changes what the
        agent can act on with no code change.

        Paginates through all pages to capture every repo in the installation
        (the API defaults to ``per_page=30`` and installations routinely have
        more repos than that).  *owner*/*repo* provide the repository context
        used to resolve the installation token per repository when no fixed
        installation id is pinned (the ``/installation/repositories`` path has
        none of its own).
        """
        per_page = 100
        page = 1
        all_repos: list[str] = []

        while True:
            data = await self._get_json(
                f"/installation/repositories?per_page={per_page}&page={page}",
                owner=owner,
                repo=repo,
            )
            repos: list[dict[str, Any]] = data.get("repositories", [])
            all_repos.extend(r["full_name"] for r in repos if "full_name" in r)
            if len(repos) < per_page:
                break
            page += 1

        return all_repos

    async def check_installation_scope(self, repo_full_name: str) -> str | None:
        """Check whether *repo_full_name* is in the GitHub App installation scope.

        This is a dedicated diagnostic step that runs before any push/PR/Actions
        operation.  It queries the GitHub API for the current installation's
        repository list and returns an actionable error message when the repo
        is not installed, or ``None`` when the repo is in scope.

        Returns:
            An error message string suitable for relaying to the user, or
            ``None`` if the repo is in the installation scope.

        """
        owner, _, repo = repo_full_name.partition("/")
        allowed = await self.list_installation_repos(
            owner=owner or None, repo=repo or None
        )
        if repo_full_name in allowed:
            return None
        if allowed:
            return (
                f"The robotsix-mill GitHub App is not installed on "
                f"'{repo_full_name}'. Install the app on this repository "
                f"and try again. Currently installed on: "
                f"{', '.join(sorted(allowed))}."
            )
        return (
            f"The robotsix-mill GitHub App is not installed on any "
            f"repository. Install the app on '{repo_full_name}' and "
            f"try again."
        )

    async def get_installation_token_diagnostics(
        self,
        repo_full_name: str,
    ) -> dict[str, Any]:
        """Mint a fresh installation token and return its expiry and scope.

        Mints a fresh installation token for *repo_full_name* and returns its
        expiry and permission scope for diagnosis.  Mints via the public
        ``mint_installation_token(owner=, repo=)`` path so the returned
        ``permissions`` reflect the GitHub App's **current** grant — not a
        possibly-stale cached token.  This lets the agent distinguish "the
        token was cached/stale" from "the App genuinely lacks the
        permission" when a GitHub API call fails with a 403 permission error.

        The returned dict carries ``configured_installation_id`` (from
        settings) and ``resolved_installation_id``.  The public
        ``mint_installation_token`` resolves the effective installation
        internally but does not expose it on the returned
        ``InstallationToken``, so the separate per-repo resolution that used
        to reach into robotsix-github-auth private internals has been dropped
        (documented fallback): in override mode the effective installation is
        the configured id and that is reported; in per-repository mode the
        effective id is not surfaced and ``resolved_installation_id`` mirrors
        the (empty) configured value.

        It further reports ``installation_mode`` (``"override"`` when an id
        is pinned, else ``"per_repository"``) and, when an id is configured,
        ``configured_installation_exists`` (``True``/``False`` from probing
        the pinned installation, or ``None`` when the probe was inconclusive)
        so an operator can tell whether a pinned override still points at a
        live installation.

        Raises:
            RuntimeError: When GitHub App credentials are missing or the
                token cannot be minted (e.g. the repo has no installation).
            ValueError: When *repo_full_name* is not ``owner/repo``.

        """
        if not (
            self._s.github_app_id and self._s.github_app_private_key.get_secret_value()
        ):
            raise RuntimeError(
                "GitHub App credentials are not configured "
                "(github_app_id / github_app_private_key)."
            )

        owner, sep, repo = repo_full_name.partition("/")
        if not sep or not repo or "/" in repo:
            raise ValueError("repo_full_name must be 'owner/repo'.")

        from robotsix_github_auth import mint_installation_token

        app_id = self._s.github_app_id
        private_key = self._s.github_app_private_key.get_secret_value()

        try:
            result = await asyncio.to_thread(
                mint_installation_token,
                app_id=app_id,
                private_key=private_key,
                owner=owner,
                repo=repo,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to mint a fresh installation token for "
                f"'{repo_full_name}': {exc}"
            ) from exc

        # Which token-resolution mode is active, and — in override mode —
        # whether the pinned installation still exists (a re-install mints a
        # new id, orphaning the old one).
        configured_id = self._s.github_app_installation_id
        installation_mode = "override" if configured_id else "per_repository"
        # The effective (per-repo) installation id is not exposed by the
        # public token path; in override mode it *is* the configured id, so
        # that is reported (see the docstring's documented fallback).
        resolved_installation_id = configured_id

        configured_installation_exists: bool | None = None
        if configured_id:

            def _probe_configured() -> bool:
                """Return True if the pinned id can mint, False on HTTP 404."""
                try:
                    mint_installation_token(
                        app_id=app_id,
                        private_key=private_key,
                        installation_id=configured_id,
                    )
                except Exception as exc:  # HTTP 404 → installation is gone
                    if "404" in str(exc):
                        return False
                    raise
                return True

            try:
                configured_installation_exists = await asyncio.to_thread(
                    _probe_configured
                )
            except Exception:  # best-effort probe — inconclusive on error
                configured_installation_exists = None

        return {
            "app_id": app_id,
            "configured_installation_id": configured_id,
            "resolved_installation_id": resolved_installation_id,
            "installation_mode": installation_mode,
            "configured_installation_exists": configured_installation_exists,
            "expires_at": result.expires_at.isoformat(),
            "seconds_remaining": round(result.seconds_remaining, 1),
            "permissions": dict(result.permissions),
        }

    async def push_branch(
        self,
        *,
        repo_full_name: str,
        branch_name: str,
        files: list[dict[str, str]],
        commit_message: str,
        ticket_id: str,
    ) -> str:
        """Push a new branch with file changes using the Git database API.

        Steps: get default branch SHA → create blobs → create tree →
        create commit → create ref.

        Never raises — returns a success/error message string.
        """
        try:
            # 1. Get the default branch HEAD SHA
            repo = await self._get_json(f"/repos/{repo_full_name}")
            default_branch = repo.get("default_branch", "main")
            ref_data = await self._get_json(
                f"/repos/{repo_full_name}/git/ref/heads/{default_branch}"
            )
            base_sha: str = ref_data["object"]["sha"]

            # 2. Create blobs, tree, and commit
            commit_sha = await self._git_push_files(
                repo_full_name=repo_full_name,
                base_sha=base_sha,
                files=files,
                commit_message=commit_message,
            )

            # 3. Create the branch ref
            await self._post_json(
                f"/repos/{repo_full_name}/git/refs",
                {
                    "ref": f"refs/heads/{branch_name}",
                    "sha": commit_sha,
                },
            )

            branch_url = (
                f"{self._base_url.replace('api.', '')}"
                if "api." in self._base_url
                else self._base_url.replace("api.github.com", "github.com")
            )
            branch_url = branch_url.rstrip("/")
            return (
                f"Branch '{branch_name}' pushed successfully to {repo_full_name}.\n"
                f"URL: {branch_url}/{repo_full_name}/tree/{branch_name}"
            )
        except RuntimeError as exc:
            return f"Error pushing branch: {exc}"
        except Exception as exc:
            return f"Error pushing branch: {exc}"

    async def installation_account(self) -> str | None:
        """Return the GitHub account (user or org) the App is installed on.

        Derived from the owner prefix of the installation's repositories —
        the most common owner wins, so the answer is the account that
        actually holds the fleet's code (a *user* such as
        ``damien-robotsix``, not an organisation guessed from a product
        name).  Returns ``None`` when the installation has no repositories.
        """
        repos = await self.list_installation_repos()
        counts: dict[str, int] = {}
        for full in repos:
            owner = full.split("/", 1)[0]
            if owner:
                counts[owner] = counts.get(owner, 0) + 1
        if not counts:
            return None
        return max(counts, key=lambda k: (counts[k], k))

    async def _search_issues(
        self,
        raw_query: str,
        *,
        per_page: int = 100,
        owner: str | None = None,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run *raw_query* against ``/search/issues`` and gather every page.

        *owner*/*repo* carry the repository context (the ``/search`` path has
        none) so the installation token resolves per repository when no id is
        pinned.
        """
        query = quote(raw_query, safe="")
        all_items: list[dict[str, Any]] = []
        page = 1

        while page <= 10:
            data = await self._get_json(
                f"/search/issues?q={query}&per_page={per_page}&page={page}",
                owner=owner,
                repo=repo,
            )
            items: list[dict[str, Any]] = data.get("items", [])
            all_items.extend(items)
            if len(items) < per_page:
                break
            page += 1

        return all_items

    async def push_commit_to_branch(
        self,
        *,
        repo_full_name: str,
        branch_name: str,
        files: list[dict[str, str]],
        commit_message: str,
        ticket_id: str,
    ) -> str:
        """Push a commit directly to an existing branch (no new branch created).

        Uses the Git database API: get branch HEAD SHA → create blobs →
        create tree → create commit → update ref to point to the new commit.

        This is the underlying operation for ``direct_fix`` — it pushes
        directly to the target branch, bypassing the PR flow.

        Never raises — returns a success/error message string.
        """
        try:
            # 1. Get the target branch HEAD SHA
            ref_data = await self._get_json(
                f"/repos/{repo_full_name}/git/ref/heads/{branch_name}"
            )
            base_sha: str = ref_data["object"]["sha"]

            # 2. Create blobs, tree, and commit
            commit_sha = await self._git_push_files(
                repo_full_name=repo_full_name,
                base_sha=base_sha,
                files=files,
                commit_message=commit_message,
            )

            # 3. Update the branch ref to point to the new commit.
            #    force=False means the update must be a fast-forward.
            await self._patch_json(
                f"/repos/{repo_full_name}/git/refs/heads/{branch_name}",
                {
                    "sha": commit_sha,
                    "force": False,
                },
            )

            return (
                f"Commit pushed successfully to {repo_full_name}/{branch_name}.\n"
                f"Commit SHA: {commit_sha}\n"
                f"Ticket: {ticket_id}"
            )
        except RuntimeError as exc:
            return f"Error pushing commit: {exc}"
        except Exception as exc:
            return f"Error pushing commit: {exc}"

    async def push_files_to_branch(
        self,
        *,
        repo_full_name: str,
        branch_name: str,
        files: list[dict[str, str]],
        deletes: list[str],
        commit_message: str,
    ) -> str:
        """Push a multi-file changeset (create/overwrite + delete) to a branch.

        Commits *files* (each ``{"path": ..., <content source>}``) and removes
        every path in *deletes* in a SINGLE commit on the existing
        *branch_name*.  Deletions are expressed as tree entries with a ``null``
        SHA, overlaid on the branch head's tree, so a rename/move is
        expressible as (delete old path + create new path).

        Uses the Git database API: get branch HEAD SHA → create blobs for the
        content entries → build a tree (blobs + null-SHA deletes) overlaid on
        the head tree → create commit → fast-forward the ref.

        Never raises — returns a success/error message string.
        """
        try:
            # 1. Resolve the branch HEAD SHA (the update's parent).
            ref_data = await self._get_json(
                f"/repos/{repo_full_name}/git/ref/heads/{branch_name}"
            )
            base_sha: str = ref_data["object"]["sha"]

            # 2. Blobs for content entries, plus null-SHA entries for deletes.
            tree_items = await self._git_create_tree_items(repo_full_name, files)
            for path in deletes:
                if not path:
                    raise ValueError("Each delete entry must have a 'path' field.")
                tree_items.append(
                    {
                        "path": path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": None,
                    }
                )

            # 3. Overlay the tree on the branch head's tree.
            base_commit = await self._get_json(
                f"/repos/{repo_full_name}/git/commits/{base_sha}"
            )
            tree_data = await self._post_json(
                f"/repos/{repo_full_name}/git/trees",
                {
                    "base_tree": base_commit["tree"]["sha"],
                    "tree": tree_items,
                },
            )

            # 4. Create the commit with the branch head as its parent.
            commit_data = await self._post_json(
                f"/repos/{repo_full_name}/git/commits",
                {
                    "message": commit_message,
                    "tree": tree_data["sha"],
                    "parents": [base_sha],
                },
            )
            commit_sha = str(commit_data["sha"])

            # 5. Fast-forward the branch ref to the new commit.
            await self._patch_json(
                f"/repos/{repo_full_name}/git/refs/heads/{branch_name}",
                {
                    "sha": commit_sha,
                    "force": False,
                },
            )

            return (
                f"Commit pushed successfully to {repo_full_name}/{branch_name}.\n"
                f"Commit SHA: {commit_sha}"
            )
        except RuntimeError as exc:
            return f"Error pushing commit: {exc}"
        except Exception as exc:
            return f"Error pushing commit: {exc}"

    async def set_security_and_analysis(
        self,
        repo_full_name: str,
        *,
        dependency_graph: str | None = None,
        advanced_security: str | None = None,
        secret_scanning: str | None = None,
        secret_scanning_push_protection: str | None = None,
    ) -> str:
        """Enable or disable repository security features.

        Sets the ``security_and_analysis`` block on a repo via
        ``PATCH /repos/{owner}/{repo}``.  Each argument accepts
        ``"enabled"`` or ``"disabled"``; ``None`` leaves the setting
        unchanged.

        Never raises — returns a success/error message string.
        """
        valid = frozenset({"enabled", "disabled"})
        for name, val in (
            ("dependency_graph", dependency_graph),
            ("advanced_security", advanced_security),
            ("secret_scanning", secret_scanning),
            ("secret_scanning_push_protection", secret_scanning_push_protection),
        ):
            if val is not None and val not in valid:
                return f"Error: {name} must be 'enabled' or 'disabled', got {val!r}"

        body: dict[str, Any] = {"security_and_analysis": {}}
        for key, val in (
            ("dependency_graph", dependency_graph),
            ("advanced_security", advanced_security),
            ("secret_scanning", secret_scanning),
            (
                "secret_scanning_push_protection",
                secret_scanning_push_protection,
            ),
        ):
            if val is not None:
                body["security_and_analysis"][key] = {"status": val}

        if not body["security_and_analysis"]:
            return "Error: at least one security feature must be specified."

        try:
            data = await self._patch_json(
                f"/repos/{repo_full_name}",
                body,
            )
            changed = list(body["security_and_analysis"].keys())
            return (
                f"Security settings updated for {repo_full_name}: "
                f"{', '.join(changed)}.\n"
                f"Response: {json.dumps(data, indent=2)}"
            )
        except RuntimeError as exc:
            return f"Error updating security settings: {exc}"
        except Exception as exc:
            return f"Error updating security settings: {exc}"

    async def _get_pages_site(self, repo_full_name: str) -> dict[str, Any] | None:
        """Return the GitHub Pages site JSON for a repo, or None on failure.

        Used for read-back verification after enabling/updating Pages.
        Never raises — callers degrade gracefully when the read-back fails.
        """
        try:
            return cast(
                "dict[str, Any] | None",
                await self._get_json(f"/repos/{repo_full_name}/pages"),
            )
        except RuntimeError:
            return None

    @staticmethod
    def _format_pages_result(
        repo_full_name: str,
        verb: str,
        site: dict[str, Any] | None,
        build_type: str,
    ) -> str:
        """Format an enable/update Pages result, including read-back status."""
        lines = [f"GitHub Pages {verb} on {repo_full_name} (build_type: {build_type})."]
        if site:
            lines.append(f"Site status: {site.get('status', 'unknown')}")
            reported_build_type = site.get("build_type", "unknown")
            if reported_build_type != build_type:
                lines.append(f"Reported build type: {reported_build_type}")
            html_url = site.get("html_url", "")
            if html_url:
                lines.append(f"Site URL: {html_url}")
        else:
            lines.append("Site status: could not be read back.")
        return "\n".join(lines)

    async def enable_pages(
        self,
        repo_full_name: str,
        build_type: str = "workflow",
    ) -> str:
        """Enable GitHub Pages built from a workflow on a repository.

        Calls ``POST /repos/{owner}/{repo}/pages`` with *build_type* and
        then reads the resulting site back so the result includes its
        current status.  Idempotent: a 409 (Pages already enabled) is
        treated as success — switching the build type via ``PUT`` when it
        differs from the requested value.  A 403 is reported as a clear
        permission error rather than raised.

        Never raises — returns a success/error message string.
        """
        if build_type not in {"workflow", "legacy"}:
            return (
                f"Error: build_type must be 'workflow' or 'legacy', got {build_type!r}"
            )

        owner_part, _, repo_part = repo_full_name.partition("/")
        owner = owner_part or None
        repo = repo_part or None
        try:
            result = await self._http_with_retry(
                "POST",
                f"{self._base_url}/repos/{repo_full_name}/pages",
                owner=owner,
                repo=repo,
                headers=await self._gh_headers(owner=owner, repo=repo),
                timeout=self._s.timeout,
                json_body={"build_type": build_type},
                label="GitHub API",
            )
        except Exception as exc:
            return f"Error enabling GitHub Pages on {repo_full_name}: {exc}"

        if result.error is None and result.status_code in (200, 201, 204):
            site = await self._get_pages_site(repo_full_name)
            return self._format_pages_result(
                repo_full_name, "enabled", site, build_type
            )

        if result.status_code == 403:
            return (
                f"Error enabling GitHub Pages on {repo_full_name}: permission "
                f"denied — the GitHub App installation token lacks "
                f"'pages: write'. Use inspect_github_installation_token to "
                f"confirm the current permission scope, then grant Pages "
                f"read/write access and retry."
            )

        if result.status_code == 409:
            existing = await self._get_pages_site(repo_full_name)
            if existing is not None and existing.get("build_type") == build_type:
                return self._format_pages_result(
                    repo_full_name, "already enabled", existing, build_type
                )
            # Pages exists under a different build_type — switch via PUT.
            try:
                updated = await self._http_with_retry(
                    "PUT",
                    f"{self._base_url}/repos/{repo_full_name}/pages",
                    owner=owner,
                    repo=repo,
                    headers=await self._gh_headers(owner=owner, repo=repo),
                    timeout=self._s.timeout,
                    json_body={"build_type": build_type},
                    label="GitHub API",
                )
            except Exception as exc:
                return (
                    f"GitHub Pages is already enabled on {repo_full_name} but "
                    f"switching build_type to {build_type!r} failed: {exc}"
                )
            if updated.error is None:
                site = await self._get_pages_site(repo_full_name)
                return self._format_pages_result(
                    repo_full_name, "updated", site, build_type
                )
            return (
                f"Error updating GitHub Pages build_type on {repo_full_name}: "
                f"{updated.error}"
            )

        return (
            f"Error enabling GitHub Pages on {repo_full_name}: "
            f"{result.error or f'HTTP {result.status_code}'}"
        )

    # -- merge helpers -----------------------------------------------------

    async def check_auto_merge_enabled(
        self,
        *,
        repo_full_name: str,
    ) -> str:
        """Check whether auto-merge is enabled on a repository.

        Calls ``GET /repos/{owner}/{repo}`` and reads the
        ``allow_auto_merge`` field from the repository object.

        Args:
            repo_full_name: ``"owner/name"``.

        Returns:
            A human-readable message indicating whether auto-merge is
            enabled, or an error message if the repository could not be
            fetched.

        Never raises — returns an error string on any failure.

        """
        try:
            repo = await self._get_json(f"/repos/{repo_full_name}")
        except RuntimeError as exc:
            return f"Error fetching repository metadata for {repo_full_name}: {exc}"

        allow_auto_merge = repo.get("allow_auto_merge", False)
        if allow_auto_merge:
            return (
                f"Auto-merge is **enabled** on {repo_full_name}.  "
                f"PRs with auto-merge armed will be merged automatically "
                f"once all required conditions (CI, reviews, branch "
                f"protection) are satisfied."
            )
        return (
            f"Auto-merge is **disabled** on {repo_full_name}.  "
            f"The repository has ``allow_auto_merge`` set to false.  "
            f"PRs cannot be armed for automatic merging — all merges "
            f"must be performed manually via the GitHub UI or the "
            f"``merge_direct_repo_pr`` tool."
        )

    async def arm_auto_merge(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        merge_method: str = "squash",
    ) -> str:
        """Enable auto-merge on a pull request.

        Calls ``PUT /repos/{owner}/{repo}/pulls/{pull_number}/auto-merge``.

        When auto-merge is enabled GitHub will automatically merge the PR
        as soon as all required conditions are met (status checks pass,
        required reviews are submitted, branch protection rules are
        satisfied).  The merge happens without further human intervention.

        Args:
            repo_full_name: ``"owner/name"``.
            pr_number: The PR number to enable auto-merge on.
            merge_method: ``"squash"`` (default), ``"merge"``, or ``"rebase"``.

        Returns:
            A success message, or an error message describing why auto-merge
            could not be enabled.

        Never raises — returns an error string on any failure.

        """
        try:
            # --- pre-flight: fetch PR to check state ---
            pr = await self.get_pr(repo_full_name=repo_full_name, pr_number=pr_number)
        except RuntimeError as exc:
            return f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}"

        if pr.get("draft"):
            return (
                f"Cannot enable auto-merge on PR #{pr_number} in "
                f"{repo_full_name}: the PR is still in draft state."
            )

        if pr.get("merged"):
            return (
                f"PR #{pr_number} in {repo_full_name} is already merged — "
                f"auto-merge is not applicable."
            )

        body: dict[str, Any] = {"merge_method": merge_method}
        try:
            await self._request_json(
                "PUT",
                f"/repos/{repo_full_name}/pulls/{pr_number}/auto-merge",
                body,
            )
        except RuntimeError as exc:
            msg = str(exc)
            if "403" in msg or "404" in msg:
                return (
                    f"Cannot enable auto-merge on PR #{pr_number} in "
                    f"{repo_full_name}: the repository may not have "
                    f"auto-merge enabled, or branch protection rules "
                    f"prevent it.  Use ``check_direct_repo_auto_merge`` "
                    f"to verify the repository's auto-merge setting.  "
                    f"GitHub response: {msg}"
                )
            return (
                f"Error enabling auto-merge on PR #{pr_number} in "
                f"{repo_full_name}: {msg}"
            )

        return (
            f"Auto-merge enabled on PR #{pr_number} in {repo_full_name} "
            f"using {merge_method}.  The PR will be merged automatically "
            f"once all required conditions are met."
        )

    # -- file-content helpers (for apply_patch_to_file) -------------------

    async def get_file_content(
        self,
        repo_full_name: str,
        path: str,
        ref: str | None = None,
    ) -> tuple[str, str]:
        """Fetch a single file's content and blob SHA from the GitHub Contents API.

        Calls ``GET /repos/{owner}/{repo}/contents/{path}``.

        Args:
            repo_full_name: ``"owner/name"``.
            path: File path relative to the repo root.
            ref: Optional branch/commit SHA (defaults to the repo default branch).

        Returns:
            ``(decoded_text_content, blob_sha)``.

        Raises:
            RuntimeError: On any API or decoding failure.
            ValueError: If the path is a directory, not a file.

        """
        api_path = f"/repos/{repo_full_name}/contents/{path}"
        if ref:
            api_path += f"?ref={ref}"
        data = await self._get_json(api_path)

        # GitHub returns a JSON array for directories.
        if isinstance(data, list):
            raise ValueError(
                f"Path '{path}' in {repo_full_name} is a directory, not a file."
            )

        encoding = data.get("encoding", "")
        content_b64 = data.get("content", "")
        sha: str = data.get("sha", "")

        if encoding != "base64":
            raise RuntimeError(
                f"Unexpected encoding '{encoding}' for {path} in {repo_full_name}."
            )

        try:
            text = _b64decode(content_b64).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"Failed to decode content for {path} in {repo_full_name}: {exc}"
            ) from exc

        return text, sha

    async def push_patched_file(
        self,
        *,
        repo_full_name: str,
        branch_name: str,
        file_path: str,
        patch_text: str,
        commit_message: str,
        ticket_id: str,
    ) -> str:
        """Fetch a file, apply a unified diff, and push the result as a commit.

        Steps:
        1. Fetch the current file content from *branch_name* via the
           GitHub Contents API.
        2. Apply *patch_text* (unified diff) to the content.
        3. Push the patched content as a commit on *branch_name*.

        Never raises — returns a success/error message string.
        """
        try:
            original, _sha = await self.get_file_content(
                repo_full_name, file_path, ref=branch_name
            )
        except (RuntimeError, ValueError) as exc:
            return f"Error fetching file '{file_path}' from {repo_full_name}: {exc}"

        try:
            from robotsix_chat.common.unified_diff import apply_patch

            patched = apply_patch(original, patch_text)
        except ValueError as exc:
            return f"Error applying patch to '{file_path}' in {repo_full_name}: {exc}"

        if patched == original:
            return (
                f"Patch applied to '{file_path}' in {repo_full_name} produced "
                f"no changes — the file is already in the desired state."
            )

        result = await self.push_commit_to_branch(
            repo_full_name=repo_full_name,
            branch_name=branch_name,
            files=[{"path": file_path, "content": patched}],
            commit_message=commit_message,
            ticket_id=ticket_id,
        )

        return result

    # -- board API delegation ----------------------------------------------

    async def get_ticket_data(self, ticket_id: str) -> dict[str, Any] | None:
        """Return the full ticket JSON from the board API, or None on failure.

        Delegates to :class:`BoardClient`.
        """
        from robotsix_chat.repo.direct.board_client import BoardClient

        return await BoardClient(self._s).get_ticket_data(ticket_id)

    async def count_implement_cycles(self, ticket_id: str) -> int | None:
        """Return the number of implement cycles for *ticket_id*, or None.

        Delegates to :class:`BoardClient`.
        """
        from robotsix_chat.repo.direct.board_client import BoardClient

        return await BoardClient(self._s).count_implement_cycles(ticket_id)

    # -- actions API delegation --------------------------------------------

    async def list_workflow_runs(
        self,
        repo_full_name: str,
        *,
        branch: str | None = None,
        per_page: int = 10,
    ) -> list[dict[str, Any]]:
        """List recent workflow runs for a repository.

        Delegates to :class:`ActionsClient`.
        """
        from robotsix_chat.repo.direct.actions_client import ActionsClient

        return await ActionsClient(self._s).list_workflow_runs(
            repo_full_name, branch=branch, per_page=per_page
        )

    async def get_workflow_run_jobs(
        self,
        repo_full_name: str,
        run_id: int,
    ) -> list[dict[str, Any]]:
        """Return jobs for a specific workflow run.

        Delegates to :class:`ActionsClient`.
        """
        from robotsix_chat.repo.direct.actions_client import ActionsClient

        return await ActionsClient(self._s).get_workflow_run_jobs(
            repo_full_name, run_id
        )

    async def _diagnose_billing_failure(
        self,
        runs: list[dict[str, Any]],
        repo_full_name: str,
    ) -> str | None:
        """Inspect recent workflow runs for a private-repo billing failure.

        Delegates to :class:`ActionsClient`.
        """
        from robotsix_chat.repo.direct.actions_client import ActionsClient

        return await ActionsClient(self._s)._diagnose_billing_failure(
            runs, repo_full_name
        )

    @staticmethod
    def apply_patch(original: str, patch_text: str) -> str:
        """Apply a unified diff to original text and return the result.

        Delegates to :func:`robotsix_chat.common.unified_diff.apply_patch`.
        """
        from robotsix_chat.common.unified_diff import apply_patch as _apply

        return _apply(original, patch_text)

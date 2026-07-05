"""Direct-repo HTTP client — GitHub App-authenticated branch push + PR open.

Talks to the GitHub API as a GitHub App installation (JWT → installation
token) and to the mill's board API for ticket-state verification.  Degrades
gracefully: all errors become short strings the assistant can relay.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from robotsix_chat.common.http import safe_http_request

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GitHub App authentication helpers
# ---------------------------------------------------------------------------

_GITHUB_APP_JWT_CACHE: dict[str, tuple[float, str]] = {}
"""In-memory JWT cache keyed by ``(app_id, private_key_hash)``.

JWTs live for 10 minutes (GitHub's max) but we expire them after 9 minutes
to avoid edge-of-expiry failures.
"""

_JWT_LIFETIME_SECONDS = 9 * 60  # 9 minutes


def _make_jwt(app_id: str, private_key_pem: str) -> str:
    """Create a signed JWT for GitHub App authentication (RS256).

    Uses PyJWT if available, otherwise raises an error directing the operator
    to install the optional dependency.
    """
    try:
        import jwt
    except ImportError:
        raise RuntimeError(
            "PyJWT is required for GitHub App authentication. "
            "Install it with: uv sync --extra direct-repo  or  pip install pyjwt"
        ) from None

    now = int(time.time())
    payload = {
        "iat": now - 60,  # 60s clock drift tolerance
        "exp": now + _JWT_LIFETIME_SECONDS,
        "iss": app_id,
    }
    token = str(jwt.encode(payload, private_key_pem, algorithm="RS256"))
    return token


def _get_jwt(settings: DirectRepoSettings) -> str:
    """Return a cached or fresh JWT for the configured GitHub App."""
    import hashlib

    app_private_key = settings.github_app_private_key.get_secret_value()
    key_hash = hashlib.sha256(
        f"{settings.github_app_id}:{app_private_key[:20]}".encode()
    ).hexdigest()

    now = time.monotonic()
    if key_hash in _GITHUB_APP_JWT_CACHE:
        ts, token = _GITHUB_APP_JWT_CACHE[key_hash]
        if now - ts < _JWT_LIFETIME_SECONDS - 60:
            return token

    jwt_token = _make_jwt(settings.github_app_id, app_private_key)
    _GITHUB_APP_JWT_CACHE[key_hash] = (now, jwt_token)
    return jwt_token


# ---------------------------------------------------------------------------
# Installation token cache
# ---------------------------------------------------------------------------

_INSTALLATION_TOKEN_CACHE: dict[str, tuple[float, str]] = {}
"""In-memory installation token cache keyed by installation_id.

Installation tokens live for 1 hour; we expire after 50 minutes.
"""

_TOKEN_LIFETIME_SECONDS = 50 * 60  # 50 minutes


async def _get_installation_token(settings: DirectRepoSettings) -> str:
    """Exchange the JWT for a short-lived installation access token."""
    now = time.monotonic()
    iid = settings.github_app_installation_id
    if iid in _INSTALLATION_TOKEN_CACHE:
        ts, token = _INSTALLATION_TOKEN_CACHE[iid]
        if now - ts < _TOKEN_LIFETIME_SECONDS:
            return token

    jwt_token = _get_jwt(settings)
    base = settings.github_api_base_url.rstrip("/")
    url = f"{base}/app/installations/{iid}/access_tokens"
    result = await safe_http_request(
        "POST",
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jwt_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=settings.timeout,
        label="GitHub App token",
    )
    if result.error:
        raise RuntimeError(f"Failed to get installation token: {result.error}")

    body = result.text or ""
    try:
        data = json.loads(body)
        token = data["token"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Failed to parse installation token response: {exc}"
        ) from exc

    _INSTALLATION_TOKEN_CACHE[iid] = (now, str(token))
    return str(token)


# ---------------------------------------------------------------------------
# DirectRepoClient
# ---------------------------------------------------------------------------


class DirectRepoClient:
    """GitHub App-authenticated client for push-branch + open-PR operations."""

    def __init__(self, settings: DirectRepoSettings) -> None:
        """Store settings; tokens are fetched lazily."""
        self._s = settings
        self._base_url = settings.github_api_base_url.rstrip("/")

    # -- helpers -----------------------------------------------------------

    async def _token(self) -> str:
        """Return a valid installation access token (cached)."""
        return await _get_installation_token(self._s)

    async def _gh_headers(self) -> dict[str, str]:
        """Return headers for a GitHub API call (with installation token)."""
        token = await self._token()
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get_json(self, path: str) -> Any:
        """GET *path* on the GitHub API and return the parsed JSON body.

        Raises RuntimeError on any failure (never returns error strings —
        callers catch and format).
        """
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "GET",
            url,
            headers=await self._gh_headers(),
            timeout=self._s.timeout,
            label="GitHub API",
        )
        if result.error:
            raise RuntimeError(f"GitHub API GET {path}: {result.error}")
        try:
            return json.loads(result.text or "")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"GitHub API GET {path}: invalid JSON: {exc}") from exc

    async def _post_json(self, path: str, body: dict[str, Any]) -> Any:
        """POST *path* on the GitHub API and return the parsed JSON body."""
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "POST",
            url,
            headers=await self._gh_headers(),
            timeout=self._s.timeout,
            json_body=body,
            label="GitHub API",
        )
        if result.error:
            raise RuntimeError(f"GitHub API POST {path}: {result.error}")
        try:
            return json.loads(result.text or "")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"GitHub API POST {path}: invalid JSON: {exc}") from exc

    # -- public API --------------------------------------------------------

    async def list_installation_repos(self) -> list[str]:
        """Return the set of ``owner/name`` repos in the installation scope.

        Resolved dynamically from the GitHub App installation — NOT a static
        allowlist — so adding/removing repos from the app changes what the
        agent can act on with no code change.
        """
        data = await self._get_json("/installation/repositories")
        repos: list[dict[str, Any]] = data.get("repositories", [])
        return [r["full_name"] for r in repos if "full_name" in r]

    async def get_ticket_state(self, ticket_id: str) -> str | None:
        """Return the ticket's state (e.g. ``"BLOCKED"``), or ``None`` on failure.

        Calls the board API directly — the same endpoint the browser UI uses.
        """
        board_url = self._s.board_api_base_url.rstrip("/")
        url = f"{board_url}/tickets/{ticket_id}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._s.board_api_token.get_secret_value():
            headers["Authorization"] = (
                f"Bearer {self._s.board_api_token.get_secret_value()}"
            )
        result = await safe_http_request(
            "GET",
            url,
            headers=headers,
            timeout=self._s.timeout,
            label="Board API (ticket state)",
        )
        if result.error:
            logger.warning(
                "Failed to fetch ticket %s state: %s", ticket_id, result.error
            )
            return None
        try:
            data = json.loads(result.text or "")
            state: str | None = data.get("state")
            return state
        except json.JSONDecodeError, TypeError:
            logger.warning(
                "Non-JSON response for ticket %s: %s",
                ticket_id,
                (result.text or "")[:200],
            )
            return None

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

            # 2. Create a blob for each file
            tree_items: list[dict[str, Any]] = []
            for f in files:
                path = f.get("path", "")
                content = f.get("content", "")
                if not path:
                    return "Error: each file entry must have a 'path' field."
                blob_data = await self._post_json(
                    f"/repos/{repo_full_name}/git/blobs",
                    {
                        "content": content,
                        "encoding": "utf-8",
                    },
                )
                tree_items.append(
                    {
                        "path": path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_data["sha"],
                    }
                )

            # 3. Create a tree from the blobs, based on the base tree
            base_commit = await self._get_json(
                f"/repos/{repo_full_name}/git/commits/{base_sha}"
            )
            base_tree_sha = base_commit["tree"]["sha"]
            tree_data = await self._post_json(
                f"/repos/{repo_full_name}/git/trees",
                {
                    "base_tree": base_tree_sha,
                    "tree": tree_items,
                },
            )

            # 4. Create a commit
            commit_data = await self._post_json(
                f"/repos/{repo_full_name}/git/commits",
                {
                    "message": commit_message,
                    "tree": tree_data["sha"],
                    "parents": [base_sha],
                },
            )

            # 5. Create the branch ref
            await self._post_json(
                f"/repos/{repo_full_name}/git/refs",
                {
                    "ref": f"refs/heads/{branch_name}",
                    "sha": commit_data["sha"],
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

    async def create_pr(
        self,
        *,
        repo_full_name: str,
        head_branch: str,
        title: str,
        body: str,
    ) -> str:
        """Open a pull request.  No auto-merge — human review required.

        Never raises — returns a success/error message string.
        """
        try:
            # Determine base branch
            repo = await self._get_json(f"/repos/{repo_full_name}")
            default_branch = repo.get("default_branch", "main")

            pr_data = await self._post_json(
                f"/repos/{repo_full_name}/pulls",
                {
                    "title": title,
                    "body": body,
                    "head": head_branch,
                    "base": default_branch,
                },
            )
            pr_url = pr_data.get("html_url", "")
            return (
                f"Pull request opened successfully.\n"
                f"URL: {pr_url}\n"
                f"Auto-merge is NOT enabled — human review required before merge."
            )
        except RuntimeError as exc:
            return f"Error opening PR: {exc}"
        except Exception as exc:
            return f"Error opening PR: {exc}"

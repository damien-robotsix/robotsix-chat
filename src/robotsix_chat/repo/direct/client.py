"""Direct-repo HTTP client — GitHub App-authenticated branch push + PR open.

Talks to the GitHub API as a GitHub App installation (JWT → installation
token) and to the mill's board API for ticket-state verification.  Degrades
gracefully: all errors become short strings the assistant can relay.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import TYPE_CHECKING, Any, cast

from robotsix_chat.common.http import safe_http_request

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


async def _get_installation_token(settings: DirectRepoSettings) -> str:
    """Mint a short-lived GitHub App installation access token.

    Delegates to the shared ``robotsix_github_auth`` library — no in-container
    JWT logic remains.
    """
    from robotsix_github_auth import mint_installation_token

    result = await asyncio.to_thread(
        mint_installation_token,
        app_id=settings.github_app_id,
        private_key=settings.github_app_private_key.get_secret_value(),
        installation_id=settings.github_app_installation_id,
    )
    return cast(str, result.token)


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

    def _invalidate_token(self) -> None:
        """Clear the cached installation token so the next call re-fetches it."""
        iid = self._s.github_app_installation_id
        _INSTALLATION_TOKEN_CACHE.pop(iid, None)

    async def _gh_headers(self) -> dict[str, str]:
        """Return headers for a GitHub API call (with installation token)."""
        token = await self._token()
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _http_with_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Any:
        """Make an HTTP request with one retry on 401 (installation token expiry).

        On the first 401 response the cached installation token is cleared
        and a fresh token is exchanged before retrying exactly once.
        Returns the ``safe_http_request`` ``HttpResult``.
        """
        result = await safe_http_request(method, url, **kwargs)
        if result.status_code == 401:
            logger.info(
                "GitHub API returned 401 — refreshing installation token and retrying"
            )
            self._invalidate_token()
            if "headers" in kwargs:
                kwargs["headers"] = await self._gh_headers()
            result = await safe_http_request(method, url, **kwargs)
        return result

    async def _get_json(self, path: str) -> Any:
        """GET *path* on the GitHub API and return the parsed JSON body.

        Raises RuntimeError on any failure (never returns error strings —
        callers catch and format).
        """
        url = f"{self._base_url}{path}"
        result = await self._http_with_retry(
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

    async def _request_json(self, method: str, path: str, body: dict[str, Any]) -> Any:
        """Issue *method* on the GitHub API and return the parsed JSON body.

        Returns an empty dict for HTTP 204 No Content (used by
        ``set_actions_secret`` and ``dispatch_workflow``).
        """
        url = f"{self._base_url}{path}"
        result = await self._http_with_retry(
            method,
            url,
            headers=await self._gh_headers(),
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
        # Normalize changelog fragment trailing newlines
        for f in files:
            if (
                f.get("path", "").startswith("changelog.d/")
                and f["path"].endswith(".md")
                and not f.get("content", "").endswith("\n")
            ):
                f["content"] = f["content"] + "\n"

        # 1. Create a blob for each file
        tree_items: list[dict[str, Any]] = []
        for f in files:
            path = f.get("path", "")
            content = f.get("content", "")
            if not path:
                raise ValueError("Each file entry must have a 'path' field.")
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

        # 2. Create a tree from the blobs, based on the base tree
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

    async def update_pr_branch(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Update a PR branch with the latest base-branch changes (rebase).

        Calls ``PUT /repos/{owner}/{repo}/pulls/{pull_number}/update-branch``
        which is equivalent to clicking the "Update branch" button on a GitHub
        PR.  GitHub attempts a rebase by default; if conflicts are detected the
        endpoint returns 422 with the conflict reason.

        Never raises — returns a success/error message string.
        """
        try:
            url = (
                f"{self._base_url}/repos/{repo_full_name}"
                f"/pulls/{pr_number}/update-branch"
            )
            result = await self._http_with_retry(
                "PUT",
                url,
                headers=await self._gh_headers(),
                timeout=self._s.timeout,
                label="GitHub API (update-branch)",
            )
            if result.ok:
                return (
                    f"PR #{pr_number} in {repo_full_name} has been queued for "
                    f"branch update (rebase).  The update is in progress."
                )
            # 422 = unprocessable (typically merge conflict)
            if result.status_code == 422:
                detail = result.error or "(no detail)"
                return (
                    f"PR #{pr_number} in {repo_full_name} could not be updated: "
                    f"merge conflict detected.  The branch has conflicts that "
                    f"must be resolved manually.\n"
                    f"GitHub response: {detail}"
                )
            return f"Error updating PR branch: {result.error or 'unknown error'}"
        except Exception as exc:
            return f"Error updating PR branch: {exc}"

    async def get_pr(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> Any:
        """Return the PR object from the GitHub API.

        Raises RuntimeError on failure (callers catch and format).
        """
        return await self._get_json(f"/repos/{repo_full_name}/pulls/{pr_number}")

    async def get_ticket_data(self, ticket_id: str) -> dict[str, Any] | None:
        """Return the full ticket JSON from the board API, or None on failure.

        Calls ``GET /tickets/{ticket_id}`` on the board API and returns the
        parsed JSON body.  The response includes ``state``, ``events`` (state
        transitions), and other ticket metadata.
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
            label="Board API (ticket data)",
        )
        if result.error:
            logger.warning(
                "Failed to fetch ticket %s data: %s", ticket_id, result.error
            )
            return None
        try:
            data: dict[str, Any] = json.loads(result.text or "")
            return data
        except json.JSONDecodeError, TypeError:
            logger.warning(
                "Non-JSON response for ticket %s: %s",
                ticket_id,
                (result.text or "")[:200],
            )
            return None

    async def count_implement_cycles(self, ticket_id: str) -> int | None:
        """Return the number of implement cycles for *ticket_id*, or None on failure.

        Inspects the ticket's ``events`` array (from the board API) and counts
        events whose ``type`` or ``action`` field contains the substring
        ``"implement"`` (case-insensitive).  Falls back to counting state
        transitions through ``"implement_complete"`` if no events array is
        present.
        """
        data = await self.get_ticket_data(ticket_id)
        if data is None:
            return None

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
            return count

        # 2. Fall back to state-transition history
        history: list[dict[str, Any]] = data.get("history", [])
        if history:
            count = 0
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                state = str(entry.get("state", entry.get("to", ""))).lower()
                action = str(entry.get("action", entry.get("type", ""))).lower()
                if "implement_complete" in state or "implement" in action:
                    count += 1
            return count

        # 3. No events/history — try a direct cycle_count field
        cycle_count = data.get("cycle_count")
        if isinstance(cycle_count, int):
            return cycle_count

        # 4. Can't determine — return 0 (not an error; the board may not
        #    expose cycle counts)
        logger.info(
            "Ticket %s has no events/history/cycle_count — "
            "assuming 0 implement cycles.",
            ticket_id,
        )
        return 0

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

    # -- GitHub Actions helpers --------------------------------------------

    async def _get_repo_public_key(self, repo_full_name: str) -> tuple[str, str]:
        """Return ``(key_id, public_key_b64)`` for Actions secret encryption.

        Calls ``GET /repos/{owner}/{repo}/actions/secrets/public-key``.
        """
        data = await self._get_json(
            f"/repos/{repo_full_name}/actions/secrets/public-key"
        )
        return str(data["key_id"]), str(data["key"])

    async def set_actions_secret(
        self,
        repo_full_name: str,
        secret_name: str,
        secret_value: str,
    ) -> str:
        """Create or update a repository Actions secret.

        Encrypts *secret_value* with the repo's public key using libsodium
        sealed-box encryption (requires ``pynacl``), then sends it via
        ``PUT /repos/{owner}/{repo}/actions/secrets/{secret_name}``.

        Never raises — returns a success/error message string.
        """
        try:
            from nacl.public import (  # type: ignore[import-not-found]
                PublicKey,
                SealedBox,
            )
        except ImportError:
            return (
                "Error: PyNaCl is required for Actions secret encryption. "
                "Install it with: uv sync --extra github-actions  or  "
                "pip install pynacl"
            )

        try:
            key_id, public_key_b64 = await self._get_repo_public_key(repo_full_name)
        except RuntimeError as exc:
            return f"Error fetching repo public key: {exc}"
        except Exception as exc:
            return f"Error fetching repo public key: {exc}"

        try:
            public_key_bytes = _b64decode(public_key_b64)
            sealed_box = SealedBox(PublicKey(public_key_bytes))
            encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
            encrypted_b64 = _b64encode(encrypted)
        except Exception as exc:
            return f"Error encrypting secret: {exc}"

        try:
            await self._request_json(
                "PUT",
                f"/repos/{repo_full_name}/actions/secrets/{secret_name}",
                {
                    "encrypted_value": encrypted_b64,
                    "key_id": key_id,
                },
            )
            return f"Secret '{secret_name}' set successfully on {repo_full_name}."
        except RuntimeError as exc:
            return f"Error setting secret: {exc}"
        except Exception as exc:
            return f"Error setting secret: {exc}"

    async def dispatch_workflow(
        self,
        repo_full_name: str,
        workflow_id: str,
        ref: str,
        inputs: dict[str, str] | None = None,
    ) -> str:
        """Trigger a workflow_dispatch event.

        Calls ``POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches``.

        Never raises — returns a success/error message string.
        """
        body: dict[str, Any] = {"ref": ref}
        if inputs:
            body["inputs"] = inputs

        try:
            await self._request_json(
                "POST",
                f"/repos/{repo_full_name}/actions/workflows/{workflow_id}/dispatches",
                body,
            )
            return (
                f"Workflow '{workflow_id}' dispatched successfully "
                f"on {repo_full_name} (ref: {ref})."
            )
        except RuntimeError as exc:
            return f"Error dispatching workflow: {exc}"
        except Exception as exc:
            return f"Error dispatching workflow: {exc}"

"""Direct-repo HTTP client — GitHub App-authenticated branch push + PR open.

Talks to the GitHub API as a GitHub App installation (JWT → installation
token) and to the mill's board API for ticket-state verification.  Degrades
gracefully: all errors become short strings the assistant can relay.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING, Any, cast

from robotsix_chat.common.github_auth import _build_github_app_auth_headers
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


_INSTALLATION_TOKEN_CACHE: dict[str, str] = {}


async def _get_installation_token(settings: DirectRepoSettings) -> str:
    """Mint a short-lived GitHub App installation access token.

    Delegates to the shared ``_build_github_app_auth_headers`` helper.
    Results are cached by installation id.
    """
    token = await _build_github_app_auth_headers(
        settings, "direct_repo:", token_cache=_INSTALLATION_TOKEN_CACHE
    )
    if token is None:
        raise RuntimeError(
            "Failed to mint GitHub App installation token. "
            "Check that github_app_id, github_app_private_key, "
            "and github_app_installation_id are correct."
        )
    return token


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

        Paginates through all pages to capture every repo in the installation
        (the API defaults to ``per_page=30`` and installations routinely have
        more repos than that).
        """
        per_page = 100
        page = 1
        all_repos: list[str] = []

        while True:
            data = await self._get_json(
                f"/installation/repositories?per_page={per_page}&page={page}"
            )
            repos: list[dict[str, Any]] = data.get("repositories", [])
            all_repos.extend(r["full_name"] for r in repos if "full_name" in r)
            if len(repos) < per_page:
                break
            page += 1

        return all_repos

    async def create_repo(
        self,
        org_name: str,
        repo_name: str,
        *,
        auto_init: bool = True,
    ) -> str:
        """Create a new repository under *org_name*.

        Calls ``POST /orgs/{org}/repos``.  By default sets ``auto_init``
        so the new repo has an initial commit and is immediately cloneable.

        Never raises — returns a success/error message string.
        """
        body: dict[str, Any] = {
            "name": repo_name,
            "auto_init": auto_init,
        }
        try:
            data = await self._post_json(f"/orgs/{org_name}/repos", body)
            html_url = data.get("html_url", "")
            return (
                f"Repository '{org_name}/{repo_name}' created successfully.\n"
                f"URL: {html_url}"
            )
        except RuntimeError as exc:
            return f"Error creating repo: {exc}"
        except Exception as exc:
            return f"Error creating repo: {exc}"

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
        allowed = await self.list_installation_repos()
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

    async def _fetch_ticket_field(
        self, ticket_id: str, label_suffix: str, field: str | None = None
    ) -> Any:
        """Fetch a ticket from the board API and return *field* or the full dict.

        When *field* is provided (e.g. ``"state"``), returns ``data.get(field)``;
        when ``None``, returns the full parsed JSON dict.  Returns ``None`` on
        any error (logged as a warning).
        """
        board_url = self._s.board_api_base_url.rstrip("/")
        url = f"{board_url}/tickets/{ticket_id}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._s.board_api_token.get_secret_value():
            headers["Authorization"] = (
                f"Bearer {self._s.board_api_token.get_secret_value()}"
            )
        label = f"Board API (ticket {label_suffix})"
        result = await safe_http_request(
            "GET", url, headers=headers, timeout=self._s.timeout, label=label
        )
        if result.error:
            logger.warning(
                "Failed to fetch ticket %s %s: %s",
                ticket_id,
                label_suffix,
                result.error,
            )
            return None
        try:
            data = json.loads(result.text or "")
        except json.JSONDecodeError, TypeError:
            logger.warning(
                "Non-JSON response for ticket %s: %s",
                ticket_id,
                (result.text or "")[:200],
            )
            return None
        if field is not None:
            return data.get(field)
        return data

    async def get_ticket_state(self, ticket_id: str) -> str | None:
        """Return the ticket's state (e.g. ``"BLOCKED"``), or ``None`` on failure.

        Calls the board API directly — the same endpoint the browser UI uses.
        """
        return cast(
            "str | None",
            await self._fetch_ticket_field(ticket_id, "state", field="state"),
        )

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

    async def delete_ticket_artifact(self, ticket_id: str, artifact_path: str) -> bool:
        """Delete an artifact on the board API for *ticket_id*.

        Sends ``DELETE /tickets/{ticket_id}/artifacts/{artifact_path}``.
        Returns ``True`` on success (HTTP 2xx), ``False`` on any error
        (logged as a warning).
        """
        board_url = self._s.board_api_base_url.rstrip("/")
        url = f"{board_url}/tickets/{ticket_id}/artifacts/{artifact_path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._s.board_api_token.get_secret_value():
            headers["Authorization"] = (
                f"Bearer {self._s.board_api_token.get_secret_value()}"
            )
        result = await safe_http_request(
            "DELETE",
            url,
            headers=headers,
            timeout=self._s.timeout,
            label=f"Board API (artifact {artifact_path})",
        )
        if result.error:
            logger.warning(
                "Failed to delete artifact %s for ticket %s: %s",
                artifact_path,
                ticket_id,
                result.error,
            )
            return False
        if result.status_code and result.status_code >= 400:
            logger.warning(
                "Board API returned %d for DELETE artifact %s on ticket %s",
                result.status_code,
                artifact_path,
                ticket_id,
            )
            return False
        return True

    async def get_ticket_data(self, ticket_id: str) -> dict[str, Any] | None:
        """Return the full ticket JSON from the board API, or None on failure.

        Calls ``GET /tickets/{ticket_id}`` on the board API and returns the
        parsed JSON body.  The response includes ``state``, ``events`` (state
        transitions), and other ticket metadata.
        """
        return cast(
            "dict[str, Any] | None", await self._fetch_ticket_field(ticket_id, "data")
        )

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

    async def get_job_log(self, repo_full_name: str, job_id: int) -> str:
        """Fetch the plain-text log for a GitHub Actions job.

        Calls ``GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs`` which
        returns a 302 redirect to a signed URL containing the raw log text.
        The redirect is followed server-side so the caller receives the log
        content directly.

        Raises ``RuntimeError`` on any failure (auth, not-found, network).
        """
        path = f"/repos/{repo_full_name}/actions/jobs/{job_id}/logs"
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "GET",
            url,
            headers=await self._gh_headers(),
            timeout=self._s.timeout,
            follow_redirects=True,
            label="GitHub Actions log",
        )
        if result.error:
            raise RuntimeError(f"GitHub Actions log GET {path}: {result.error}")
        return result.text or ""

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

    # -- workflow run query helpers ----------------------------------------

    async def list_workflow_runs(
        self,
        repo_full_name: str,
        *,
        branch: str | None = None,
        per_page: int = 5,
    ) -> list[dict[str, Any]]:
        """Return recent workflow runs for *repo_full_name*.

        Calls ``GET /repos/{owner}/{repo}/actions/runs``.

        Args:
            repo_full_name: ``"owner/name"``.
            branch: Optional branch filter (``?branch=...``).
            per_page: Maximum runs to return (1-100, default 5).

        Returns:
            A list of workflow-run dicts (``id``, ``status``, ``conclusion``,
            ``head_branch``, ``event``, ``created_at``, …).  Returns an empty
            list on any error (callers receive no exceptions).

        """
        params: dict[str, str] = {"per_page": str(min(max(per_page, 1), 100))}
        if branch:
            params["branch"] = branch
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        try:
            data = await self._get_json(f"/repos/{repo_full_name}/actions/runs?{qs}")
            runs: list[dict[str, Any]] = data.get("workflow_runs", [])
            return runs
        except RuntimeError as exc:
            logger.warning(
                "Failed to list workflow runs for %s: %s", repo_full_name, exc
            )
            return []

    async def get_workflow_run_jobs(
        self,
        repo_full_name: str,
        run_id: int,
    ) -> list[dict[str, Any]]:
        """Return the jobs for a specific workflow run.

        Calls ``GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs``.

        Returns:
            A list of job dicts (``id``, ``status``, ``conclusion``, ``name``,
            …).  Returns an empty list on any error.

        """
        try:
            data = await self._get_json(
                f"/repos/{repo_full_name}/actions/runs/{run_id}/jobs"
            )
            jobs: list[dict[str, Any]] = data.get("jobs", [])
            return jobs
        except RuntimeError as exc:
            logger.warning(
                "Failed to get workflow run jobs for %s run %d: %s",
                repo_full_name,
                run_id,
                exc,
            )
            return []

    async def get_workflow_run_annotations(
        self,
        repo_full_name: str,
        run_id: int,
        *,
        max_check_runs: int = 20,
    ) -> str:
        """Fetch annotations for all check runs in a workflow run.

        Orchestrates three GitHub API calls:
        1. ``GET /repos/{owner}/{repo}/actions/runs/{run_id}`` — get the
           ``check_suite_id`` for the run.
        2. ``GET /repos/{owner}/{repo}/check-suites/{check_suite_id}/check-runs``
           — list check runs belonging to the suite.
        3. For each check run with ``annotations_count > 0``,
           ``GET /repos/{owner}/{repo}/check-runs/{check_run_id}/annotations``.

        Returns a formatted Markdown string listing all annotations grouped
        by check run, or a diagnostic message when no annotations are found.

        Args:
            repo_full_name: ``"owner/name"``.
            run_id: The workflow run id.
            max_check_runs: Maximum check runs to inspect (default 20).

        Returns:
            A Markdown-formatted string with annotations, or an error message.

        Never raises — returns an error string on any failure.

        """
        try:
            # 1. Get the workflow run to find the check_suite_id.
            run = await self._get_json(f"/repos/{repo_full_name}/actions/runs/{run_id}")
            check_suite_id = run.get("check_suite_id")
            if check_suite_id is None:
                return (
                    f"Workflow run {run_id} on {repo_full_name} has no "
                    f"associated check suite — annotations are not available."
                )

            # 2. List check runs for the check suite.
            suite_data = await self._get_json(
                f"/repos/{repo_full_name}/check-suites/{check_suite_id}"
                f"/check-runs?per_page={min(max_check_runs, 100)}"
                f"&filter=latest"
            )
            check_runs: list[dict[str, Any]] = suite_data.get("check_runs", [])

            if not check_runs:
                return (
                    f"Workflow run {run_id} on {repo_full_name} has no "
                    f"check runs in its check suite."
                )

            # 3. Fetch annotations for each check run that has any.
            all_annotations: list[dict[str, Any]] = []
            check_run_summaries: list[str] = []

            for cr in check_runs:
                cr_id = cr.get("id")
                cr_name = cr.get("name", str(cr_id))
                cr_conclusion = cr.get("conclusion", "?")
                ann_count = cr.get("annotations_count", 0)

                if ann_count == 0:
                    continue

                try:
                    annotations = await self._get_json(
                        f"/repos/{repo_full_name}/check-runs/{cr_id}/annotations"
                        f"?per_page=100"
                    )
                    if isinstance(annotations, list):
                        all_annotations.extend(annotations)
                        check_run_summaries.append(
                            f"{cr_name} (conclusion={cr_conclusion}, "
                            f"{len(annotations)} annotation(s))"
                        )
                except RuntimeError as exc:
                    logger.warning(
                        "Failed to fetch annotations for check run %d: %s",
                        cr_id,
                        exc,
                    )
                    continue

            if not all_annotations:
                return (
                    f"Workflow run {run_id} on {repo_full_name} has "
                    f"{len(check_runs)} check run(s) but none with annotations."
                )

            # 4. Format the output.
            lines: list[str] = [
                f"## Workflow run {run_id} annotations ({repo_full_name})",
                "",
                f"**{len(all_annotations)} annotation(s)** across "
                f"{len(check_run_summaries)} check run(s):",
                "",
            ]

            for summary in check_run_summaries:
                lines.append(f"- {summary}")

            lines.append("")
            lines.append("### Details")
            lines.append("")

            for i, ann in enumerate(all_annotations):
                level = ann.get("annotation_level", "?")
                path = ann.get("path", "")
                start_line = ann.get("start_line")
                end_line = ann.get("end_line")
                message = ann.get("message", "")
                title = ann.get("title", "")

                loc = path
                if start_line is not None:
                    loc += f":{start_line}"
                    if end_line is not None and end_line != start_line:
                        loc += f"-{end_line}"

                lines.append(
                    f"**{i + 1}.** `{level}` "
                    + (f"**{title}** — " if title else "")
                    + f"{message}"
                )
                if loc:
                    lines.append(f"  _Location: {loc}_")
                lines.append("")

            return "\n".join(lines)

        except RuntimeError as exc:
            return f"Error fetching workflow run annotations: {exc}"
        except Exception as exc:
            return f"Error fetching workflow run annotations: {exc}"

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

    @staticmethod
    def apply_patch(original: str, patch_text: str) -> str:
        """Apply a unified diff to *original* and return the patched content.

        Supports the standard unified diff format produced by ``diff -u``
        and ``git diff``::

            --- a/path
            +++ b/path
            @@ -start,count +start,count @@
             context
            -removed
            +added
             context

        Multiple hunks (multiple ``@@`` headers) are supported.

        Args:
            original: The original file content.
            patch_text: The unified diff to apply.

        Returns:
            The patched file content.

        Raises:
            ValueError: If a hunk cannot be applied (context mismatch).

        """
        import re

        orig_lines = original.splitlines(keepends=True)
        patch_lines = patch_text.splitlines(keepends=True)

        result = list(orig_lines)
        cumulative_offset = 0  # net lines added (positive) or removed (negative)

        idx = 0
        while idx < len(patch_lines):
            line = patch_lines[idx]

            # Skip file headers (--- / +++)
            if line.startswith("--- ") or line.startswith("+++ "):
                idx += 1
                continue

            # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
            m = re.match(
                r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@",
                line,
            )
            if not m:
                idx += 1
                continue

            old_start = int(m.group(1))
            idx += 1

            # Collect hunk body lines
            hunk_lines: list[str] = []
            while idx < len(patch_lines) and not patch_lines[idx].startswith("@@"):
                hunk_lines.append(patch_lines[idx])
                idx += 1

            # Apply the hunk
            orig_pos = old_start - 1 + cumulative_offset  # 0-indexed in *result*
            hunk_offset_add = 0
            hunk_offset_del = 0
            hj = 0
            while hj < len(hunk_lines):
                hl = hunk_lines[hj]
                if hl.startswith(" "):  # context line
                    if orig_pos >= len(result):
                        raise ValueError(
                            f"Hunk at line {old_start}: context line {hj + 1} "
                            f"exceeds file length ({len(result)} lines)."
                        )
                    actual = result[orig_pos].rstrip("\n")
                    expected = hl[1:].rstrip("\n")
                    if actual != expected:
                        raise ValueError(
                            f"Hunk at line {old_start}: context mismatch at "
                            f"file line {orig_pos + 1}. "
                            f"Expected: {expected!r}, got: {actual!r}"
                        )
                    orig_pos += 1
                    hj += 1
                elif hl.startswith("-"):  # remove line
                    if orig_pos >= len(result):
                        raise ValueError(
                            f"Hunk at line {old_start}: removal at line {hj + 1} "
                            f"exceeds file length ({len(result)} lines)."
                        )
                    actual = result[orig_pos].rstrip("\n")
                    expected = hl[1:].rstrip("\n")
                    if actual != expected:
                        raise ValueError(
                            f"Hunk at line {old_start}: removal mismatch at "
                            f"file line {orig_pos + 1}. "
                            f"Expected to remove: {expected!r}, got: {actual!r}"
                        )
                    del result[orig_pos]
                    hunk_offset_del += 1
                    # Don't advance orig_pos — line was removed
                    hj += 1
                elif hl.startswith("+"):  # add line
                    result.insert(orig_pos, hl[1:])
                    orig_pos += 1
                    hunk_offset_add += 1
                    hj += 1
                elif hl == "\n" or hl == "":
                    # Empty context line (no leading space)
                    if orig_pos < len(result):
                        actual = result[orig_pos]
                        if actual not in ("\n", ""):
                            raise ValueError(
                                f"Hunk at line {old_start}: expected empty "
                                f"context line, got: {actual!r}"
                            )
                    orig_pos += 1
                    hj += 1
                elif hl.startswith("\\"):  # "No newline at end of file" marker
                    hj += 1
                else:
                    # Unknown line — skip
                    hj += 1

            cumulative_offset += hunk_offset_add - hunk_offset_del

        return "".join(result)

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
            patched = self.apply_patch(original, patch_text)
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

    def _diagnose_billing_failure(
        self,
        runs: list[dict[str, Any]],
    ) -> str | None:
        """Inspect recent workflow runs for a private-repo billing failure.

        Heuristic: a run whose ``run_started_at`` is ``null`` (never started)
        strongly suggests the repo has no GitHub Actions billing enabled.

        Note: zero-job detection is NOT attempted here because the
        ``/actions/runs`` endpoint does not include per-job run data.
        That signature is handled by the per-run inspection path in
        ``check_workflow_run`` (via ``get_workflow_run_jobs``).

        Returns a human-readable diagnostic string, or ``None`` when the
        signature is not detected.
        """
        for run in runs:
            conclusion = str(run.get("conclusion", "")).lower()
            if conclusion != "failure":
                continue
            run_id = run.get("id")
            run_name = run.get("name", str(run_id))
            # Runs that never started signal billing issues.
            if "run_started_at" in run and not run.get("run_started_at"):
                return (
                    f"Workflow run '{run_name}' (id {run_id}) for "
                    f"{run.get('head_branch', '?')} never started — "
                    f"this is typical of a private repository with no "
                    f"GitHub Actions billing. "
                    f"Enable Actions in the repo's Settings > Actions > General, "
                    f"or add billing at the organisation level."
                )
        return None

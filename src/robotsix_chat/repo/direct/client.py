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
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

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


class DirectRepoClient:
    """GitHub App-authenticated client for core repo operations.

    Handles push-branch, open-PR, merge-PR, auto-merge, file-content
    retrieval, repo creation, installation-scope listing, and security
    settings.  Board/ticket API, GitHub Actions workflow, and unified-diff
    concerns have been extracted to dedicated modules.
    """

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
            self._invalidate_token()
            if "headers" in kwargs:
                kwargs["headers"] = await self._gh_headers()
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

    async def get_installation_token_diagnostics(
        self,
        repo_full_name: str,
    ) -> dict[str, Any]:
        """Mint a fresh installation token and return its expiry and scope.

        Mints a fresh installation token for *repo_full_name* and returns its
        expiry and permission scope for diagnosis.  Resolves the installation
        id from the repository (bypassing any cached token) so the returned
        ``permissions`` reflect the GitHub App's **current** grant — not a
        possibly-stale cached token.  This lets the agent distinguish "the
        token was cached/stale" from "the App genuinely lacks the
        permission" when a GitHub API call fails with a 403 permission error.

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

        try:
            result = await asyncio.to_thread(
                mint_installation_token,
                app_id=self._s.github_app_id,
                private_key=self._s.github_app_private_key.get_secret_value(),
                owner=owner,
                repo=repo,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to mint a fresh installation token for "
                f"'{repo_full_name}': {exc}"
            ) from exc

        return {
            "app_id": self._s.github_app_id,
            "configured_installation_id": self._s.github_app_installation_id,
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

    async def search_open_prs(
        self,
        *,
        org_name: str,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Return open PRs across *org_name*'s repositories via the Search API.

        Uses a single ``/search/issues`` batch query
        (``type:pr state:open org:<org_name>``) instead of one REST call per
        repository.  Paginates through the result pages (GitHub caps search
        results at 10 pages / 1000 items); results are limited to
        repositories the GitHub App installation can access.

        Raises RuntimeError on failure (callers catch and format).
        """
        query = quote(f"type:pr state:open org:{org_name}", safe="")
        all_items: list[dict[str, Any]] = []
        page = 1

        while page <= 10:
            data = await self._get_json(
                f"/search/issues?q={query}&per_page={per_page}&page={page}"
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

    # -- merge helpers -----------------------------------------------------

    async def merge_pr(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        merge_method: str = "squash",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> str:
        """Merge a pull request.

        Calls ``PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge``.

        Before attempting the merge the method fetches the PR to surface
        actionable diagnostics when the merge is blocked: draft state,
        merge conflicts, or failing/pending CI checks.  GitHub enforces
        the same preconditions server-side (returns 405/409), so the
        pre-flight check is a best-effort diagnostic layer.

        Args:
            repo_full_name: ``"owner/name"``.
            pr_number: The PR number to merge.
            merge_method: ``"squash"`` (default), ``"merge"``, or ``"rebase"``.
            commit_title: Optional title for the merge commit (squash/merge only).
            commit_message: Optional body for the merge commit (squash/merge only).

        Returns:
            A success message with the merge commit SHA, or an error message.

        Never raises — returns an error string on any failure.

        """
        try:
            # --- pre-flight: fetch PR to diagnose blockers ---
            pr = await self.get_pr(repo_full_name=repo_full_name, pr_number=pr_number)
        except RuntimeError as exc:
            return f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}"

        # Draft check
        if pr.get("draft"):
            return (
                f"Cannot merge PR #{pr_number} in {repo_full_name}: "
                f"the PR is still in draft state.  Mark it as ready for "
                f"review before merging."
            )

        # Mergeability check (GitHub computes this asynchronously)
        mergeable = pr.get("mergeable")
        mergeable_state = pr.get("mergeable_state", "unknown")
        if mergeable is False:
            return (
                f"Cannot merge PR #{pr_number} in {repo_full_name}: "
                f"merge conflicts detected (mergeable_state={mergeable_state}). "
                f"Resolve conflicts or rebase the branch before merging."
            )
        if mergeable is None:
            return (
                f"Cannot merge PR #{pr_number} in {repo_full_name} yet: "
                f"mergeability is still being computed by GitHub. "
                f"Wait a few seconds and try again."
            )

        # Already merged?
        if pr.get("merged"):
            merge_sha = pr.get("merge_commit_sha", "(unknown)")
            return (
                f"PR #{pr_number} in {repo_full_name} is already merged "
                f"(merge commit: {merge_sha})."
            )

        # --- attempt the merge ---
        body: dict[str, Any] = {"merge_method": merge_method}
        if commit_title is not None:
            body["commit_title"] = commit_title
        if commit_message is not None:
            body["commit_message"] = commit_message

        try:
            result = await self._request_json(
                "PUT",
                f"/repos/{repo_full_name}/pulls/{pr_number}/merge",
                body,
            )
        except RuntimeError as exc:
            msg = str(exc)
            # GitHub returns 405 when the PR is not mergeable (e.g. CI
            # checks pending/failing, required reviews missing).
            if "405" in msg:
                return (
                    f"Cannot merge PR #{pr_number} in {repo_full_name}: "
                    f"the PR is not in a mergeable state.  Common causes: "
                    f"required status checks are pending or failing, "
                    f"required reviews are missing, or branch protection "
                    f"rules are not satisfied.  "
                    f"GitHub response: {msg}"
                )
            if "409" in msg:
                return (
                    f"Cannot merge PR #{pr_number} in {repo_full_name}: "
                    f"merge conflict or SHA mismatch.  "
                    f"GitHub response: {msg}"
                )
            return f"Error merging PR #{pr_number} in {repo_full_name}: {msg}"

        merged = result.get("merged", False)
        sha = result.get("sha", "(unknown)")
        message = result.get("message", "")

        if merged:
            return (
                f"PR #{pr_number} in {repo_full_name} merged successfully "
                f"using {merge_method}.\n"
                f"Merge commit SHA: {sha}"
            )
        # GitHub returned 200 but merged=False — surface the message
        return (
            f"PR #{pr_number} in {repo_full_name} was not merged: "
            f"{message or 'unknown reason'} (SHA: {sha})"
        )

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

"""Direct-repository-capability tools for the chat agent.

Exposes :func:`build_direct_repo_tools` — a factory returning the LLM tools
that let the agent push branches and open PRs against repositories in the
robotsix-mill GitHub App's installation scope, authenticating as the app.
Returns no tools when the direct-repo capability is disabled.

**Guardrails enforced by the tools:**
- Actions are ONLY permitted for tickets currently in BLOCKED state.
- The repo set is resolved DYNAMICALLY from the GitHub App installation
  (list-installation-repositories) — no static allowlist.
- PRs are opened in a reviewable state with no auto-merge; the merge gate
  stays human.
- No merge capability exists on this path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

__all__ = ["build_direct_repo_tools"]


def build_direct_repo_tools(
    settings: DirectRepoSettings,
) -> list[Callable[..., Any]]:
    """Return direct-repo tool(s) for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    from .client import DirectRepoClient

    client = DirectRepoClient(settings)

    async def _assert_blocked_and_scoped(
        client: DirectRepoClient,
        ticket_id: str,
        repo_full_name: str,
    ) -> str | None:
        """Return an error string if preconditions fail, or None if OK."""
        state = await client.get_ticket_state(ticket_id)
        if state is None:
            return (
                f"Error: could not determine state for ticket {ticket_id}. "
                "Verify the ticket id and board API connectivity."
            )
        if state.upper() != "BLOCKED":
            return (
                f"Refused: ticket {ticket_id} is in state '{state}', not BLOCKED. "
                "Direct-repo actions are only permitted for BLOCKED tickets."
            )

        allowed = await client.list_installation_repos()
        if repo_full_name not in allowed:
            return (
                f"Refused: repo '{repo_full_name}' is not in the GitHub App "
                f"installation scope. Allowed repos: {', '.join(sorted(allowed))}"
            )
        return None

    async def push_direct_repo_branch(
        ticket_id: str,
        repo_full_name: str,
        branch_name: str,
        files_json: str,
        commit_message: str = "",
    ) -> str:
        """Push a new branch with file changes to a GitHub repository.

        Creates a new branch, writes the given files, and pushes them in a
        single commit.  The branch is created from the repository's default
        branch.

        **Precondition:** The ticket identified by *ticket_id* MUST be in
        BLOCKED state.  This tool will verify that and refuse otherwise.

        **Scope:** The *repo_full_name* must be within the robotsix-mill
        GitHub App's current installation scope (checked dynamically at
        call time).

        Args:
            ticket_id: The blocked ticket this branch addresses (e.g.
                ``"20250624T020652Z-my-ticket-a1b2"``).  Used to verify
                BLOCKED state and for traceability in the commit/PR.
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            branch_name: Name for the new branch (e.g.
                ``"fix/20250624T020652Z-my-ticket-a1b2"``).
            files_json: JSON array of ``{"path": "...", "content": "..."}``
                objects describing the files to create or overwrite.
                Paths are relative to the repo root.
            commit_message: Commit message.  Defaults to a message that
                references the *ticket_id*.

        Returns:
            A status message with the branch URL on success, or an error
            message describing why the push was refused or failed.

        """
        import json

        try:
            files: list[dict[str, str]] = json.loads(files_json)
        except json.JSONDecodeError, TypeError:
            return (
                "Error: files_json must be a valid JSON array "
                "of {path, content} objects."
            )

        if not isinstance(files, list):
            return "Error: files_json must be a JSON array."

        if error := await _assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        # --- ensure changelog fragments end with a newline ---
        for f in files:
            if (
                f.get("path", "").startswith("changelog.d/")
                and f["path"].endswith(".md")
                and not f.get("content", "").endswith("\n")
            ):
                f["content"] = f["content"] + "\n"

        # --- push the branch ---
        msg = commit_message or f"fix: address blocked ticket {ticket_id}"
        return await client.push_branch(
            repo_full_name=repo_full_name,
            branch_name=branch_name,
            files=files,
            commit_message=msg,
            ticket_id=ticket_id,
        )

    async def open_direct_repo_pr(
        ticket_id: str,
        repo_full_name: str,
        branch_name: str,
        title: str,
        body: str = "",
    ) -> str:
        """Open a pull request from a branch.

        Opens a standard PR (not draft) in a reviewable state.  Auto-merge
        is NOT requested or enabled — the merge gate stays human.

        **Precondition:** The ticket identified by *ticket_id* MUST be in
        BLOCKED state.  This tool will verify that and refuse otherwise.

        **Scope:** The *repo_full_name* must be within the robotsix-mill
        GitHub App's current installation scope (checked dynamically at
        call time).

        Args:
            ticket_id: The blocked ticket this PR addresses.
            repo_full_name: GitHub ``owner/name``.
            branch_name: The head branch to merge from (must already exist).
            title: PR title. Should reference the ticket id for traceability.
            body: PR description.  Defaults to a message linking back to the
                originating ticket.

        Returns:
            A status message with the PR URL on success, or an error message.

        """
        if error := await _assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        pr_body = body or (
            f"PR opened by robotsix-chat agent to resolve blocked ticket "
            f"`{ticket_id}`.\n\n"
            f"**Auto-merge is disabled** — human review required before merge."
        )
        return await client.create_pr(
            repo_full_name=repo_full_name,
            head_branch=branch_name,
            title=title,
            body=pr_body,
        )

    async def update_pr_branch(
        ticket_id: str,
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Attempt to rebase a PR branch onto the latest base branch.

        Calls GitHub's update-branch API, which tries to rebase the PR's head
        branch onto the current tip of the base branch.  If the rebase succeeds,
        the PR is updated.  If merge conflicts are detected, the tool returns
        the conflict details so the agent can decide next steps.

        **Precondition:** The ticket identified by *ticket_id* MUST be in
        BLOCKED state.  This tool will verify that and refuse otherwise.

        **Scope:** The *repo_full_name* must be within the robotsix-mill
        GitHub App's current installation scope (checked dynamically at
        call time).

        Args:
            ticket_id: The blocked ticket the PR belongs to (e.g.
                ``"20250624T020652Z-my-ticket-a1b2"``).
            repo_full_name: GitHub ``owner/name``.
            pr_number: The PR number to update.

        Returns:
            A status message — success with a note that the update is queued,
            or an error describing merge conflicts or other failures.

        """
        if error := await _assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        return await client.update_pr_branch(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )

    async def check_pr_merge_conflict(
        ticket_id: str,
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Check whether a PR has merge conflicts.

        Fetches the PR's mergeability status from GitHub and returns a
        human-readable summary including the mergeable state and, when
        available, the specific conflict reason.

        **Precondition:** The ticket identified by *ticket_id* MUST be in
        BLOCKED state.  This tool will verify that and refuse otherwise.

        **Scope:** The *repo_full_name* must be within the robotsix-mill
        GitHub App's current installation scope (checked dynamically at
        call time).

        Args:
            ticket_id: The blocked ticket the PR belongs to.
            repo_full_name: GitHub ``owner/name``.
            pr_number: The PR number to inspect.

        Returns:
            A status message with mergeability details, or an error message.

        """
        if error := await _assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        try:
            pr = await client.get_pr(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
            )
        except Exception as exc:
            return f"Error fetching PR #{pr_number}: {exc}"

        mergeable = pr.get("mergeable")
        mergeable_state = pr.get("mergeable_state", "unknown")
        title = pr.get("title", "(no title)")
        html_url = pr.get("html_url", "")

        lines = [
            f"PR #{pr_number} in {repo_full_name}: {title}",
            f"URL: {html_url}",
            f"Mergeable state: {mergeable_state}",
        ]

        if mergeable is None:
            lines.append(
                "Mergeability is still being computed by GitHub — "
                "try again in a few seconds."
            )
        elif mergeable is True:
            lines.append("No merge conflicts detected — PR is mergeable.")
        elif mergeable is False:
            lines.append(
                "Merge conflicts detected — the PR cannot be merged as-is. "
                "Consider rebasing the branch or resolving conflicts manually."
            )

        # Include additional fields that may carry useful conflict info
        for field in ("merged", "merged_at", "merge_commit_sha", "draft"):
            val = pr.get(field)
            if val is not None:
                lines.append(f"{field}: {val}")

        return "\n".join(lines)

    return [
        push_direct_repo_branch,
        open_direct_repo_pr,
        update_pr_branch,
        check_pr_merge_conflict,
    ]

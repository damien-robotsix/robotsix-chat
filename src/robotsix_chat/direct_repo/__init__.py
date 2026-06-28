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
        except (json.JSONDecodeError, TypeError):
            return (
                "Error: files_json must be a valid JSON array "
                "of {path, content} objects."
            )

        if not isinstance(files, list):
            return "Error: files_json must be a JSON array."

        if error := await _assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

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

    return [push_direct_repo_branch, open_direct_repo_pr]

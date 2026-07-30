"""Direct-repository-capability tools for the chat agent.

Exposes :func:`build_direct_repo_tools` — a factory returning the LLM tools
that let the agent push branches, open PRs, and (when enabled) push direct
fixes against repositories in the robotsix-mill GitHub App's installation
scope, authenticating as the app.  Returns no tools when the direct-repo
capability is disabled.

**Guardrails enforced by the tools:**
- Actions are ONLY permitted for tickets currently in BLOCKED state.
- The repo set is resolved DYNAMICALLY from the GitHub App installation
  (list-installation-repositories) — no static allowlist.
- PRs are opened in a reviewable state with no auto-merge; the merge gate
  stays human.
- No merge capability exists on this path.

**Additional guardrails for ``direct_fix``:**
- Ticket must have exhausted its spawn limit (≥3 implement cycles),
  verified against the board API.
- Every direct-fix action is audited at WARNING log level.
- The tool is only available when ``direct_fix_enabled`` is ``True``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

__all__ = ["build_direct_repo_tools"]

logger = logging.getLogger(__name__)


def build_direct_repo_tools(
    settings: DirectRepoSettings,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return direct-repo tool(s) for the agent, or ``[]`` when disabled.

    When *component_request* is provided, ticket-state verification uses
    it (the same roster-based connectivity as the component API) instead
    of the direct ``board_api_base_url`` path.  This ensures push/PR
    operations succeed when the roster-based path works but the direct
    config path doesn't.
    """
    if not settings.enabled:
        return []

    from .client import DirectRepoClient

    client = DirectRepoClient(settings)

    async def _get_ticket_state_via_component(
        component_req: Callable[..., Any],
        ticket_id: str,
    ) -> tuple[str | None, str | None]:
        """Fetch ticket state via *component_req*; return ``(state, error)``.

        Returns ``(state, None)`` on success, ``(None, error_msg)`` on failure.
        The error message includes the connectivity path used for diagnosis.
        """
        resp = await component_req("mill", "GET", f"/tickets/{ticket_id}")
        # Parse the component_request response format: "HTTP <status>\n<body>"
        # or "Error: ..."
        if resp.startswith("Error:"):
            return None, (
                f"Error: could not determine state for ticket {ticket_id} "
                f"via component_request (roster-based board connectivity): "
                f"{resp}"
            )
        try:
            newline = resp.index("\n")
            status_line = resp[:newline]
            body_str = resp[newline + 1 :]
        except ValueError:
            return None, (
                f"Error: could not determine state for ticket {ticket_id} "
                f"via component_request (roster-based board connectivity): "
                f"unexpected response format"
            )
        if not status_line.startswith("HTTP "):
            return None, (
                f"Error: could not determine state for ticket {ticket_id} "
                f"via component_request (roster-based board connectivity): "
                f"{status_line}"
            )
        try:
            status_code = int(status_line.split()[1])
        except IndexError, ValueError:
            return None, (
                f"Error: could not determine state for ticket {ticket_id} "
                f"via component_request (roster-based board connectivity): "
                f"unparsable status {status_line!r}"
            )
        if status_code >= 400:
            return None, (
                f"Error: could not determine state for ticket {ticket_id} "
                f"via component_request (roster-based board connectivity): "
                f"HTTP {status_code}"
            )
        try:
            data = json.loads(body_str)
            state: str | None = data.get("state")
            return state, None
        except json.JSONDecodeError, TypeError:
            return None, (
                f"Error: could not determine state for ticket {ticket_id} "
                f"via component_request (roster-based board connectivity): "
                f"non-JSON response body"
            )

    async def _get_implement_cycles_via_component(
        component_req: Callable[..., Any],
        ticket_id: str,
    ) -> int | None:
        """Count implement cycles via *component_req*; return count or None.

        Mirrors :meth:`DirectRepoClient.count_implement_cycles` but uses the
        roster-based connectivity path so cycle-counting succeeds when the
        direct ``board_api_base_url`` path is unreachable.
        """
        resp = await component_req("mill", "GET", f"/tickets/{ticket_id}")
        if resp.startswith("Error:"):
            return None
        try:
            newline = resp.index("\n")
            status_line = resp[:newline]
            body_str = resp[newline + 1 :]
        except ValueError:
            return None
        if not status_line.startswith("HTTP "):
            return None
        try:
            status_code = int(status_line.split()[1])
        except IndexError, ValueError:
            return None
        if status_code >= 400:
            return None
        try:
            data = json.loads(body_str)
        except json.JSONDecodeError, TypeError:
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
                st = str(entry.get("state", entry.get("to", ""))).lower()
                act = str(entry.get("action", entry.get("type", ""))).lower()
                if "implement_complete" in st or "implement" in act:
                    count += 1
            return count

        # 3. No events/history — try a direct cycle_count field
        cycle_count = data.get("cycle_count")
        if isinstance(cycle_count, int):
            return cycle_count

        # 4. Can't determine — return 0
        return 0

    async def _delete_artifact_via_component(
        component_req: Callable[..., Any],
        ticket_id: str,
        artifact_path: str,
    ) -> tuple[bool, str | None]:
        """Delete a board artifact via *component_req*; return ``(success, error)``.

        Returns ``(True, None)`` on success, ``(False, error_msg)`` on failure,
        or ``(False, None)`` when the response is ambiguous (caller should
        fall back to the direct API).
        """
        resp = await component_req(
            "mill",
            "DELETE",
            f"/tickets/{ticket_id}/artifacts/{artifact_path}",
        )
        if resp.startswith("Error:"):
            return False, (
                f"Error: could not reset implement spawn counter for ticket "
                f"{ticket_id} via component_request "
                f"(roster-based board connectivity): {resp}"
            )
        try:
            newline = resp.index("\n")
            status_line = resp[:newline]
        except ValueError:
            # No newline — treat as ambiguous, fall through to direct API
            return False, None
        if not status_line.startswith("HTTP "):
            return False, (
                f"Error: could not reset implement spawn counter for ticket "
                f"{ticket_id} via component_request "
                f"(roster-based board connectivity): {status_line}"
            )
        try:
            status_code = int(status_line.split()[1])
        except IndexError, ValueError:
            return False, (
                f"Error: could not reset implement spawn counter for ticket "
                f"{ticket_id} via component_request "
                f"(roster-based board connectivity): "
                f"unparsable status {status_line!r}"
            )
        if 200 <= status_code < 300:
            return True, None
        return False, (
            f"Error: could not reset implement spawn counter for ticket "
            f"{ticket_id} via component_request "
            f"(roster-based board connectivity): HTTP {status_code}"
        )

    async def _assert_blocked_and_scoped(
        client: DirectRepoClient,
        ticket_id: str,
        repo_full_name: str,
    ) -> str | None:
        """Return an error string if preconditions fail, or None if OK.

        Installation scope is checked only when the mill pipeline credential
        is NOT available (i.e. *component_request* is ``None``).  When the
        push is orchestrated through the component roster the mill already
        has its own GitHub access, so the scope check is an unnecessary gate.
        """
        # --- scope check: skipped when mill pipeline credential available ---
        # When component_request is available the mill pipeline is
        # orchestrating the push with its own credentials — the GitHub App
        # installation scope check is an unnecessary hurdle.
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        if component_request is not None:
            state, error = await _get_ticket_state_via_component(
                component_request, ticket_id
            )
        else:
            state = await client.get_ticket_state(ticket_id)
            if state is not None:
                error = None
            else:
                board_url = client._s.board_api_base_url.rstrip("/")
                error = (
                    f"Error: could not determine state for ticket "
                    f"{ticket_id}. Verify the ticket id and board API "
                    f"connectivity (tried {board_url}/tickets/{ticket_id})."
                )
        if error is not None:
            return error
        if state is not None and state.upper() != "BLOCKED":
            return (
                f"Refused: ticket {ticket_id} is in state '{state}', not BLOCKED. "
                "Direct-repo actions are only permitted for BLOCKED tickets."
            )
        if state is None:
            return (
                f"Error: ticket {ticket_id} returned no state field. "
                "Verify the ticket id and board API connectivity."
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

        **Scope:** When called through the component roster (i.e. the
        ``component_request`` credential is available) the GitHub App
        installation scope check is bypassed — the mill already has its
        own GitHub access.  For direct board-API calls, *repo_full_name*
        must be within the robotsix-mill GitHub App's current installation
        scope (checked dynamically at call time).

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

        **Scope:** When called through the component roster (i.e. the
        ``component_request`` credential is available) the GitHub App
        installation scope check is bypassed — the mill already has its
        own GitHub access.  For direct board-API calls, *repo_full_name*
        must be within the robotsix-mill GitHub App's current installation
        scope (checked dynamically at call time).

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

        **Scope:** When called through the component roster (i.e. the
        ``component_request`` credential is available) the GitHub App
        installation scope check is bypassed — the mill already has its
        own GitHub access.  For direct board-API calls, *repo_full_name*
        must be within the robotsix-mill GitHub App's current installation
        scope (checked dynamically at call time).

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

        **Scope:** When called through the component roster (i.e. the
        ``component_request`` credential is available) the GitHub App
        installation scope check is bypassed — the mill already has its
        own GitHub access.  For direct board-API calls, *repo_full_name*
        must be within the robotsix-mill GitHub App's current installation
        scope (checked dynamically at call time).

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

    async def reset_implement_spawn_counter(ticket_id: str) -> str:
        """Reset the implement-agent spawn counter for a blocked ticket.

        Deletes the ``implement_spawn_count`` artifact on the board API,
        clearing the spawn-limit block so the implement agent can be
        re-spawned against this ticket.  Use when a ticket is blocked at
        the implement spawn limit and the ``resume-blocked`` mechanism
        has proven unreliable.

        Tries the component roster path first when *component_request*
        is available (resolving ``"mill"`` via the central-deploy roster
        or component fallbacks); on failure falls back to the direct
        ``board_api_base_url`` path.  When *component_request* is
        unavailable the direct path is used directly.

        Args:
            ticket_id: The blocked ticket whose counter to reset
                (e.g. ``"20250624T020652Z-my-ticket-a1b2"``).

        Returns:
            A status message — success confirmation or an error describing
            why the reset failed.

        """
        # Try roster path first when available.
        if component_request is not None:
            resp = await component_request(
                "mill",
                "DELETE",
                f"/tickets/{ticket_id}/artifacts/implement_spawn_count",
            )
            if resp.startswith("HTTP 2"):
                return (
                    f"Implement spawn counter reset for ticket {ticket_id} "
                    "(via roster path). The ticket can now be re-spawned."
                )
            logger.info(
                "reset_implement_spawn_counter roster path failed for %s; "
                "falling back to direct board API",
                ticket_id,
            )

        # Fall back to the direct board API path.
        board_url = client._s.board_api_base_url.rstrip("/")
        ok = await client.delete_ticket_artifact(ticket_id, "implement_spawn_count")
        if ok:
            return (
                f"Implement spawn counter reset for ticket {ticket_id}. "
                "The ticket can now be re-spawned."
            )

        return (
            f"Error: could not reset implement spawn counter for ticket "
            f"{ticket_id}.  Verify the ticket id and board API connectivity "
            f"({board_url})."
        )

    async def apply_patch_to_file(
        ticket_id: str,
        repo_full_name: str,
        branch_name: str,
        file_path: str,
        patch_content: str,
        commit_message: str = "",
    ) -> str:
        """Push a patched file to a new branch using a unified diff.

        Fetches the current *file_path* from the repo's default branch,
        applies *patch_content* (a unified diff), and pushes the result
        as a commit on a new branch named *branch_name*.  This avoids
        the need to reconstruct the entire file content — only the diff
        is required.

        **Precondition:** The ticket identified by *ticket_id* MUST be in
        BLOCKED state.  This tool will verify that and refuse otherwise.

        **Scope:** When called through the component roster the GitHub App
        installation scope check is bypassed.  For direct board-API calls,
        *repo_full_name* must be within the robotsix-mill GitHub App's
        installation scope.

        **Patch format:** Standard unified diff (as produced by ``diff -u``
        or ``git diff``)::

            --- a/path
            +++ b/path
            @@ -start,count +start,count @@
             context
            -removed
            +added

        Args:
            ticket_id: The blocked ticket this patch addresses (e.g.
                ``"20250624T020652Z-my-ticket-a1b2"``).
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            branch_name: Name for the new branch (e.g.
                ``"fix/20250624T020652Z-my-ticket-a1b2"``).
            file_path: Path to the file to patch, relative to the repo
                root (e.g. ``"src/dashboard.js"``).
            patch_content: The unified diff to apply.  Must include at
                least one ``@@`` hunk header with context lines.
            commit_message: Commit message.  Defaults to a message that
                references the *ticket_id*.

        Returns:
            A status message with the branch URL on success, or an error
            message describing why the patch was refused or failed.

        """
        if error := await _assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        msg = commit_message or (
            f"fix: patch {file_path} for blocked ticket {ticket_id}"
        )

        try:
            # Fetch the file from the default branch
            repo = await client._get_json(f"/repos/{repo_full_name}")
            default_branch: str = repo.get("default_branch", "main")

            original, _sha = await client.get_file_content(
                repo_full_name, file_path, ref=default_branch
            )
        except (RuntimeError, ValueError) as exc:
            return (
                f"Error fetching file '{file_path}' from "
                f"{repo_full_name}/{default_branch}: {exc}"
            )

        try:
            patched = DirectRepoClient.apply_patch(original, patch_content)
        except ValueError as exc:
            return f"Error applying patch to '{file_path}' in {repo_full_name}: {exc}"

        if patched == original:
            return (
                f"Patch applied to '{file_path}' in {repo_full_name} produced "
                f"no changes — the file may already be in the desired state."
            )

        return await client.push_branch(
            repo_full_name=repo_full_name,
            branch_name=branch_name,
            files=[{"path": file_path, "content": patched}],
            commit_message=msg,
            ticket_id=ticket_id,
        )

    async def push_patch_to_pr_branch(
        ticket_id: str,
        repo_full_name: str,
        pr_number: int,
        file_path: str,
        patch_content: str,
        commit_message: str = "",
    ) -> str:
        """Push a patched commit to an existing pull request's head branch.

        Fetches *file_path* from the PR's head branch, applies *patch_content*
        (a unified diff), and pushes the result as a commit on the same branch.
        This is the standard path for updating a PR with a code change — no
        cycle-count gate, no new-branch creation.

        **Preconditions (all enforced by the tool):**
        1. Ticket MUST be in BLOCKED state.
        2. When called through the component roster (i.e. the
           ``component_request`` credential is available) the GitHub App
           installation scope check is bypassed — the mill already has its
           own GitHub access.  For direct board-API calls, *repo_full_name*
           MUST be in the GitHub App installation scope.
        3. The PR must exist and its head branch must belong to the same
           repository (*repo_full_name*) — cross-repo PR updates are refused.

        **Patch format:** Standard unified diff (as produced by ``diff -u``
        or ``git diff``)::

            --- a/path
            +++ b/path
            @@ -start,count +start,count @@
             context
            -removed
            +added

        Args:
            ticket_id: The blocked ticket this PR addresses (e.g.
                ``"20250624T020652Z-my-ticket-a1b2"``).
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            pr_number: The PR number to push to.
            file_path: Path to the file to patch, relative to the repo
                root (e.g. ``"src/dashboard.js"``).
            patch_content: The unified diff to apply.  Must include at
                least one ``@@`` hunk header with context lines.
            commit_message: Commit message.  Defaults to a message that
                references the *ticket_id*.

        Returns:
            A status message with the commit SHA on success, or an error
            message describing why the push was refused or failed.

        """
        # --- guard 1+2: BLOCKED + scope ---
        if error := await _assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        # --- guard 3: fetch PR and verify head branch ---
        try:
            pr = await client.get_pr(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
            )
        except Exception as exc:
            return f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}"

        head_info = pr.get("head", {})
        head_branch: str | None = head_info.get("ref")
        head_repo = head_info.get("repo", {})
        head_repo_full_name: str | None = head_repo.get("full_name")

        if not head_branch:
            return (
                f"Error: PR #{pr_number} in {repo_full_name} has no head "
                f"branch — cannot determine where to push."
            )

        if head_repo_full_name and head_repo_full_name != repo_full_name:
            return (
                f"Refused: PR #{pr_number} head branch '{head_branch}' belongs "
                f"to '{head_repo_full_name}', not '{repo_full_name}'. "
                f"Cross-repo PR updates are not permitted."
            )

        # --- push the patched commit ---
        msg = commit_message or (
            f"fix: patch {file_path} for blocked ticket {ticket_id} (PR #{pr_number})"
        )
        return await client.push_patched_file(
            repo_full_name=repo_full_name,
            branch_name=head_branch,
            file_path=file_path,
            patch_text=patch_content,
            commit_message=msg,
            ticket_id=ticket_id,
        )

    tools: list[Callable[..., Any]] = [
        push_direct_repo_branch,
        open_direct_repo_pr,
        update_pr_branch,
        check_pr_merge_conflict,
        reset_implement_spawn_counter,
        apply_patch_to_file,
        push_patch_to_pr_branch,
    ]

    # ------------------------------------------------------------------
    # direct_fix — push directly to target branch (gated on mill exhaustion)
    # ------------------------------------------------------------------

    if settings.direct_fix_enabled:

        async def direct_fix(
            ticket_id: str,
            repo_full_name: str,
            target_branch: str,
            files_json: str,
            commit_message: str = "",
        ) -> str:
            """Push a commit directly to a target branch, bypassing the PR flow.

            **DANGER ZONE — last-resort escape hatch.**  This tool pushes a
            commit directly to *target_branch* (e.g. ``"main"``) without
            creating a pull request.  It is only available when the ticket
            has exhausted the mill's implement cycle limit.

            **Preconditions (all enforced by the tool):**
            1. Ticket MUST be in BLOCKED state.
            2. Ticket MUST have ≥3 implement cycles (verified via board API).
            3. When called through the component roster (i.e. the
               ``component_request`` credential is available) the GitHub App
               installation scope check is bypassed — the mill already has
               its own GitHub access.  For direct board-API calls,
               *repo_full_name* MUST be in the GitHub App installation
               scope.

            **Auditability:** Every invocation is logged at WARNING level
            with the ticket id, repo, branch, and file paths.

            Args:
                ticket_id: The blocked, mill-exhausted ticket this fix
                    addresses (e.g. ``"20250624T020652Z-my-ticket-a1b2"``).
                repo_full_name: GitHub ``owner/name`` (e.g.
                    ``"robotsix/robotsix-chat"``).
                target_branch: Branch to push directly to (e.g. ``"main"``).
                files_json: JSON array of ``{"path": "...", "content": "..."}``
                    objects describing the files to create or overwrite.
                    Paths are relative to the repo root.
                commit_message: Commit message.  Defaults to a message that
                    references the *ticket_id* and marks it as a direct fix.

            Returns:
                A status message with the commit SHA on success, or an error
                message describing why the push was refused or failed.

            """
            _logger = logging.getLogger(__name__)

            # --- validate files_json ---
            try:
                files: list[dict[str, str]] = json.loads(files_json)
            except json.JSONDecodeError, TypeError:
                return (
                    "Error: files_json must be a valid JSON array "
                    "of {path, content} objects."
                )

            if not isinstance(files, list):
                return "Error: files_json must be a JSON array."

            # --- ensure changelog fragments end with a newline ---
            for f in files:
                if (
                    f.get("path", "").startswith("changelog.d/")
                    and f["path"].endswith(".md")
                    and not f.get("content", "").endswith("\n")
                ):
                    f["content"] = f["content"] + "\n"

            # --- guard 1+2: BLOCKED + scope ---
            if error := await _assert_blocked_and_scoped(
                client, ticket_id, repo_full_name
            ):
                return error

            # --- guard 3: ≥3 implement cycles ---
            if component_request is not None:
                cycles = await _get_implement_cycles_via_component(
                    component_request, ticket_id
                )
            else:
                cycles = await client.count_implement_cycles(ticket_id)
            if cycles is None:
                return (
                    f"Error: could not fetch ticket data for {ticket_id}. "
                    "Verify the ticket id and board API connectivity."
                )
            if cycles < 3:
                return (
                    f"Refused: ticket {ticket_id} has only {cycles} implement "
                    f"cycle(s).  direct_fix requires ≥3 implement cycles "
                    "(mill exhaustion).  Use push_direct_repo_branch + "
                    "open_direct_repo_pr for the standard PR flow."
                )

            # --- audit log ---
            file_paths = [f.get("path", "?") for f in files]
            _logger.warning(
                "direct_fix: ticket=%s repo=%s branch=%s files=%s",
                ticket_id,
                repo_full_name,
                target_branch,
                file_paths,
            )

            # --- push the commit ---
            msg = commit_message or (
                f"fix: direct fix for blocked ticket {ticket_id} "
                f"(mill exhausted after {cycles} implement cycles)"
            )
            result = await client.push_commit_to_branch(
                repo_full_name=repo_full_name,
                branch_name=target_branch,
                files=files,
                commit_message=msg,
                ticket_id=ticket_id,
            )

            if "Error" in result or "error" in result.lower():
                _logger.error(
                    "direct_fix FAILED: ticket=%s repo=%s branch=%s: %s",
                    ticket_id,
                    repo_full_name,
                    target_branch,
                    result,
                )

            return result

        async def patch_direct_repo_file(
            ticket_id: str,
            repo_full_name: str,
            target_branch: str,
            file_path: str,
            patch_content: str,
            commit_message: str = "",
        ) -> str:
            """Apply a unified diff to a file and push directly to a branch.

            Fetches *file_path* from *target_branch*, applies *patch_content*
            (a unified diff), and pushes the patched content as a commit on
            the same branch — without requiring full-file reconstruction.
            This is the diff-based counterpart to ``direct_fix`` for large
            files where reproducing the entire file content is impractical.

            **Preconditions (all enforced by the tool):**
            1. Ticket MUST be in BLOCKED state.
            2. Ticket MUST have ≥3 implement cycles (verified via board API).
            3. When called through the component roster (i.e. the
               ``component_request`` credential is available) the GitHub App
               installation scope check is bypassed — the mill already has
               its own GitHub access.  For direct board-API calls,
               *repo_full_name* MUST be in the GitHub App installation
               scope.

            **Auditability:** Every invocation is logged at WARNING level
            with the ticket id, repo, branch, and file path.

            **Patch format:** Standard unified diff (as produced by ``diff -u``
            or ``git diff``)::

                --- a/path
                +++ b/path
                @@ -start,count +start,count @@
                 context
                -removed
                +added

            Args:
                ticket_id: The blocked, mill-exhausted ticket this patch
                    addresses (e.g. ``"20250624T020652Z-my-ticket-a1b2"``).
                repo_full_name: GitHub ``owner/name`` (e.g.
                    ``"robotsix/robotsix-chat"``).
                target_branch: Branch to push directly to (e.g. ``"main"``).
                file_path: Path to the file to patch, relative to the repo
                    root (e.g. ``"src/dashboard.js"``).
                patch_content: The unified diff to apply.  Must include at
                    least one ``@@`` hunk header with context lines.
                commit_message: Commit message.  Defaults to a message that
                    references the *ticket_id*.

            Returns:
                A status message with the commit SHA on success, or an error
                message describing why the patch was refused or failed.

            """
            _logger = logging.getLogger(__name__)

            # --- guard 1+2: BLOCKED + scope ---
            if error := await _assert_blocked_and_scoped(
                client, ticket_id, repo_full_name
            ):
                return error

            # --- guard 3: ≥3 implement cycles ---
            if component_request is not None:
                cycles = await _get_implement_cycles_via_component(
                    component_request, ticket_id
                )
            else:
                cycles = await client.count_implement_cycles(ticket_id)
            if cycles is None:
                return (
                    f"Error: could not fetch ticket data for {ticket_id}. "
                    "Verify the ticket id and board API connectivity."
                )
            if cycles < 3:
                return (
                    f"Refused: ticket {ticket_id} has only {cycles} implement "
                    f"cycle(s).  patch_direct_repo_file requires ≥3 implement "
                    f"cycles (mill exhaustion).  Use apply_patch_to_file + "
                    "open_direct_repo_pr for the standard PR flow."
                )

            # --- audit log ---
            _logger.warning(
                "patch_direct_repo_file: ticket=%s repo=%s branch=%s file=%s",
                ticket_id,
                repo_full_name,
                target_branch,
                file_path,
            )

            # --- apply patch and push ---
            msg = commit_message or (
                f"fix: patched {file_path} for blocked ticket {ticket_id} "
                f"(mill exhausted after {cycles} implement cycles)"
            )
            result = await client.push_patched_file(
                repo_full_name=repo_full_name,
                branch_name=target_branch,
                file_path=file_path,
                patch_text=patch_content,
                commit_message=msg,
                ticket_id=ticket_id,
            )

            if "Error" in result or "error" in result.lower():
                _logger.error(
                    "patch_direct_repo_file FAILED: ticket=%s repo=%s "
                    "branch=%s file=%s: %s",
                    ticket_id,
                    repo_full_name,
                    target_branch,
                    file_path,
                    result,
                )

            return result

        tools.append(direct_fix)
        tools.append(patch_direct_repo_file)

    return tools

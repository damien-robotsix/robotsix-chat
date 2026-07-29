"""Direct-repository-capability tools for the chat agent.

Exposes :func:`build_direct_repo_tools` — a factory returning the LLM tools
that let the agent push branches, open PRs, merge PRs, arm auto-merge, and
(when enabled) push direct fixes against repositories in the robotsix-mill
GitHub App's installation scope, authenticating as the app.  Returns no
tools when the direct-repo capability is disabled.

Also exposes :func:`load_direct_repo_skill` which returns the component
skill markdown — a description of the direct-repo tools, their auth
requirements, and their confirmation-gated mutation policy.

**Guardrails enforced by the tools:**
- Branch/PR/fix actions are ONLY permitted for tickets currently in BLOCKED
  state.
- Merge and auto-merge tools require installation scope but do NOT require
  BLOCKED state — they are follow-up operations on PRs already created.
- The repo set is resolved DYNAMICALLY from the GitHub App installation
  (list-installation-repositories) — no static allowlist.
- PRs are opened in a reviewable state with no auto-merge; the merge gate
  stays human.
- A ``recover_auto_merge`` tool can update a stale PR branch to re-arm
  auto-merge when a green, review-approved PR has bounced (no BLOCKED-state
  gate required).
- Merge and auto-merge are confirmation-gated: the agent must obtain
  explicit operator approval in-chat before calling either tool.

**Additional guardrails for ``direct_fix``:**
- Ticket must have exhausted its spawn limit (≥3 implement cycles),
  verified against the board API.
- Every direct-fix action is audited at WARNING log level.
- The tool is only available when ``direct_fix_enabled`` is ``True``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

__all__ = ["build_direct_repo_tools", "load_direct_repo_skill"]


def load_direct_repo_skill() -> str:
    """Return the direct-repo component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.

    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


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

    from robotsix_chat.common.unified_diff import apply_patch as _apply_patch

    from .board_client import BoardClient
    from .client import DirectRepoClient, _count_cycles_from_data

    client = DirectRepoClient(settings)
    board = BoardClient(settings)

    async def _retry_component_ticket_fetch(
        component_req: Callable[..., Any],
        ticket_id: str,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> str:
        """Call *component_req* for a ticket with retry on transient failures.

        Retries up to *max_retries* times with exponential backoff starting
        at *backoff_base* seconds.  Returns the raw response string from the
        last attempt (success or failure).
        """
        last_resp = "Error: no response"
        for attempt in range(max_retries):
            if attempt > 0:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
            resp = cast(
                "str",
                await component_req("mill", "GET", f"/tickets/{ticket_id}"),
            )
            # Treat "Error:" prefixed responses as retryable — they indicate
            # a connectivity or routing failure, not a semantic error.
            if not resp.startswith("Error:"):
                return resp
            last_resp = resp
        return last_resp

    async def _get_ticket_state_via_component(
        component_req: Callable[..., Any],
        ticket_id: str,
    ) -> tuple[str | None, str | None]:
        """Fetch ticket state via *component_req*; return ``(state, error)``.

        Returns ``(state, None)`` on success, ``(None, error_msg)`` on failure.
        The error message includes the connectivity path used for diagnosis.

        The underlying component_request call is retried up to 3 times with
        exponential backoff to absorb transient board-API connectivity issues.
        """
        resp = await _retry_component_ticket_fetch(component_req, ticket_id)
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

    async def _check_blocked_exhausted(
        client: DirectRepoClient,
        ticket_id: str,
        repo_full_name: str,
    ) -> tuple[str | None, int]:
        """Verify BLOCKED state + scope and count implement cycles in one API call.

        Fetches ticket data ONCE via the same connectivity path used by
        ``_assert_blocked_and_scoped``, then extracts both the ``state``
        field and the implement-cycle count from the single response.
        This eliminates the second API round-trip that previously caused
        spurious "could not fetch ticket data" failures when the cycle
        count fetch hit a different (unreachable) path.

        Returns ``(None, cycles)`` when all preconditions pass, or
        ``(error_message, 0)`` when a precondition fails.  The *cycles*
        value is only meaningful when *error* is ``None``.
        """
        # --- scope check: skipped when mill pipeline credential available ---
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error, 0

        # Resolve paraphrased / abbreviated IDs against the live board
        # before fetching ticket data.
        resolved_map = await board.resolve_ticket_ids([ticket_id])
        effective_id = resolved_map.get(ticket_id) or ticket_id

        # --- single API call for state + cycles ---
        if component_request is not None:
            # Retry the roster path with exponential backoff (absorbs
            # transient connectivity failures), then fall back to the
            # direct board-API path when the roster path fails entirely
            # — same pattern used by _assert_blocked_and_scoped.
            resp = await _retry_component_ticket_fetch(component_request, effective_id)
            if resp.startswith("Error:"):
                data = await board.get_ticket_data(effective_id)
                if data is None:
                    board_url = board._board_url
                    return (
                        f"Error: could not fetch ticket data for "
                        f"{effective_id}.  Verify the ticket id and board "
                        f"API connectivity (tried roster path and "
                        f"{board_url}/tickets/{effective_id}).",
                        0,
                    )
                state = data.get("state")
                cycles = _count_cycles_from_data(data)
            else:
                try:
                    newline = resp.index("\n")
                    status_line = resp[:newline]
                    body_str = resp[newline + 1 :]
                except ValueError:
                    return (
                        f"Error: could not parse component_request "
                        f"response for ticket {effective_id} (no status "
                        f"line)",
                        0,
                    )
                if not status_line.startswith("HTTP "):
                    return (
                        f"Error: unexpected component_request response for "
                        f"ticket {effective_id}: {status_line!r}",
                        0,
                    )
                try:
                    status_code = int(status_line.split()[1])
                except IndexError, ValueError:
                    return (
                        f"Error: unparsable HTTP status in "
                        f"component_request response for ticket "
                        f"{effective_id}: {status_line!r}",
                        0,
                    )
                if status_code >= 400:
                    return (
                        f"Error: board API returned HTTP {status_code} for "
                        f"ticket {effective_id} via component_request",
                        0,
                    )
                try:
                    data = json.loads(body_str)
                except json.JSONDecodeError, TypeError:
                    return (
                        f"Error: non-JSON response for ticket "
                        f"{effective_id} via component_request",
                        0,
                    )
                state = data.get("state")
                cycles = _count_cycles_from_data(data)
        else:
            data = await board.get_ticket_data(effective_id)
            if data is None:
                board_url = board._board_url
                return (
                    f"Error: could not fetch ticket data for {effective_id}. "
                    f"Verify the ticket id and board API connectivity "
                    f"(tried {board_url}/tickets/{effective_id}).",
                    0,
                )
            state = data.get("state")
            cycles = _count_cycles_from_data(data)

        # --- state check ---
        if state is None:
            return (
                f"Error: ticket {effective_id} data did not contain a state "
                "field (response was received but is missing required "
                "fields — the ticket may not exist or may be malformed).",
                0,
            )
        if state.upper() != "BLOCKED":
            return (
                f"Refused: ticket {effective_id} is in state '{state}', not BLOCKED. "
                "Direct-repo actions are only permitted for BLOCKED tickets.",
                0,
            )

        return None, cycles

    async def _check_preconditions(
        client: DirectRepoClient,
        ticket_id: str,
        repo_full_name: str,
        action_name: str,
        fallback_name: str,
    ) -> str | None:
        """Verify BLOCKED + scope + ≥3 implement cycles; return error or None.

        Wraps ``_check_blocked_exhausted`` and the cycle-count guard into a
        single helper so the two call sites (``direct_fix`` and
        ``patch_direct_repo_file``) share the same logic — only the refusal
        message differs.
        """
        error, cycles = await _check_blocked_exhausted(
            client, ticket_id, repo_full_name
        )
        if error is not None:
            return error
        if cycles < 3:
            return (
                f"Refused: ticket {ticket_id} has only {cycles} implement "
                f"cycle(s).  {action_name} requires ≥3 implement cycles "
                f"(mill exhaustion).  Use {fallback_name} + "
                "open_direct_repo_pr for the standard PR flow."
            )
        return None

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

        # Resolve paraphrased / abbreviated IDs against the live board
        # before fetching ticket state.  This prevents 404 failures when
        # an ID was derived from narrative text rather than from a board
        # API response.
        resolved_map = await board.resolve_ticket_ids([ticket_id])
        effective_id = resolved_map.get(ticket_id) or ticket_id

        if component_request is not None:
            state, error = await _get_ticket_state_via_component(
                component_request, effective_id
            )
            # Fall back to the direct board-API client when the
            # roster-based (component_request) path fails, so a
            # transient connectivity issue on one path does not
            # hard-fail the entire tool invocation.
            if error is not None:
                fallback_state = await board.get_ticket_state(effective_id)
                if fallback_state is not None:
                    state = fallback_state
                    error = None
        else:
            state = await board.get_ticket_state(effective_id)
            if state is not None:
                error = None
            else:
                board_url = board._board_url
                error = (
                    f"Error: could not determine state for ticket "
                    f"{effective_id}. Verify the ticket id and board API "
                    f"connectivity (tried {board_url}/tickets/{effective_id})."
                )
        if error is not None:
            return error
        if state is not None and state.upper() != "BLOCKED":
            return (
                f"Refused: ticket {effective_id} is in state '{state}', not BLOCKED. "
                "Direct-repo actions are only permitted for BLOCKED tickets."
            )
        if state is None:
            return (
                f"Error: ticket {effective_id} returned no state field. "
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

    async def recover_auto_merge(
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Attempt to recover a PR whose auto-merge has bounced.

        Fetches the PR to verify it is open, then calls GitHub's update-branch
        API to rebase the head branch onto the base.  When auto-merge was
        enabled before the bounce, updating the branch typically re-arms it.

        This tool does **not** require the owning ticket to be in BLOCKED
        state — it is designed for recovery when a green, review-approved PR
        has fallen behind the base branch and auto-merge has failed.

        **Scope:** When called through the component roster (i.e. the
        ``component_request`` credential is available) the GitHub App
        installation scope check is bypassed — the mill already has its
        own GitHub access.  For direct board-API calls, *repo_full_name*
        must be within the robotsix-mill GitHub App's current installation
        scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name``.
            pr_number: The PR number to recover.

        Returns:
            A status message with mergeable state and update-branch outcome.

        """
        # Installation scope check (skipped when component_request is available)
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        # Verify PR exists and is open
        try:
            pr = await client.get_pr(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
            )
        except Exception as exc:
            return f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}"

        state = pr.get("state", "unknown")
        if state != "open":
            return (
                f"PR #{pr_number} in {repo_full_name} is {state}, not open. "
                "Auto-merge recovery only applies to open PRs."
            )

        mergeable_state = pr.get("mergeable_state", "unknown")
        behind_by: int = pr.get("behind_by", pr.get("commits_behind", 0))

        context_lines = [
            f"PR #{pr_number} in {repo_full_name}: {pr.get('title', '(no title)')}",
            f"Mergeable state: {mergeable_state}",
        ]
        if behind_by:
            context_lines.append(f"Behind base by {behind_by} commit(s)")

        result = await client.update_pr_branch(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )

        return "\n".join(context_lines) + "\n\n" + result

    async def _assert_in_scope(
        client: DirectRepoClient,
        repo_full_name: str,
    ) -> str | None:
        """Return an error string if repo not in installation scope, or None.

        Scope check is skipped when *component_request* is available
        (the mill pipeline has its own credentials).
        """
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error
        return None

    async def merge_direct_repo_pr(
        repo_full_name: str,
        pr_number: int,
        pr_title: str,
        head_base_branches: str,
        merge_method: str = "squash",
        commit_title: str = "",
        commit_message: str = "",
    ) -> str:
        """Merge a pull request in a GitHub repository.

        **This is a confirmation-gated mutation.**  Before calling this tool
        you MUST obtain explicit operator approval in the conversation.
        State the exact repo, PR number, PR title, and head/base branches
        and wait for the operator to confirm before proceeding.  Never merge
        a PR without the operator's explicit consent in-chat.

        **Preconditions (enforced server-side by GitHub):**
        - PR must be mergeable (no conflicts).
        - Required status checks / CI must be green.
        - PR must not be in draft state.

        **Scope:** *repo_full_name* must be within the robotsix-mill GitHub
        App's current installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            pr_number: The PR number to merge.
            pr_title: The PR title — used for the confirmation echo.
            head_base_branches: Description of head/base branches
                (e.g. ``"fix/ticket → main"``) — used for the
                confirmation echo.
            merge_method: How to merge — ``"squash"`` (default),
                ``"merge"``, or ``"rebase"``.
            commit_title: Optional merge-commit title (squash/merge).
            commit_message: Optional merge-commit body (squash/merge).

        Returns:
            A status message with the merge commit SHA on success, or an
            actionable error message (conflicts, CI not green, draft, etc.).

        """
        if error := await _assert_in_scope(client, repo_full_name):
            return error

        result = await client.merge_pr(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            merge_method=merge_method,
            commit_title=commit_title or None,
            commit_message=commit_message or None,
        )
        # Include confirmation context in the result for auditability
        if "merged successfully" in result.lower():
            result += f"\nPR: {pr_title}\nBranches: {head_base_branches}"
        return result

    async def arm_direct_repo_auto_merge(
        repo_full_name: str,
        pr_number: int,
        pr_title: str,
        head_base_branches: str,
        merge_method: str = "squash",
    ) -> str:
        """Enable auto-merge on a pull request.

        When auto-merge is enabled GitHub will automatically merge the PR
        as soon as all required conditions are met (CI passes, reviews are
        submitted, branch protection rules are satisfied).  The merge
        happens without further human intervention.

        **This is a confirmation-gated mutation.**  Before calling this tool
        you MUST obtain explicit operator approval in the conversation.
        State the exact repo, PR number, PR title, and head/base branches
        and wait for the operator to confirm before proceeding.  Never
        enable auto-merge without the operator's explicit consent in-chat.

        **Scope:** *repo_full_name* must be within the robotsix-mill GitHub
        App's current installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            pr_number: The PR number to enable auto-merge on.
            pr_title: The PR title — used for the confirmation echo.
            head_base_branches: Description of head/base branches
                (e.g. ``"fix/ticket → main"``) — used for the
                confirmation echo.
            merge_method: Merge strategy to use when auto-merge fires —
                ``"squash"`` (default), ``"merge"``, or ``"rebase"``.

        Returns:
            A success message, or an actionable error message.

        """
        if error := await _assert_in_scope(client, repo_full_name):
            return error

        result = await client.arm_auto_merge(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            merge_method=merge_method,
        )
        # Include confirmation context in the result for auditability
        if "auto-merge enabled" in result.lower():
            result += f"\nPR: {pr_title}\nBranches: {head_base_branches}"
        return result

    async def reset_implement_spawn_counter(ticket_id: str) -> str:
        """Reset the implement-agent spawn counter for a blocked ticket.

        Sends ``POST /tickets/{ticket_id}/resume-blocked`` to the board API
        with a spawn-counter justification, clearing the spawn-limit block
        so the implement agent can be re-spawned against this ticket.

        Tries the component roster path first when *component_request*
        is available (resolving ``"mill"`` via the central-deploy roster
        or component fallbacks); on failure falls back to the direct
        ``board_api_base_url`` path.  When *component_request* is
        unavailable the direct path is used directly.

        On board builds that do not expose the ``implement_spawn_count``
        artifact DELETE route the call returns **HTTP 405 / an error**.
        Do not retry the reset — use the ``resume-blocked`` mechanism
        (``POST /tickets/{id}/resume-blocked``) as the standard fallback.

        Args:
            ticket_id: The blocked ticket whose counter to reset
                (e.g. ``"20250624T020652Z-my-ticket-a1b2"``).

        Returns:
            A status message — success confirmation or an error describing
            why the reset failed.

        """
        # Resolve paraphrased / abbreviated IDs before making the request.
        resolved_map = await board.resolve_ticket_ids([ticket_id])
        effective_id = resolved_map.get(ticket_id) or ticket_id

        justification = (
            "Spawn counter reset — allowing re-implement after spawn limit reached."
        )

        # Try roster path first when available.
        if component_request is not None:
            resp = await component_request(
                "mill",
                "POST",
                f"/tickets/{effective_id}/resume-blocked",
                json_body={"justification": justification},
            )
            if resp.startswith("HTTP 2"):
                return (
                    f"Implement spawn counter reset for ticket {effective_id} "
                    "(via roster path). The ticket can now be re-spawned."
                )
            logger.info(
                "reset_implement_spawn_counter roster path failed for %s; "
                "falling back to direct board API",
                effective_id,
            )

        # Fall back to the direct board API path.
        ok = await board.resume_blocked_ticket(effective_id, justification)
        if ok:
            return (
                f"Implement spawn counter reset for ticket {effective_id}. "
                "The ticket can now be re-spawned."
            )

        board_url = board._board_url
        return (
            f"Error: could not reset implement spawn counter for ticket "
            f"{effective_id}.  Verify the ticket id and board API connectivity "
            f"({board_url})."
        )

    async def apply_patch_to_file(
        ticket_id: str,
        repo_full_name: str,
        branch_name: str,
        file_path: str,
        patch_content: str,
        commit_message: str = "",
        target_branch: str = "",
    ) -> str:
        """Push a patched file to a branch using a unified diff.

        By default (when *target_branch* is empty), fetches the current
        *file_path* from the repo's default branch, applies *patch_content*
        (a unified diff), and pushes the result as a commit on a **new**
        branch named *branch_name*.  This is the standard path for creating
        a fix PR from a blocked ticket and works regardless of the ticket's
        implement-cycle count.

        When *target_branch* is supplied, fetches and patches *file_path*
        from that **existing** branch and pushes the commit back onto it.
        Use this to update an open PR's head branch without creating a
        new branch, again with **no implement-cycle gate**.  This is the
        escape hatch for pushing a fix directly to an existing branch when
        the ticket is BLOCKED but ``patch_direct_repo_file`` refuses due to
        insufficient implement cycles.

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
                ``"fix/20250624T020652Z-my-ticket-a1b2"``).  Ignored when
                *target_branch* is supplied.
            file_path: Path to the file to patch, relative to the repo
                root (e.g. ``"src/dashboard.js"``).
            patch_content: The unified diff to apply.  Must include at
                least one ``@@`` hunk header with context lines.
            commit_message: Commit message.  Defaults to a message that
                references the *ticket_id*.
            target_branch: Optional.  When non-empty, push the patched file
                directly to this existing branch instead of creating a new
                branch.  The file is fetched from *target_branch* (not the
                default branch) so the patch applies to the branch's current
                state.  No implement-cycle gate is enforced.

        Returns:
            A status message with the branch URL on success, or an error
            message describing why the patch was refused or failed.

        """
        if error := await _assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        msg = commit_message or (
            f"fix: patch {file_path} for blocked ticket {ticket_id}"
        )

        if target_branch:
            # Push directly to the existing target branch —
            # fetches from that branch, patches, and pushes back.
            return await client.push_patched_file(
                repo_full_name=repo_full_name,
                branch_name=target_branch,
                file_path=file_path,
                patch_text=patch_content,
                commit_message=msg,
                ticket_id=ticket_id,
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
            patched = _apply_patch(original, patch_content)
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
        recover_auto_merge,
        merge_direct_repo_pr,
        arm_direct_repo_auto_merge,
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

            # --- guard 1+2+3: BLOCKED + scope + ≥3 implement cycles (single API call) ---  # noqa: E501
            if error := await _check_preconditions(
                client,
                ticket_id,
                repo_full_name,
                action_name="direct_fix",
                fallback_name="push_direct_repo_branch",
            ):
                return error

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
                f"(mill exhausted after ≥3 implement cycles)"
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

            # --- guard 1+2+3: BLOCKED + scope + ≥3 implement cycles (single API call) ---  # noqa: E501
            if error := await _check_preconditions(
                client,
                ticket_id,
                repo_full_name,
                action_name="patch_direct_repo_file",
                fallback_name="apply_patch_to_file",
            ):
                return error

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
                f"(mill exhausted after ≥3 implement cycles)"
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

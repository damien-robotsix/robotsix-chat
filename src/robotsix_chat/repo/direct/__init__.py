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

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

from robotsix_http import RetryConfig, acall_with_retry

from .github_tools import build_github_tools

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

    from .board_client import BoardClient
    from .client import DirectRepoClient, _count_cycles_from_data

    client = DirectRepoClient(settings)
    board = BoardClient(settings)

    class _ComponentTicketError(Exception):
        """Raised when component_request returns an ``"Error:"``-prefixed response."""

    async def _retry_component_ticket_fetch(
        component_req: Callable[..., Any],
        ticket_id: str,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> str:
        """Call *component_req* for a ticket with retry on transient failures.

        Retries up to *max_retries* times with exponential backoff starting
        at *backoff_base* seconds via :func:`robotsix_http.acall_with_retry`.
        Returns the raw response string from the last attempt (success or failure).
        """
        if max_retries <= 0:
            return "Error: no response"

        async def _attempt() -> str:
            resp = cast(
                "str",
                await component_req("mill", "GET", f"/tickets/{ticket_id}"),
            )
            # Treat "Error:" prefixed responses as retryable — they indicate
            # a connectivity or routing failure, not a semantic error.
            if resp.startswith("Error:"):
                raise _ComponentTicketError(resp)
            return resp

        try:
            return cast(
                "str",
                await acall_with_retry(
                    _attempt,
                    config=RetryConfig(
                        max_retries=max_retries - 1,
                        backoff_base=backoff_base,
                        backoff_cap=30.0,
                        jitter_factor=0.0,
                    ),
                    is_transient_fn=lambda e: isinstance(e, _ComponentTicketError),
                    what=f"component ticket fetch for {ticket_id}",
                ),
            )
        except _ComponentTicketError as e:
            return str(e)

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

    tools: list[Callable[..., Any]] = build_github_tools(
        client=client,
        board=board,
        settings=settings,
        component_request=component_request,
        assert_blocked_and_scoped=_assert_blocked_and_scoped,
        assert_in_scope=_assert_in_scope,
    )

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

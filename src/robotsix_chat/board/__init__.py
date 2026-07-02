"""Direct HTTP access to the mill's board API via the shared BoardHTTPClient.

Exposes :func:`build_board_reader_tools` — a factory returning the LLM tools
that let the assistant list, read, and create tickets from the SAME HTTP
endpoint the user's browser UI consumes, giving read/write parity with the
user.  Returns no tools when the board reader is disabled, so the chat runs
exactly as before.

The tools are plain async callables; robotsix-llmio converts them into tools for
the underlying agent (the claude-sdk tool loop, or pydantic-ai function tools).
"""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import BoardSettings

logger = logging.getLogger(__name__)

# Set by list_board_tickets / read_board_ticket / create_board_ticket when
# any of them is called during an agent turn.  Checked by the agent after
# the turn completes: if the response looks like board/ticket narrative but
# no board tool was called, the response is blocked (hallucination guard).
board_was_read: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "board_was_read", default=False
)

__all__ = ["board_was_read", "build_board_reader_tools"]


def build_board_reader_tools(
    settings: BoardSettings,
) -> list[Callable[..., Any]]:
    """Return board-reader tool(s) for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    from robotsix_board_client import BoardHTTPClient, ErrorStrategy

    client = BoardHTTPClient(
        base_url=settings.api_base_url,
        token=settings.api_token,
        error_strategy=ErrorStrategy.RETURN,
        cache_ttl=settings.cache_ttl,
    )

    async def list_board_tickets(
        repo_id: str,
        include_closed: bool = False,
        state: str = "",
    ) -> str:
        """List tickets on a board by repo_id.

        Calls the board's ``GET /tickets`` endpoint — the same one the user's
        browser UI polls.  Returns the raw JSON response as text so the
        assistant sees EXACTLY what the user sees.

        Use this to verify what tickets exist and their current states before
        reporting any board/ticket status to the user.  Never narrate or
        fabricate ticket states — always read them first with this tool.

        Args:
            repo_id: The board's repo identifier (e.g. ``"robotsix-chat"``,
                ``"robotsix-mill"``).  Pass ``"all"`` for every board.
            include_closed: When ``True``, include terminal-state tickets
                (CLOSED, EPIC_CLOSED, ANSWERED).  Default ``False`` hides them.
            state: Optional state filter (e.g. ``"draft"``, ``"ready"``,
                ``"in_progress"``).  Empty string = no filter.

        Returns:
            The board API's JSON response as a text string.

        """
        board_was_read.set(True)
        _ = include_closed  # reserved for future client support
        client_state: str | None = state or None
        result = await client.list_tickets(state=client_state, repo_id=repo_id)
        if isinstance(result, dict) and result.get("error"):
            return json.dumps(result)
        # If include_closed is True and no explicit state filter was given,
        # the board API may only return non-closed tickets by default.
        # In that case we do a second pass: the current BoardHTTPClient does
        # not support an include_closed param, so we rely on the board API
        # returning all states when no state filter is passed (state=None).
        # If include_closed is False and state is empty, the board API
        # default already excludes closed tickets.
        return json.dumps(result)

    async def read_board_ticket(ticket_id: str) -> str:
        """Read a single ticket's full details by its id.

        Calls the board's ``GET /tickets/{ticket_id}`` endpoint — the same one
        the user sees when opening a ticket drawer.  Returns the raw JSON
        response as text.

        Use this to verify a specific ticket's state, description, comments, or
        metadata before reporting it to the user.  Always read the ticket first
        — never narrate or fabricate ticket details.

        Args:
            ticket_id: The ticket's unique identifier (e.g. a timestamp-slug
                like ``"20250624T020652Z-my-ticket-a1b2"``).

        Returns:
            The board API's JSON response as a text string, or an error
            message when the ticket is not found.

        """
        board_was_read.set(True)
        result = await client.get_ticket(ticket_id)
        return json.dumps(result)

    async def create_board_ticket(
        title: str,
        description: str,
        repo_id: str,
        kind: str = "",
        force: bool = False,
    ) -> str:
        """Create a new ticket on the board directly.

        Calls the board's ``POST /tickets`` endpoint — the same one the board
        manager uses internally.  This is a direct, synchronous (inline) call:
        no broker indirection, no background sub-agent.  Use this whenever the
        user asks you to file a ticket, create a task, or report a bug on the
        board.

        Prefer this over spawning a subsession for simple ticket creation —
        it is faster and uses fewer tokens.

        Before creating, this tool checks for likely duplicates among OPEN
        tickets on the target repo and surfaces candidates so you can comment
        on / reuse the existing ticket instead.  If a candidate is flagged,
        the tool returns a descriptive warning WITHOUT posting; you should
        then ``read_board_ticket`` the candidate(s) and decide whether to
        comment/reuse or to override by re-calling with ``force=True``.

        Args:
            title: Short, descriptive title for the ticket (required).
            description: Full description / body — be thorough; include
                context, steps, and any relevant details (required).
            repo_id: The board's repo identifier (e.g. ``"robotsix-chat"``,
                ``"robotsix-mill"``).  Required.
            kind: Optional ticket kind hint (e.g. ``"task"``, ``"bug"``,
                ``"epic"``).  Empty string = board default (usually "task").
            force: Set ``force=True`` ONLY after confirming via
                ``read_board_ticket`` that the existing candidate(s) are
                genuinely a different intent.

        Returns:
            The board API's JSON response as a text string (the created
            ticket), or an error message on failure, OR a duplicate-warning
            message listing candidate ticket(s) to review.

        """
        from .dedup import find_duplicate_candidates

        board_was_read.set(True)

        if not force:
            raw = await client.list_tickets(repo_id=repo_id)
            tickets: list[dict[str, Any]] = []
            # BoardHTTPClient with ErrorStrategy.RETURN returns parsed JSON
            # on success or an error dict on failure.
            if isinstance(raw, dict) and raw.get("error"):
                logger.warning(
                    "create_board_ticket: list_tickets returned error; "
                    "proceeding to create (fail-open). raw=%r",
                    raw,
                )
            elif isinstance(raw, list):
                tickets = raw
            else:
                logger.warning(
                    "create_board_ticket: list_tickets returned unexpected "
                    "type %s; proceeding to create (fail-open).",
                    type(raw).__name__,
                )

            if tickets:
                candidates = find_duplicate_candidates(title, tickets)
                if candidates:
                    lines = [
                        "⚠️ Likely duplicate ticket(s) found — NOT created.",
                        "",
                        "Existing OPEN ticket(s) with a similar title:",
                    ]
                    for c in candidates:
                        cid = c.get("id", "?")
                        ctitle = c.get("title", "?")
                        cstate = c.get("state", "?")
                        lines.append(f'  – {cid}  state={cstate}  title="{ctitle}"')
                    lines.append("")
                    lines.append(
                        "ACTION: Use read_board_ticket to inspect the "
                        "candidate(s). If they cover the same intent, "
                        "comment on / reuse the existing ticket instead "
                        "of filing a duplicate. If your intent is "
                        "genuinely distinct, re-call "
                        "create_board_ticket with force=True."
                    )
                    return "\n".join(lines)

        result = await client.create_ticket(
            title=title,
            description=description,
            repo_id=repo_id,
            kind=kind or "task",
        )
        return json.dumps(result)

    return [list_board_tickets, read_board_ticket, create_board_ticket]

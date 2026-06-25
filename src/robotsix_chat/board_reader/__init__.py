"""Direct HTTP access to the mill's board API.

Exposes :func:`build_board_reader_tools` — a factory returning the LLM tools
that let the assistant list, read, and create tickets from the SAME HTTP
endpoint the user's browser UI consumes, giving read/write parity with the
user.  Returns no tools when the board reader is disabled, so the chat runs
exactly as before.

The tools are plain async callables; robotsix-llmio converts them into tools for
the underlying agent (the claude-sdk tool loop, or pydantic-ai function tools).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import BoardReaderSettings

logger = logging.getLogger(__name__)

__all__ = ["build_board_reader_tools"]


def build_board_reader_tools(
    settings: BoardReaderSettings,
) -> list[Callable[..., Any]]:
    """Return board-reader tool(s) for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    from .client import BoardReader

    client = BoardReader(settings)

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
        return await client.list_tickets(
            repo_id=repo_id,
            include_closed=include_closed,
            state=state,
        )

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
        return await client.get_ticket(ticket_id)

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

        Prefer this over ``delegate_task`` for simple ticket creation — it is
        faster and uses fewer tokens.

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
        import json

        from .dedup import find_duplicate_candidates

        if not force:
            raw = await client.list_tickets(
                repo_id=repo_id,
                include_closed=False,
            )
            tickets: list[dict[str, Any]] = []
            parse_ok = True
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    tickets = parsed
                else:
                    parse_ok = False
            except (json.JSONDecodeError, TypeError):
                parse_ok = False

            if not parse_ok:
                logger.warning(
                    "create_board_ticket: list_tickets returned non-JSON list; "
                    "proceeding to create (fail-open). raw=%r",
                    raw[:200] if isinstance(raw, str) else repr(raw)[:200],
                )
            else:
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

        return await client.create_ticket(
            title=title,
            description=description,
            repo_id=repo_id,
            kind=kind,
        )

    return [list_board_tickets, read_board_ticket, create_board_ticket]

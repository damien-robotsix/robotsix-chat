"""Direct HTTP read access to the mill's board API.

Exposes :func:`build_board_reader_tools` — a factory returning the LLM tools
that let the assistant list and read tickets from the SAME HTTP endpoint the
user's browser UI consumes, giving read parity with the user.  Returns no tools
when the board reader is disabled, so the chat runs exactly as before.

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

    return [list_board_tickets, read_board_ticket]

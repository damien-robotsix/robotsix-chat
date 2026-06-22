"""Calendar/tasks integration over the agent-comm broker.

Exposes :func:`build_calendar_tools` — a factory returning the LLM tools that
let the chat agent query and manage the user's calendar and tasks in natural
language. Returns no tools when calendar integration is disabled or when the
``broker`` extra (robotsix-agent-comm) is absent, so the chat runs exactly as
before.

Both calendar and task requests route to the single ``robotsix-calendar-agent``
recipient under the assumption that one agent handles both CalDAV ``VEVENT`` and
``VTODO`` objects. If a distinct tasks recipient is needed later, add a separate
``tasks_agent_id`` setting and pass it from the task-tool callables.

The tools are plain async callables; robotsix-llmio converts them into tools for
the underlying agent (the claude-sdk tool loop, or pydantic-ai function tools).
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import CalendarSettings

logger = logging.getLogger(__name__)

__all__ = ["build_calendar_tools"]


def build_calendar_tools(settings: CalendarSettings) -> list[Callable[..., Any]]:
    """Return the calendar/task tools for the agent, or ``[]`` when unavailable."""
    if not settings.enabled:
        return []
    if importlib.util.find_spec("robotsix_agent_comm") is None:
        logger.warning(
            "calendar.enabled is true but the 'broker' extra (robotsix-agent-comm) "
            "is not installed — calendar tools are unavailable. Install "
            "robotsix-chat[broker]."
        )
        return []

    from .client import CalendarClient

    client = CalendarClient(settings)

    async def query_calendar(request: str) -> str:
        """Read and answer questions about the user's schedule and upcoming events.

        Use this whenever the user asks "what's on my calendar", "what do I have
        today/this week", "when is my next meeting", or similar schedule queries.
        Pass the user's question as-is — the calendar agent interprets it.

        Args:
            request: A natural-language question about the user's schedule.

        Returns:
            The calendar agent's reply.

        """
        return await client.consult(request, domain="calendar")

    async def manage_calendar(request: str) -> str:
        """Create or update calendar events.

        Use this when the user wants to schedule a new event, reschedule, cancel,
        or update an existing event. Pass a clear natural-language description
        that includes the event title, date/time, and attendees (if any).

        Args:
            request: A natural-language description of the calendar change.

        Returns:
            The calendar agent's reply confirming the change (or an error).

        """
        return await client.consult(request, domain="calendar")

    async def query_tasks(request: str) -> str:
        """List the user's tasks/to-dos.

        Use this when the user asks "what's on my task list", "what are my
        to-dos", "show me my tasks", or similar queries about their pending
        work items.

        Args:
            request: A natural-language question about the user's tasks.

        Returns:
            The calendar/tasks agent's reply listing matching tasks.

        """
        return await client.consult(request, domain="tasks")

    async def manage_tasks(request: str) -> str:
        """Create, update, or complete tasks.

        Use this when the user wants to add a new to-do, mark a task done, update
        a task's title or due date, or delete a task. Pass a clear
        natural-language description of the change.

        Args:
            request: A natural-language description of the task change.

        Returns:
            The calendar/tasks agent's reply confirming the change (or an error).

        """
        return await client.consult(request, domain="tasks")

    return [query_calendar, manage_calendar, query_tasks, manage_tasks]

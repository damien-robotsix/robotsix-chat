"""Talk to robotsix-calendar-agent over the agent-comm broker.

Thin subclass of :class:`~robotsix_chat.broker_client.BaseBrokeredClient` that
wires the calendar-specific target agent ID, default reply, and the ``domain``
payload key for CalDAV object routing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robotsix_chat.broker_client import BaseBrokeredClient

if TYPE_CHECKING:
    from robotsix_chat.config import CalendarSettings


class CalendarClient(BaseBrokeredClient):
    """Forwards natural-language calendar/task requests to the calendar agent."""

    def __init__(self, settings: CalendarSettings) -> None:
        """Store the calendar broker settings, build a brokered requester."""
        super().__init__(
            settings,
            target_agent_id=settings.calendar_agent_id,
            default_reply="The calendar agent returned no reply.",
        )

    async def consult(self, request: str, *, domain: str) -> str:
        """Send *request* to the calendar agent under *domain* and return its reply.

        *domain* is ``"calendar"`` or ``"tasks"`` — passed in the payload so the
        calendar agent can route to the right CalDAV object type (``VEVENT`` vs
        ``VTODO``).

        Never raises: broker/timeout/recipient errors become a short message the
        calling LLM can relay to the user.
        """
        return await super().consult(
            request,
            empty_reply=f"No request was provided to send to the {domain} agent.",
            error_label=f"calendar ({domain})",
            domain=domain,
        )

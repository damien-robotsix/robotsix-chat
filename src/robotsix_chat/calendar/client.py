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

    # robotsix-calendar-agent reads the request text from the ``"instruction"``
    # key (not the board-manager's ``"message"``); see its agent.process().
    _request_key = "instruction"

    def __init__(self, settings: CalendarSettings) -> None:
        """Store the calendar broker settings, build a brokered requester."""
        super().__init__(
            settings,
            target_agent_id=settings.calendar_agent_id,
            default_reply="The calendar agent returned no reply.",
        )

    async def consult(
        self,
        request: str,
        *,
        empty_reply: str = "",
        error_label: str = "",
        **extra_payload: object,
    ) -> str:
        """Send *request* to the calendar agent under *domain* and return its reply.

        *domain* is ``"calendar"`` or ``"tasks"`` — passed in the payload so the
        calendar agent can route to the right CalDAV object type (``VEVENT`` vs
        ``VTODO``).

        May raise :class:`BrokerUnavailableError` (propagated from
        :meth:`BaseBrokeredClient.consult`) when the broker cannot reach the
        calendar agent.  All other errors are caught and returned as text.
        """
        domain = str(extra_payload.pop("domain", ""))
        return await super().consult(
            request,
            empty_reply=f"No request was provided to send to the {domain} agent.",
            error_label=f"calendar ({domain})",
            domain=domain,
        )

"""Talk to robotsix-calendar-agent over the agent-comm broker.

Uses :class:`~robotsix_agent_comm.sdk.BrokeredRequester` to send a
natural-language request to ``calendar-agent-robotsix`` and relay the reply.
The blocking broker call is offloaded to a thread so it never stalls the async
server.  robotsix-agent-comm is imported lazily (the optional ``broker`` extra);
failures degrade to a message the agent can relay, never an exception into the
chat path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robotsix_chat.config import CalendarSettings

logger = logging.getLogger(__name__)


class CalendarClient:
    """Forwards natural-language calendar/task requests to the calendar agent."""

    def __init__(self, settings: CalendarSettings) -> None:
        """Store the calendar broker settings, build a brokered requester."""
        # Lazy import: robotsix-agent-comm is the optional `broker` extra.
        from robotsix_agent_comm.sdk import BrokeredRequester

        self._s = settings
        self._requester = BrokeredRequester(
            settings.agent_id,
            settings.calendar_agent_id,
            broker_host=settings.broker_host,
            broker_port=settings.broker_port,
            broker_scheme=settings.broker_scheme,
            broker_token=settings.broker_token,
            timeout=settings.timeout,
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
        if not request.strip():
            return f"No request was provided to send to the {domain} agent."
        try:
            payload: dict[str, str] = {"message": request, "domain": domain}
            return await asyncio.to_thread(self._requester.request, payload)
        except Exception as exc:  # noqa: BLE001 — surface as text, never crash chat
            logger.warning("calendar consult (%s) failed: %s", domain, exc)
            return f"The calendar request could not be completed: {exc}"

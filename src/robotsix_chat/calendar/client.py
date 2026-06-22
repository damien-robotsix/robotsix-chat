"""Talk to robotsix-calendar-agent over the agent-comm broker.

Mirrors the mill → board pattern: a per-call pull/mailbox
:class:`~robotsix_agent_comm.sdk.agent.Agent` sends a natural-language request to
``calendar-agent-robotsix`` and relays the reply. The blocking broker call is
offloaded to a thread so it never stalls the async server. robotsix-agent-comm is
imported lazily (the optional ``broker`` extra); failures degrade to a message
the agent can relay, never an exception into the chat path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import CalendarSettings

logger = logging.getLogger(__name__)


class CalendarClient:
    """Forwards natural-language calendar/task requests to the calendar agent."""

    def __init__(self, settings: CalendarSettings) -> None:
        """Store the calendar broker settings for later consult calls."""
        self._s = settings

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
            body = await asyncio.to_thread(self._send, request, domain)
        except Exception as exc:  # noqa: BLE001 — surface as text, never crash chat
            logger.warning("calendar consult (%s) failed: %s", domain, exc)
            return f"The calendar request could not be completed: {exc}"
        return _reply_text(body)

    def _send(self, request: str, domain: str) -> Any:
        # Lazy import: robotsix-agent-comm is the optional `broker` extra.
        from robotsix_agent_comm.protocol import Error
        from robotsix_agent_comm.sdk.agent import Agent
        from robotsix_agent_comm.transport.brokered import create_transport_pair

        s = self._s
        registry, transport = create_transport_pair(
            "brokered",
            broker_host=s.broker_host,
            broker_port=s.broker_port,
            broker_scheme=s.broker_scheme,
            broker_token=s.broker_token,
        )
        agent = Agent(
            s.agent_id, registry, transport=transport, pull=True, timeout=s.timeout
        )
        payload: dict[str, Any] = {"message": request, "domain": domain}
        # `with agent:` registers a mailbox + receive loop, torn down per call.
        with agent:
            reply = agent.send_request(s.calendar_agent_id, payload, timeout=s.timeout)
        if isinstance(reply, Error):
            err = getattr(reply, "body", None) or {}
            raise RuntimeError(err.get("message") or "calendar agent returned an error")
        return getattr(reply, "body", None)


def _reply_text(body: Any) -> str:
    """Extract the calendar agent's human-readable reply from its response body."""
    if body is None:
        return "The calendar agent returned no reply."
    if isinstance(body, dict):
        reply = body.get("reply")
        if isinstance(reply, str) and reply.strip():
            return reply
        return str(body)
    return str(body)

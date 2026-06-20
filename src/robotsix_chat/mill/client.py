"""Talk to robotsix-mill's board manager over the agent-comm broker.

Mirrors the cost-analyst → board pattern: a per-call pull/mailbox
:class:`~robotsix_agent_comm.sdk.agent.Agent` sends a natural-language request to
``board-manager-robotsix-mill`` and relays the reply. The blocking broker call is
offloaded to a thread so it never stalls the async server. robotsix-agent-comm is
imported lazily (the optional ``broker`` extra); failures degrade to a message
the agent can relay, never an exception into the chat path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import MillSettings

logger = logging.getLogger(__name__)


class MillClient:
    """Forwards natural-language requests to the mill's board manager."""

    def __init__(self, settings: MillSettings) -> None:
        self._s = settings

    async def consult(self, request: str) -> str:
        """Send *request* to the board manager and return its reply as text.

        Never raises: broker/timeout/board errors become a short message the
        calling LLM can relay to the user.
        """
        if not request.strip():
            return "No request was provided to send to the mill."
        try:
            body = await asyncio.to_thread(self._send, request)
        except Exception as exc:  # noqa: BLE001 — surface as text, never crash chat
            logger.warning("mill consult failed: %s", exc)
            return f"The mill request could not be completed: {exc}"
        return _reply_text(body)

    def _send(self, request: str) -> Any:
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
        payload: dict[str, Any] = {"message": request}
        if s.repo_id:
            payload["repo_id"] = s.repo_id
        # `with agent:` registers a mailbox + receive loop, torn down per call.
        with agent:
            reply = agent.send_request(
                s.board_manager_id, payload, timeout=s.timeout
            )
        if isinstance(reply, Error):
            err = getattr(reply, "body", None) or {}
            raise RuntimeError(err.get("message") or "board manager returned an error")
        return getattr(reply, "body", None)


def _reply_text(body: Any) -> str:
    """Extract the board manager's human-readable reply from its response body."""
    if body is None:
        return "The mill board manager returned no reply."
    if isinstance(body, dict):
        reply = body.get("reply")
        if isinstance(reply, str) and reply.strip():
            return reply
        return str(body)
    return str(body)

"""Talk to robotsix-mill's board manager over the agent-comm broker.

Uses :class:`~robotsix_agent_comm.sdk.BrokeredRequester` to send a
natural-language request to ``board-manager-robotsix-mill`` and relay the reply.
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
    from robotsix_chat.config import MillSettings

logger = logging.getLogger(__name__)


class MillClient:
    """Forwards natural-language requests to the mill's board manager."""

    def __init__(self, settings: MillSettings) -> None:
        """Store the mill broker settings, build a brokered requester."""
        # Lazy import: robotsix-agent-comm is the optional `broker` extra.
        from robotsix_agent_comm.sdk import BrokeredRequester

        self._s = settings
        self._requester = BrokeredRequester(
            settings.agent_id,
            settings.board_manager_id,
            broker_host=settings.broker_host,
            broker_port=settings.broker_port,
            broker_scheme=settings.broker_scheme,
            broker_token=settings.broker_token,
            timeout=settings.timeout,
            default_reply="The mill board manager returned no reply.",
        )

    async def consult(self, request: str) -> str:
        """Send *request* to the board manager and return its reply as text.

        Never raises: broker/timeout/board errors become a short message the
        calling LLM can relay to the user.
        """
        if not request.strip():
            return "No request was provided to send to the mill."
        try:
            payload: dict[str, object] = {"message": request}
            if self._s.repo_id:
                payload["repo_id"] = self._s.repo_id
            return await asyncio.to_thread(self._requester.request, payload)
        except Exception as exc:  # noqa: BLE001 — surface as text, never crash chat
            logger.warning("mill consult failed: %s", exc)
            return f"The mill request could not be completed: {exc}"

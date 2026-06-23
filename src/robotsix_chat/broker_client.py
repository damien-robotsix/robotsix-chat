"""Shared base for brokered agent-comm clients.

Provides the lazy import of :class:`~robotsix_agent_comm.sdk.BrokeredRequester`,
the common ``__init__`` pattern, and a ``consult()`` method that offloads the
blocking broker call to a thread so it never stalls the async server.

robotsix-agent-comm is imported lazily (the optional ``broker`` extra); failures
degrade to a message the agent can relay, never an exception into the chat path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class BaseBrokeredClient:
    """Base for brokered clients that forward requests to an agent over the broker.

    Subclasses pass *target_agent_id* (the broker-registered ID of the
    recipient agent) and *default_reply* (the fallback when the broker
    returns an empty reply).  ``consult()`` forwards any ``**extra_payload``
    keys into the request dict alongside the request text.

    The request text is sent under ``_request_key`` (default ``"message"``,
    matching the mill board-manager's contract).  Subclasses whose recipient
    expects a different key override it — e.g. the calendar agent requires
    ``"instruction"``.
    """

    _request_key: str = "message"

    def __init__(
        self,
        settings: Any,
        *,
        target_agent_id: str,
        default_reply: str,
    ) -> None:
        """Store the broker settings and build a brokered requester."""
        # Lazy import: robotsix-agent-comm is the optional `broker` extra.
        from robotsix_agent_comm.sdk import BrokeredRequester  # noqa: I001

        self._s = settings
        self._requester = BrokeredRequester(
            settings.agent_id,
            target_agent_id,
            broker_host=settings.broker_host,
            broker_port=settings.broker_port,
            broker_scheme=settings.broker_scheme,
            broker_token=settings.broker_token,
            timeout=settings.timeout,
            default_reply=default_reply,
        )

    async def consult(
        self,
        request: str,
        *,
        empty_reply: str,
        error_label: str,
        **extra_payload: object,
    ) -> str:
        """Send *request* to the target agent, forwarding **extra_payload.

        Never raises: broker/timeout/recipient errors become a short message
        the calling LLM can relay to the user.
        """
        if not request.strip():
            return empty_reply
        try:
            payload: dict[str, object] = {self._request_key: request, **extra_payload}
            return await asyncio.to_thread(self._requester.request, payload)
        except Exception as exc:  # noqa: BLE001 — surface as text, never crash chat
            logger.warning("%s consult failed: %s", error_label, exc)
            return f"The {error_label} request could not be completed: {exc}"

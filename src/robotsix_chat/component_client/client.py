"""Component agent client — inspect and configure remote component agents.

Talks to any component agent's responder over the agent-comm broker.  Not a
subclass of :class:`~robotsix_chat.broker_client.BaseBrokeredClient` because it
targets multiple recipient agents (one per configured component), caching one
:class:`~robotsix_agent_comm.sdk.BrokeredRequester` per target.

robotsix-agent-comm is imported lazily (the optional ``broker`` extra); failures
degrade to a message the agent can relay, never an exception into the chat path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from robotsix_chat.broker_client import _is_broker_unavailable

if TYPE_CHECKING:
    from robotsix_chat.config import ComponentClientSettings

logger = logging.getLogger(__name__)

__all__ = ["ComponentAgentClient"]


class ComponentAgentClient:
    """Sends ``monitor``, ``config-get``, ``config-set`` requests to component agents.

    One instance is shared across all tools.  A :class:`BrokeredRequester` is
    created (and cached) per distinct *target_agent_id* on first use, using the
    settings' broker host, port, token, and timeout.
    """

    def __init__(self, settings: ComponentClientSettings) -> None:
        """Store the broker settings; requesters are built lazily per target."""
        self._settings = settings
        self._requesters: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public async methods (called by tool callables)
    # ------------------------------------------------------------------

    async def monitor(self, target_agent_id: str) -> str:
        """Fetch live telemetry from *target_agent_id*.

        Sends ``{"kind": "monitor", "payload": {}}``.
        """
        return await self._send(target_agent_id, "monitor", {})

    async def config_get(self, target_agent_id: str) -> str:
        """Read the current config from *target_agent_id*.

        Sends ``{"kind": "config-get", "payload": {}}``.
        """
        return await self._send(target_agent_id, "config-get", {})

    async def config_set(self, target_agent_id: str, updates: dict[str, Any]) -> str:
        """Apply a validated config update to *target_agent_id*.

        Sends ``{"kind": "config-set", "payload": {"updates": updates}}``.
        """
        return await self._send(target_agent_id, "config-set", {"updates": updates})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_requester(self, target_agent_id: str) -> Any:
        """Return a cached (or new) ``BrokeredRequester`` for *target_agent_id*."""
        if target_agent_id not in self._requesters:
            # Lazy import: robotsix-agent-comm is the optional `broker` extra.
            from robotsix_agent_comm.sdk import BrokeredRequester  # noqa: I001

            s = self._settings
            self._requesters[target_agent_id] = BrokeredRequester(
                s.agent_id,
                target_agent_id,
                broker_host=s.broker_host,
                broker_port=s.broker_port,
                broker_scheme=s.broker_scheme,
                broker_token=s.broker_token,
                timeout=s.timeout,
                default_reply="",
            )
        return self._requesters[target_agent_id]

    async def _send(
        self, target_agent_id: str, kind: str, payload: dict[str, Any]
    ) -> str:
        """Send ``{"kind": kind, "payload": payload}`` and return the reply.

        Broker-unreachable errors and all other exceptions are caught and
        returned as a user-facing string — these are interactive inspect/
        configure tools, not durable writes.
        """
        requester = self._get_requester(target_agent_id)
        body: dict[str, Any] = {"kind": kind, "payload": payload}
        try:
            return await asyncio.to_thread(requester.request, body)
        except Exception as exc:  # noqa: BLE001 — surface as text, never crash chat
            if _is_broker_unavailable(exc):
                logger.warning(
                    "component-agent %s %s request broker unavailable: %s",
                    target_agent_id,
                    kind,
                    exc,
                )
                return f"Component agent '{target_agent_id}' is unreachable: {exc}"
            logger.warning(
                "component-agent %s %s request failed: %s",
                target_agent_id,
                kind,
                exc,
            )
            return f"Request to component agent '{target_agent_id}' failed: {exc}"

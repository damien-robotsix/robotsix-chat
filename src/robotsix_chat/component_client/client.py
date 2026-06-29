"""Component agent client — inspect and configure remote component agents.

Talks to any component agent's HTTP API directly
(``POST /api/component-agent/monitor``, ``POST /api/component-agent/config``)
— no broker indirection.  Degrades gracefully: HTTP/timeout errors become
strings the assistant can relay to the user.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from robotsix_chat.common.http import safe_http_request

if TYPE_CHECKING:
    from robotsix_chat.config import ComponentClientSettings

logger = logging.getLogger(__name__)

__all__ = ["ComponentAgentClient"]


class ComponentAgentClient:
    """Sends ``monitor``, ``config-get``, ``config-set`` requests to component agents.

    One instance is shared across all tools.  Makes direct HTTP POST calls
    to each target's ``/api/component-agent/monitor`` or
    ``/api/component-agent/config`` endpoint — no broker, no cached
    connections.
    """

    def __init__(self, settings: ComponentClientSettings) -> None:
        """Store the shared timeout; no persistent HTTP client."""
        self._timeout = settings.timeout

    # ------------------------------------------------------------------
    # Public async methods (called by tool callables)
    # ------------------------------------------------------------------

    async def monitor(self, base_url: str) -> str:
        """Fetch live telemetry from the component agent at *base_url*.

        POSTs ``{"kind": "monitor", "payload": {}}`` to
        ``/api/component-agent/monitor``.
        """
        return await self._post(base_url, "monitor", {})

    async def config_get(self, base_url: str) -> str:
        """Read the current config from the component agent at *base_url*.

        POSTs ``{"kind": "config-get", "payload": {}}`` to
        ``/api/component-agent/config``.
        """
        return await self._post(base_url, "config-get", {})

    async def config_set(self, base_url: str, updates: dict[str, Any]) -> str:
        """Apply a validated config update to the component agent at *base_url*.

        POSTs ``{"kind": "config-set", "payload": {"updates": updates}}`` to
        ``/api/component-agent/config``.
        """
        return await self._post(base_url, "config-set", {"updates": updates})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _post(
        self, base_url: str, kind: str, payload: dict[str, Any]
    ) -> str:
        """POST to the component agent endpoint and return the text body.

        All errors (HTTP status, timeout, connection) are caught by
        :func:`safe_http_request` and returned as user-facing strings
        — these are interactive inspect/configure tools, not durable writes.
        """
        endpoint = "monitor" if kind == "monitor" else "config"
        url = f"{base_url.rstrip('/')}/api/component-agent/{endpoint}"
        result = await safe_http_request(
            "POST",
            url,
            json_body={"kind": kind, "payload": payload},
            timeout=self._timeout,
            label="Component Agent",
        )
        if result.error:
            return result.error
        return result.text  # type: ignore[return-value]

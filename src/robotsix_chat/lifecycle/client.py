"""HTTP client for the deploy-lifecycle API.

Calls the central-deploy lifecycle server over HTTP with ``X-API-Key``
auth.  All methods return strings — success payloads and error messages
alike — so nothing raises into the agent loop.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from robotsix_chat.common.http import safe_http_request
from robotsix_chat.config import LifecycleSettings

logger = logging.getLogger(__name__)


class LifecycleClient:
    """Read-only HTTP client for the deploy-lifecycle API."""

    def __init__(self, settings: LifecycleSettings) -> None:
        self._s = settings
        self._base_url = settings.base_url.rstrip("/")

    # -- public methods ---------------------------------------------------

    async def list_services(self) -> str:
        """``GET /services`` — list all managed services."""
        return await self._get("/services")

    async def service_status(self, service_name: str) -> str:
        """``GET /services/{name}/status`` — status and health."""
        return await self._get(f"/services/{service_name}/status")

    async def service_config(self, service_name: str) -> str:
        """``GET /services/{name}/config`` — config (secrets masked)."""
        return await self._get(f"/services/{service_name}/config")

    async def service_env(self, service_name: str) -> str:
        """``GET /services/{name}/env`` — environment (secrets masked)."""
        return await self._get(f"/services/{service_name}/env")

    # -- internals --------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        api_key = self._s.api_key.get_secret_value()
        if api_key:
            headers["X-API-Key"] = api_key
        return headers

    async def _get(self, path: str) -> str:
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "GET",
            url,
            headers=self._headers(),
            timeout=self._s.timeout,
            label="Lifecycle",
        )
        if result.error:
            return result.error
        # Re-serialise through json for consistent formatting.
        try:
            parsed = json.loads(str(result.text))
            return json.dumps(parsed, indent=2)
        except Exception:
            return str(result.text)

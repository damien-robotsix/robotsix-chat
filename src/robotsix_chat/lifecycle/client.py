"""HTTP client for the deploy-lifecycle API.

Calls the central-deploy lifecycle server over HTTP with ``X-API-Key``
auth.  All methods return strings — success payloads and error messages
alike — so nothing raises into the agent loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from robotsix_chat.common.http import safe_http_request
from robotsix_chat.config import LifecycleSettings

logger = logging.getLogger(__name__)

# Defaults for watch_service_redeploy.
_DEFAULT_MAX_WAIT_SECONDS = 300.0  # 5 minutes
_DEFAULT_POLL_INTERVAL_SECONDS = 15.0
_MIN_POLL_INTERVAL_SECONDS = 5.0


class LifecycleClient:
    """Read-only HTTP client for the deploy-lifecycle API."""

    def __init__(self, settings: LifecycleSettings) -> None:
        """Initialise with lifecycle settings."""
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

    async def watch_service_redeploy(
        self,
        service_name: str,
        max_wait_seconds: float = _DEFAULT_MAX_WAIT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> str:
        """Poll the lifecycle server until *service_name* is redeployed.

        Takes a snapshot of the service config, then polls every
        *poll_interval_seconds* until the config changes (indicating a
        redeploy) or *max_wait_seconds* elapses.  Returns a summary of
        what happened.

        Args:
            service_name: The lifecycle-registered service to watch.
            max_wait_seconds: Maximum time to wait before giving up
                (clamped to the configured client timeout as a lower
                bound so the tool always reports before the HTTP client
                itself would time out).
            poll_interval_seconds: Seconds between poll attempts (minimum
                5 s — lower values are clamped).

        Returns:
            A status summary string.

        """
        if poll_interval_seconds < _MIN_POLL_INTERVAL_SECONDS:
            poll_interval_seconds = _MIN_POLL_INTERVAL_SECONDS

        config_path = f"/services/{service_name}/config"
        status_path = f"/services/{service_name}/status"

        # Take the initial snapshots.
        initial_config = await self._get_raw(config_path)
        initial_status = await self._get_raw(status_path)

        if initial_config is None or initial_status is None:
            return (
                f"Could not reach the lifecycle server to watch "
                f"{service_name} — check that the service name is "
                f"correct and the lifecycle API is reachable."
            )

        started_at = time.monotonic()
        deadline = started_at + max_wait_seconds
        attempts = 0

        while True:
            elapsed = time.monotonic() - started_at
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return (
                    f"Timeout after {elapsed:.0f}s ({attempts} polls) — "
                    f"{service_name} config has not changed.  The service "
                    f"may still be running the previous deployment.  "
                    f"Notify the operator or check the central-deploy "
                    f"dashboard to trigger a manual redeploy."
                )

            await asyncio.sleep(min(poll_interval_seconds, max(0.0, remaining)))
            attempts += 1

            current_config = await self._get_raw(config_path)
            if current_config is None:
                logger.warning(
                    "Lifecycle config poll %d for %s failed — will retry.",
                    attempts,
                    service_name,
                )
                continue

            if current_config != initial_config:
                current_status = await self._get_raw(status_path)
                status_text = (
                    json.dumps(json.loads(current_status), indent=2)
                    if current_status
                    else "(unavailable)"
                )
                return (
                    f"Redeploy detected for {service_name} after "
                    f"{elapsed:.0f}s ({attempts} polls).\n\n"
                    f"Current status:\n{status_text}"
                )

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

    async def _get_raw(self, path: str) -> str | None:
        """Return the raw response text, or ``None`` on any failure."""
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "GET",
            url,
            headers=self._headers(),
            timeout=self._s.timeout,
            label="Lifecycle",
        )
        if result.error:
            return None
        return result.text

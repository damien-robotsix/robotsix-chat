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
from typing import Any

from robotsix_chat.common.http import safe_http_request
from robotsix_chat.config import LifecycleSettings

logger = logging.getLogger(__name__)

# Defaults for watch_service_redeploy.
_DEFAULT_MAX_WAIT_SECONDS = 300.0  # 5 minutes
_DEFAULT_POLL_INTERVAL_SECONDS = 15.0
_MIN_POLL_INTERVAL_SECONDS = 5.0

# Set of recognised URL schemes that do not need a protocol prepend.
_KNOWN_SCHEMES = frozenset({"http", "https"})


def _ensure_url_scheme(raw: str, default_protocol: str) -> str:
    """Return *raw* with a protocol scheme if it is missing one.

    If *raw* is empty, returns it as-is (the caller is expected to
    handle the empty-base-url case separately).  If *raw* already
    contains ``://`` with a recognised scheme, returns it unchanged.
    Otherwise prepends ``{default_protocol}://`` and logs a warning.
    """
    if not raw:
        return raw
    # Already has a scheme component?
    if "://" in raw:
        scheme = raw.split("://", 1)[0]
        if scheme in _KNOWN_SCHEMES:
            return raw
        # Unrecognised scheme — leave it alone so httpx can reject it
        # with a clear error rather than silently rewriting it.
        return raw
    # No scheme — apply the default protocol.
    fixed = f"{default_protocol}://{raw}"
    logger.warning(
        "lifecycle.base_url has no URL scheme; prepending %s:// → %s",
        default_protocol,
        fixed,
    )
    return fixed


def _is_transient_error(result: str) -> bool:
    """Return ``True`` if *result* indicates a retryable (transient) error.

    Heuristic: lifecycle error strings follow the pattern
    ``"Lifecycle error NNN for METHOD URL: …"`` (HTTP errors) or
    ``"Lifecycle request timed out …"`` / ``"Lifecycle request failed: …"``
    (network-level errors).  5xx server errors and network failures are
    transient and worth retrying; 4xx client errors (configuration or
    auth) are not.
    """
    if "Lifecycle error 5" in result:
        return True
    if "Lifecycle request timed out" in result:
        return True
    return "Lifecycle request failed:" in result


class LifecycleClient:
    """HTTP client for the deploy-lifecycle API.

    Provides read-only inspection and (when permitted by the deploy
    server's per-repo access toggle) mutation operations: restart,
    config-write, and env-write for the agent's own service.
    """

    def __init__(self, settings: LifecycleSettings) -> None:
        """Initialise with lifecycle settings."""
        self._s = settings
        base_url = _ensure_url_scheme(
            settings.base_url, settings.default_protocol
        ).rstrip("/")
        if not base_url:
            logger.warning(
                "lifecycle.base_url is empty — all lifecycle API calls "
                "will fail with a URL protocol error."
            )
        self._base_url = base_url

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
            max_wait_seconds: Maximum time to wait before giving up.
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                elapsed = time.monotonic() - started_at
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
                elapsed = time.monotonic() - started_at
                current_status = await self._get_raw(status_path)
                if current_status:
                    try:
                        status_text = json.dumps(json.loads(current_status), indent=2)
                    except Exception:
                        status_text = current_status
                else:
                    status_text = "(unavailable)"
                return (
                    f"Redeploy detected for {service_name} after "
                    f"{elapsed:.0f}s ({attempts} polls).\n\n"
                    f"Current status:\n{status_text}"
                )

    async def restart_service(self, service_name: str) -> str:
        """``POST /services/{name}/restart`` — restart a service."""
        return await self._post(f"/services/{service_name}/restart")

    async def self_restart(self) -> str:
        """Restart this service via ``POST /chat/services/{name}/restart``.

        The deploy server exposes **no** bare ``/self/restart`` route; a
        service restarts itself by naming itself through
        ``lifecycle.service_name``.  This uses the chat-agent restart
        endpoint, granted by the same ``allow_chat_access`` /
        ``chat_agent_mutatable`` flag that gates the other mutation
        endpoints (restart access — not the more sensitive ``update``
        capability).  Returns a clear message (never raises) when
        ``service_name`` is not configured.

        On transient failures this method retries with exponential
        backoff (configurable via ``self_restart_max_retries``,
        ``self_restart_backoff_base``, and ``self_restart_backoff_cap``)
        before reporting failure.  Non-retryable errors (e.g. 4xx client
        errors) are returned immediately.
        """
        name = self._s.service_name
        if not name:
            return (
                "self_restart is unavailable: lifecycle.service_name is not "
                "configured, so this service cannot name itself to the deploy "
                "server."
            )
        if not self._base_url:
            return (
                "self_restart is unavailable: lifecycle.base_url is empty. "
                "Set lifecycle.base_url to the deploy-lifecycle API address "
                "(e.g. http://central-deploy:8100) and restart the chat server."
            )

        path = f"/chat/services/{name}/restart"
        max_retries = self._s.self_restart_max_retries
        backoff_base = self._s.self_restart_backoff_base
        backoff_cap = self._s.self_restart_backoff_cap

        last_result: str | None = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = min(backoff_base * (2 ** (attempt - 1)), backoff_cap)
                logger.warning(
                    "self_restart retry %d/%d after %.1fs",
                    attempt,
                    max_retries,
                    delay,
                )
                await asyncio.sleep(delay)

            result = await self._post(path)

            # Retry only on transient errors: 5xx or network-level failures
            # (timeouts, connection errors).  4xx errors are not retryable.
            if _is_transient_error(result):
                last_result = result
                continue

            return result

        # All retries exhausted.
        logger.error(
            "self_restart failed after %d retries (max %d): %s",
            max_retries,
            max_retries,
            last_result,
        )
        return (
            f"self_restart failed after {max_retries + 1} attempts. "
            f"Last error: {last_result}"
        )

    async def update_service_config(
        self, service_name: str, config: dict[str, Any]
    ) -> str:
        """``PUT /services/{name}/config`` — update service configuration."""
        return await self._put(f"/services/{service_name}/config", config)

    async def update_service_env(self, service_name: str, env: dict[str, Any]) -> str:
        """``PUT /services/{name}/env`` — update service environment."""
        return await self._put(f"/services/{service_name}/env", env)

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

    async def _post(self, path: str, json_body: dict[str, Any] | None = None) -> str:
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "POST",
            url,
            headers=self._headers(),
            timeout=self._s.timeout,
            json_body=json_body,
            label="Lifecycle",
        )
        if result.error:
            return result.error
        try:
            parsed = json.loads(str(result.text))
            return json.dumps(parsed, indent=2)
        except Exception:
            return str(result.text)

    async def _put(self, path: str, json_body: dict[str, Any]) -> str:
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "PUT",
            url,
            headers=self._headers(),
            timeout=self._s.timeout,
            json_body=json_body,
            label="Lifecycle",
        )
        if result.error:
            return result.error
        try:
            parsed = json.loads(str(result.text))
            return json.dumps(parsed, indent=2)
        except Exception:
            return str(result.text)

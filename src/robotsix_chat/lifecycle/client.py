"""HTTP client for the deploy-lifecycle API.

Calls the central-deploy lifecycle server over HTTP with ``X-API-Key``
auth.  All methods return strings — success payloads and error messages
alike — so nothing raises into the agent loop.

Configuration management is owned by each component internally; this
client does not expose config-store endpoints (``GET/PUT
/services/{name}/config``).  Use the component's own ``/config``
endpoints for configuration access.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast
from urllib.parse import urlparse

from robotsix_http import RetryConfig, acall_with_retry

from robotsix_chat.common.http import safe_http_request
from robotsix_chat.config import LifecycleSettings

logger = logging.getLogger(__name__)

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


def _validate_http_url(url: str) -> bool:
    """Return ``True`` if *url* is a valid absolute HTTP(S) URL.

    The URL must have a recognised scheme (``http`` or ``https``) and a
    non-empty host component.  This is stricter than ``urlparse``'s own
    parsing — it rejects relative URLs, protocol-only stubs (e.g.
    ``http://``), and URLs with unrecognised schemes.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme in _KNOWN_SCHEMES and parsed.netloc)
    except Exception:
        return False


class _TransientLifecycleError(Exception):
    """Raised when a lifecycle API call returns a transient error string."""


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


# -- self_restart diagnostic report ---------------------------------------

# Mapping of error substrings to (plain_language, next_steps) pairs.
# Ordered by specificity: more specific patterns are checked first.
_SELF_RESTART_DIAGNOSTICS: list[tuple[str, str, str]] = [
    (
        "Lifecycle error 400",
        "The deploy server rejected the restart request as malformed.",
        (
            "Verify that lifecycle.service_name is set to the exact "
            "service identifier registered with central-deploy (check "
            "list_lifecycle_services for the correct name)."
        ),
    ),
    (
        "Lifecycle error 401",
        "The deploy server rejected the API key — authentication failed.",
        (
            "Check that lifecycle.api_key is set correctly in the "
            "chat server's configuration and that the key has not "
            "expired or been revoked in central-deploy."
        ),
    ),
    (
        "Lifecycle error 403",
        "The deploy server denied the restart — the chat-agent restart "
        "toggle is not enabled for this service.",
        (
            "Ask the operator to enable the chat_agent_mutatable flag "
            "in central-deploy for this component, or restart the "
            "service manually via the central-deploy dashboard."
        ),
    ),
    (
        "Lifecycle error 404",
        "The deploy server does not recognise this service name.",
        (
            "Check that lifecycle.service_name matches a registered "
            "service in central-deploy.  Run list_lifecycle_services "
            "to see the available service identifiers."
        ),
    ),
    (
        "Lifecycle request timed out",
        "The deploy server did not respond in time — "
        "a network or firewall issue may be blocking the request.",
        (
            "Verify that the deploy server is reachable from this "
            "host (check lifecycle.base_url) and that no firewall "
            "rules are blocking outbound HTTP to the deploy server's "
            "port."
        ),
    ),
    (
        "Lifecycle request failed:",
        "The HTTP request to the deploy server could not be completed — "
        "this may be a network error, DNS failure, or a URL protocol "
        "configuration problem.",
        (
            "Check that lifecycle.base_url is a valid HTTP URL "
            "(e.g. http://central-deploy:8100) and that the deploy "
            "server hostname resolves from this container.  If the "
            "base_url has an unrecognised scheme, fix it to http "
            "or https."
        ),
    ),
]


def _diagnose_self_restart_failure(
    result: str,
    attempts: int,
    max_retries: int,
) -> str:
    """Build a diagnostic report for a failed ``self_restart`` call.

    *result* is the last error string returned by the lifecycle API call.
    The report categorises the failure and includes plain-language
    explanation and actionable next steps so the agent (or operator) can
    self-remediate without reading raw HTTP logs.
    """
    # Categorise the error.
    explanation = ""
    next_steps = ""
    for pattern, expl, steps in _SELF_RESTART_DIAGNOSTICS:
        if pattern in result:
            explanation = expl
            next_steps = steps
            break

    if not explanation:
        # Generic fallback for unexpected / unclassified errors.
        explanation = (
            "The deploy server returned an unexpected error during the restart request."
        )
        next_steps = (
            "Inspect the raw error below.  Check that lifecycle.base_url "
            "points to the correct deploy-lifecycle API address and that "
            "the deploy server is reachable and healthy."
        )

    # Build the report.
    parts: list[str] = []
    parts.append("## self_restart failure diagnostic")
    parts.append("")

    if attempts > 1:
        parts.append(
            f"The restart was attempted **{attempts}** time(s) "
            f"(max retries: {max_retries}) and every attempt failed."
        )
    else:
        parts.append(
            "The restart failed on the first attempt — the error is not "
            "retryable so no further attempts were made."
        )
    parts.append("")

    parts.append("### What happened")
    parts.append(explanation)
    parts.append("")

    parts.append("### Next steps")
    parts.append(next_steps)
    parts.append("")

    parts.append("### Raw error")
    parts.append("```")
    parts.append(result)
    parts.append("```")

    return "\n".join(parts)


def _service_is_healthy(data: dict[str, Any]) -> bool:
    """Return ``True`` when *data* indicates the service is healthy.

    Checks for common status-field shapes produced by the deploy
    lifecycle server:
    - ``{"status": "running", "health_checks": [{"ok": true}, ...]}``
    - ``{"status": "healthy"}``
    - ``{"state": "running"}``
    """
    status = str(data.get("status") or data.get("state", "")).lower()
    if status not in ("running", "healthy", "up"):
        return False
    health_checks = data.get("health_checks") or data.get("healthChecks")
    if isinstance(health_checks, list) and health_checks:
        return all(
            h.get("ok") or h.get("healthy") or h.get("passing")
            for h in health_checks
            if isinstance(h, dict)
        )
    # No health-checks array — trust the status field alone.
    return True


def _running_image_matches(data: dict[str, Any], expected_ref: str) -> bool | None:
    """Check whether the running image in *data* matches *expected_ref*.

    Returns:
        ``True`` when the running image matches *expected_ref*.
        ``False`` when image info is present but does not match.
        ``None`` when no image information is available in *data*.

    The comparison is substring-based: a match occurs when either the
    running image string contains *expected_ref* or *expected_ref*
    contains the running image string.  This handles partial forms
    (bare ``:tag``, full ``registry/repo:tag``, ``@sha256:...``).

    """
    image_candidates: list[str] = []
    for key in (
        "image",
        "image_digest",
        "image_id",
        "container_image",
        "Image",
        "Config.Image",
    ):
        val = data.get(key)
        if isinstance(val, str) and val:
            image_candidates.append(val)

    # Also check nested paths.
    for container_key in ("container", "Container", "spec", "Spec"):
        container = data.get(container_key)
        if isinstance(container, dict):
            for img_key in ("image", "Image", "image_digest"):
                val = container.get(img_key)
                if isinstance(val, str) and val:
                    image_candidates.append(val)

    if not image_candidates:
        return None  # No image info available.

    for running_image in image_candidates:
        if expected_ref in running_image or running_image in expected_ref:
            return True
    return False


class LifecycleClient:
    """HTTP client for the deploy-lifecycle API.

    Provides read-only inspection and (when permitted by the deploy
    server's per-repo access toggle) mutation operations: restart
    and env-write.  Configuration management is owned by each
    component internally — use the component's own ``/config``
    endpoints.
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
        elif not _validate_http_url(base_url):
            logger.warning(
                "lifecycle.base_url %r is malformed — all lifecycle API "
                "calls will fail.  Set lifecycle.base_url to a valid "
                "HTTP URL (e.g. http://central-deploy:8100) and restart "
                "the chat server.",
                base_url,
            )
            base_url = ""
        self._base_url = base_url

    # -- public methods ---------------------------------------------------

    async def list_services(self) -> str:
        """``GET /services`` — list all managed services."""
        return await self._get("/services")

    async def service_status(self, service_name: str) -> str:
        """``GET /services/{name}/status`` — status and health."""
        return await self._get(f"/services/{service_name}/status")

    async def service_env(self, service_name: str) -> str:
        """``GET /services/{name}/env`` — environment (secrets masked)."""
        return await self._get(f"/services/{service_name}/env")

    async def restart_service(self, service_name: str) -> str:
        """``POST /services/{name}/restart`` — restart a service."""
        return await self._post(f"/services/{service_name}/restart")

    async def redeploy_service(self, service_name: str, image_ref: str = "") -> str:
        """``POST /services/{name}/redeploy`` — redeploy a service.

        Sends a redeploy request to the deploy server.  When *image_ref*
        is non-empty, the deploy server is instructed to pull that
        specific image reference (e.g. ``sha-abc123def``) rather than the
        service's default tag (which may be stale if the build pipeline
        has not finished publishing the latest ``:main`` tag).

        Args:
            service_name: The service identifier to redeploy.
            image_ref: Optional specific image reference (tag, digest,
                or ``sha-<commit>``) to pull.  When empty, the deploy
                server uses the service's default image tag.

        Returns:
            The redeploy result, or an error message.

        """
        if image_ref:
            return await self._post(
                f"/services/{service_name}/redeploy",
                json_body={"image_ref": image_ref},
            )
        return await self._post(f"/services/{service_name}/redeploy")

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
        backoff (configurable via ``self_restart_max_retries``)
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

        attempts = 0

        async def _attempt() -> str:
            nonlocal attempts
            attempts += 1
            result = await self._post(path)
            if _is_transient_error(result):
                raise _TransientLifecycleError(result)
            return result

        try:
            result = cast(
                "str",
                await acall_with_retry(
                    _attempt,
                    config=RetryConfig(
                        max_retries=max_retries,
                        backoff_base=1.0,
                        backoff_cap=30.0,
                        jitter_factor=0.0,
                    ),
                    is_transient_fn=lambda e: isinstance(e, _TransientLifecycleError),
                    what="self_restart",
                ),
            )
            # Success responses (valid JSON, not a lifecycle error string)
            # don't start with "Lifecycle" — return them as-is.
            if not result.startswith("Lifecycle"):
                return result
            # Non-retryable error (e.g. 4xx, URL protocol) — return a
            # diagnostic report so the agent can self-remediate.
            return _diagnose_self_restart_failure(result, attempts, max_retries)
        except _TransientLifecycleError as e:
            # All retries exhausted on transient errors.
            return _diagnose_self_restart_failure(str(e), attempts, max_retries)

    async def update_service_env(self, service_name: str, env: dict[str, Any]) -> str:
        """``PUT /services/{name}/env`` — update service environment."""
        return await self._put(f"/services/{service_name}/env", env)

    async def verify_deployment(
        self,
        service_name: str,
        expected_image_ref: str = "",
        poll_timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 15.0,
    ) -> str:
        """Poll *service_name* status until healthy and image matches.

        Calls ``GET /services/{name}/status`` in a loop, waiting up to
        *poll_timeout_seconds*.  Returns a JSON verdict with
        ``verified`` (bool), ``service_status``, and ``detail``.
        """
        import asyncio
        import time

        start = time.monotonic()
        last_status: str = ""
        attempts = 0

        while True:
            attempts += 1
            elapsed = time.monotonic() - start
            if elapsed >= poll_timeout_seconds:
                return json.dumps(
                    {
                        "verified": False,
                        "service_name": service_name,
                        "expected_image_ref": expected_image_ref,
                        "detail": (
                            f"Timed out after {elapsed:.0f}s "
                            f"({attempts} poll(s)).  Last known status "
                            "is included below."
                        ),
                        "last_service_status": last_status,
                    },
                    indent=2,
                )

            status = await self.service_status(service_name)
            last_status = status

            if status.startswith("Lifecycle"):
                # Error from the API — not a transient network issue,
                # so return immediately.
                return json.dumps(
                    {
                        "verified": False,
                        "service_name": service_name,
                        "expected_image_ref": expected_image_ref,
                        "detail": f"Lifecycle API error: {status}",
                        "last_service_status": status,
                    },
                    indent=2,
                )

            # Try to parse status for image info.
            try:
                data: dict[str, Any] = json.loads(status)
            except json.JSONDecodeError:
                data = {}

            service_healthy = _service_is_healthy(data)

            if not service_healthy:
                await asyncio.sleep(poll_interval_seconds)
                continue

            # --- healthy ---
            if not expected_image_ref:
                # No image ref to verify — healthy is enough.
                return json.dumps(
                    {
                        "verified": True,
                        "service_name": service_name,
                        "detail": (
                            f"Service is running and healthy "
                            f"after {elapsed:.0f}s ({attempts} poll(s))."
                        ),
                        "last_service_status": status,
                    },
                    indent=2,
                )

            # Check image match.
            image_match = _running_image_matches(data, expected_image_ref)
            if image_match is True:
                return json.dumps(
                    {
                        "verified": True,
                        "service_name": service_name,
                        "expected_image_ref": expected_image_ref,
                        "detail": (
                            f"Service is healthy and image matches "
                            f"{expected_image_ref} after {elapsed:.0f}s "
                            f"({attempts} poll(s))."
                        ),
                        "last_service_status": status,
                    },
                    indent=2,
                )
            if image_match is False:
                # Image present but does not match — keep polling
                # (a redeploy may still be in progress).
                pass
            # image_match is None → no image info in status; keep polling.

            await asyncio.sleep(poll_interval_seconds)

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        api_key = self._s.api_key.get_secret_value()
        if api_key:
            headers["X-API-Key"] = api_key
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        *,
        raw: bool = False,
    ) -> str | None:
        if not self._base_url:
            return (
                None
                if raw
                else (
                    "Lifecycle base URL is not configured or is malformed. "
                    "Set lifecycle.base_url to a valid HTTP URL "
                    "(e.g. http://central-deploy:8100) and restart the "
                    "chat server."
                )
            )
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            method,
            url,
            headers=self._headers(),
            timeout=self._s.timeout,
            json_body=json_body,
            label="Lifecycle",
        )
        if raw:
            return None if result.error else result.text
        if result.error:
            return result.error
        # Re-serialise through json for consistent formatting.
        try:
            parsed = json.loads(str(result.text))
            return json.dumps(parsed, indent=2)
        except Exception:
            return str(result.text)

    async def _get(self, path: str) -> str:
        return await self._request("GET", path)  # type: ignore[return-value]

    async def _post(self, path: str, json_body: dict[str, Any] | None = None) -> str:
        return await self._request("POST", path, json_body=json_body)  # type: ignore[return-value]

    async def _put(self, path: str, json_body: dict[str, Any]) -> str:
        return await self._request("PUT", path, json_body=json_body)  # type: ignore[return-value]

"""Generic ``component_request`` tool and its factory.

Returns a single async callable that the LLM uses to call any component
in the roster — no per-component tools, no typed board operations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from robotsix_chat.config import CentralDeploySettings

logger = logging.getLogger(__name__)

_TRUNCATE_LENGTH = 8000

# Retry configuration for transient component-call failures.
_MAX_ATTEMPTS = 3
_BASE_DELAY = 1.0  # seconds
_MAX_DELAY = 10.0  # seconds
_HEALTH_PROBE_TIMEOUT = 2.0  # seconds


def _is_transient_exception(exc: Exception) -> bool:
    """Return True if *exc* represents a transient (retryable) error.

    Network-level errors (connection refused, timeout, protocol errors)
    are transient — the request may not have reached the server at all.
    An empty exception message is also treated as transient: it often
    indicates a proxy/network hiccup that didn't produce a meaningful
    diagnostic.
    """
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
            httpx.NetworkError,
        ),
    ):
        return True
    if isinstance(exc, OSError):
        return True
    # Empty error messages often signal transient network hiccups.
    return bool(not str(exc))


def _is_terminal_http_status(status_code: int, method: str) -> bool:
    """Return True if this HTTP status should NOT be retried.

    For idempotent methods (GET, HEAD, PUT, DELETE): only 4xx is terminal.
    For non-idempotent methods (POST, PATCH): ANY HTTP response is terminal
    — the write may have been partially or fully processed server-side,
    and a retry would risk duplication.
    """
    if method.upper() in ("POST", "PATCH"):
        # Non-idempotent: a response means the server received the request.
        return True
    # Idempotent: only client errors (4xx) are terminal.
    return 400 <= status_code < 500


async def _health_probe(base_url: str) -> bool:
    """Lightweight health check before attempting component calls.

    Returns True if the component's /health endpoint responds (any 2xx),
    False if it is unreachable or errors. Used to distinguish a
    genuinely-down component from a transient request failure.
    """
    url = f"{base_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_PROBE_TIMEOUT) as client:
            resp = await client.get(url)
            return 200 <= resp.status_code < 300
    except Exception:
        return False


async def _component_request_impl(
    roster_entries: list[dict[str, Any]],
    component_id: str,
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
) -> str:
    """Call *component_id*'s API at *method* *path*.

    Resolves the component's ``base_url`` from the roster only — refuses
    unknown ids and absolute URLs. Returns the response status + truncated
    body as a string.
    """
    # Resolve the component from the roster.
    # If the roster is empty or contains only error sentinels, surface a
    # specific message — this is usually a transient upstream blip, not a
    # registration problem.
    non_error = [e for e in roster_entries if not e.get("_error")]
    if not non_error:
        return (
            "Error: component roster is currently empty or unavailable — "
            "this is likely transient; retry shortly."
        )

    entry: dict[str, Any] | None = None
    for e in roster_entries:
        if e.get("id") == component_id:
            entry = e
            break

    if entry is None:
        known = [e.get("id", "?") for e in non_error]
        return (
            f"Error: unknown component_id '{component_id}'. "
            f"Known components: {', '.join(known) if known else '(none)'}"
        )

    if entry.get("_error"):
        return f"Error: roster unavailable — {entry.get('_error', 'unknown error')}"

    base_url = entry.get("base_url", "")
    if not base_url:
        return f"Error: component '{component_id}' has no base_url in the roster"

    # Sanity: refuse absolute URLs / hosts in path.
    if path.startswith(("http://", "https://", "//")):
        return "Error: path must be relative (e.g. /tickets), not an absolute URL"

    method_upper = method.upper()
    if method_upper not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return f"Error: unsupported HTTP method '{method}'"

    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    headers: dict[str, str] = {"Accept": "application/json"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    # Apply the roster entry's auth metadata: the roster carries env-var
    # NAMES (never secret values); the credentials themselves live in this
    # process's environment, provisioned via the deploy EnvStore.
    auth: tuple[str, str] | None = None
    auth_meta = entry.get("auth") or {}
    auth_type = auth_meta.get("type", "")
    if auth_type == "basic":
        username = os.environ.get(auth_meta.get("username_env", ""), "")
        password = os.environ.get(auth_meta.get("password_env", ""), "")
        if not (username and password):
            return (
                f"Error: component '{component_id}' requires Basic auth via "
                f"env vars {auth_meta.get('username_env')!r}/"
                f"{auth_meta.get('password_env')!r}, which are not set in the "
                "agent environment. Provision them via the deploy EnvStore "
                "and redeploy robotsix-chat."
            )
        auth = (username, password)
    elif auth_type == "header":
        header_name = auth_meta.get("header_name", "")
        token = os.environ.get(auth_meta.get("token_env", ""), "")
        if not (header_name and token):
            return (
                f"Error: component '{component_id}' requires a "
                f"{header_name or '?'} header via env var "
                f"{auth_meta.get('token_env')!r}, which is not set in the "
                "agent environment. Provision it via the deploy EnvStore "
                "and redeploy robotsix-chat."
            )
        headers[header_name] = token

    auth_arg: Any = auth if auth is not None else httpx.USE_CLIENT_DEFAULT

    # Optional health probe before the first attempt — if the component
    # is genuinely down, we surface a clear message without wasting retries.
    health_ok = await _health_probe(base_url)
    if not health_ok:
        logger.warning(
            "Health probe failed for %s at %s — component may be down; "
            "will still attempt the request",
            component_id,
            base_url,
        )

    def _format_body(status: int, body_str: str) -> str:
        """Format a response body with truncation."""
        if len(body_str) > _TRUNCATE_LENGTH:
            body_str = body_str[:_TRUNCATE_LENGTH] + (
                f"\n\n... (truncated at {_TRUNCATE_LENGTH} chars, "
                f"original length {len(body_str)})"
            )
        return f"HTTP {status}\n{body_str}"

    last_error: str | None = None
    last_status: int | None = None
    last_body_str: str | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        resp: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method_upper == "GET":
                    resp = await client.get(url, headers=headers, auth=auth_arg)
                elif method_upper == "DELETE":
                    resp = await client.delete(url, headers=headers, auth=auth_arg)
                else:
                    resp = await client.request(
                        method_upper,
                        url,
                        headers=headers,
                        json=json_body,
                        auth=auth_arg,
                    )
        except Exception as exc:
            if not _is_transient_exception(exc):
                # Non-transient error — report immediately.
                logger.warning(
                    "component_request %s %s %s failed with non-transient "
                    "error (attempt %d/%d): %s",
                    component_id,
                    method_upper,
                    path,
                    attempt,
                    _MAX_ATTEMPTS,
                    exc,
                )
                return f"Error calling {component_id} {method_upper} {path}: {exc}"

            # Transient exception — retry if attempts remain.
            last_error = str(exc) or f"{type(exc).__name__} (no detail)"
            if attempt < _MAX_ATTEMPTS:
                delay = min(_BASE_DELAY * (2 ** (attempt - 1)), _MAX_DELAY)
                logger.warning(
                    "component_request %s %s %s transient error "
                    "(attempt %d/%d, retrying in %.1fs): %s",
                    component_id,
                    method_upper,
                    path,
                    attempt,
                    _MAX_ATTEMPTS,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                continue
            logger.error(
                "component_request %s %s %s exhausted %d attempts; last error: %s",
                component_id,
                method_upper,
                path,
                _MAX_ATTEMPTS,
                exc,
            )
            continue

        # We got an HTTP response (resp is not None).
        if resp is None:  # pragma: no cover — defensive, not reachable
            raise RuntimeError("Expected httpx.Response but got None")
        status = resp.status_code

        # Extract body once.
        try:
            body = resp.json()
            body_str = json.dumps(body)
        except Exception:
            body_str = resp.text

        # Save for possible exhausted-return at loop end.
        last_status = status
        last_body_str = body_str

        # Immediate return: success (2xx / 3xx) or terminal status.
        if status < 400 or _is_terminal_http_status(status, method_upper):
            tag = "terminal" if status >= 400 else "ok"
            logger.info(
                "component_request %s %s %s → %d (%s, attempt %d/%d)",
                component_id,
                method_upper,
                path,
                status,
                tag,
                attempt,
                _MAX_ATTEMPTS,
            )
            return _format_body(status, body_str)

        # Retryable HTTP status (5xx on idempotent methods).
        last_error = f"HTTP {status}"
        if attempt < _MAX_ATTEMPTS:
            delay = min(_BASE_DELAY * (2 ** (attempt - 1)), _MAX_DELAY)
            logger.warning(
                "component_request %s %s %s → %d (attempt %d/%d, retrying in %.1fs)",
                component_id,
                method_upper,
                path,
                status,
                attempt,
                _MAX_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)
        else:
            logger.error(
                "component_request %s %s %s → %d (attempt %d/%d, exhausted)",
                component_id,
                method_upper,
                path,
                status,
                attempt,
                _MAX_ATTEMPTS,
            )

    # All retries exhausted.
    if last_status is not None and last_body_str is not None:
        return _format_body(last_status, last_body_str)
    return (
        f"Error calling {component_id} {method_upper} {path}: "
        f"all {_MAX_ATTEMPTS} attempts failed. Last error: {last_error}"
    )


def build_component_access_tools(
    settings: CentralDeploySettings,
) -> list[Callable[..., Any]]:
    """Return component-access tool(s) for the agent.

    When ``settings.url`` is empty, returns ``[]`` — no tools, no
    system-prompt injection.

    The roster is fetched once at agent construction time and refreshed
    on each tool call if the TTL has expired.
    """
    if not settings.url:
        return []

    from .roster import fetch_roster

    # We need a mutable container so the closure can refresh the roster
    # between calls.
    _state: dict[str, Any] = {"entries": []}

    async def _refresh() -> None:
        _state["entries"] = await fetch_roster(settings)

    async def component_request(
        component_id: str,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        """Call an external component's API.

        Use this to interact with any registered component. Each component
        declares its own API surface as a skill — consult the skill
        descriptions for allowed operations, paths, and safety rules.

        Args:
            component_id: The component's identifier (e.g. "robotsix-mill").
            method: HTTP method — GET, POST, PUT, PATCH, or DELETE.
            path: The API path relative to the component's base URL
                (e.g. "/tickets", "/chat/skill").
            json_body: Optional JSON body for POST/PUT/PATCH requests.

        Returns:
            The component's HTTP status code and response body (truncated
            if very long), or an error message.

        """
        # Refresh the roster on every call (TTL-gated internally).
        await _refresh()
        return await _component_request_impl(
            _state["entries"], component_id, method, path, json_body
        )

    return [component_request]

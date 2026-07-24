"""Generic ``component_request`` tool and its factory.

Returns a single async callable that the LLM uses to call any component
in the roster — no per-component tools, no typed board operations.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
from robotsix_http import ExternalHTTPError, RetryClient, RetryConfig

if TYPE_CHECKING:
    from robotsix_chat.config import CentralDeploySettings

logger = logging.getLogger(__name__)

_TRUNCATE_LENGTH = 8000  # default for write methods (POST/PUT/PATCH/DELETE)

_HEALTH_PROBE_TIMEOUT = 2.0  # seconds

# Retry configuration for transient component-call failures.
# max_retries=2 + 1 initial = 3 total attempts, matching the prior hand-rolled
# _MAX_ATTEMPTS=3.
_COMPONENT_RETRY_CONFIG = RetryConfig(
    max_retries=2,
    backoff_base=1.0,
    backoff_cap=10.0,
    jitter_factor=0.5,
)


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
    read_response_max_chars: int = _TRUNCATE_LENGTH,
    component_credentials: dict[str, Any] | None = None,
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

    # Resolve credentials from the component_credentials config dict,
    # keyed by component id.  The roster carries auth metadata (type,
    # header name); the actual secret values live in config, never in env.
    creds = (component_credentials or {}).get(component_id)
    auth: tuple[str, str] | None = None
    auth_meta = entry.get("auth") or {}
    auth_type = auth_meta.get("type", "")
    if auth_type == "basic":
        if creds is None:
            return (
                f"Error: component '{component_id}' requires Basic auth "
                f"but no credentials are configured in "
                f"central_deploy.component_credentials.{component_id}. "
                "Add a ComponentCredentials entry for this component."
            )
        username = creds.basic_auth_username.get_secret_value()
        password = creds.basic_auth_password.get_secret_value()
        if not (username and password):
            return (
                f"Error: component '{component_id}' requires Basic auth "
                f"but basic_auth_username and/or basic_auth_password are "
                f"empty in central_deploy.component_credentials.{component_id}."
            )
        auth = (username, password)
    elif auth_type == "header":
        if creds is None:
            return (
                f"Error: component '{component_id}' requires header auth "
                f"but no credentials are configured in "
                f"central_deploy.component_credentials.{component_id}. "
                "Add a ComponentCredentials entry for this component."
            )
        header_name = auth_meta.get("header_name", "")
        token = creds.header_token.get_secret_value()
        if not (header_name and token):
            return (
                f"Error: component '{component_id}' requires a "
                f"{header_name or '?'} header but header_token is "
                f"empty in central_deploy.component_credentials.{component_id}."
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
        """Format a response body with truncation.

        Read-only methods (GET, HEAD) use *read_response_max_chars*;
        write methods use the lower ``_TRUNCATE_LENGTH`` default.
        """
        limit = (
            read_response_max_chars
            if method_upper in ("GET", "HEAD")
            else _TRUNCATE_LENGTH
        )
        if len(body_str) > limit:
            body_str = body_str[:limit] + (
                f"\n\n... (truncated at {limit} chars, original length {len(body_str)})"
            )
        return f"HTTP {status}\n{body_str}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        retry_client = RetryClient(client, config=_COMPONENT_RETRY_CONFIG)
        try:
            resp = await retry_client.request(
                method_upper,
                url,
                headers=headers,
                json=json_body,
                auth=auth_arg,
            )
        except ExternalHTTPError as exc:
            # Terminal HTTP status (mapped by the library: auth errors, rate
            # limits, service errors) — return the body so the caller can
            # inspect it.
            status = exc.status_code
            try:
                body = exc.response.json()
                body_str = json.dumps(body)
            except Exception:
                body_str = exc.response.text
            logger.info(
                "component_request %s %s %s → %d (terminal, not retried)",
                component_id,
                method_upper,
                path,
                status,
            )
            return _format_body(status, body_str)
        except httpx.HTTPStatusError as exc:
            # Unmapped HTTP status (e.g., 404, 418) — also terminal.
            status = exc.response.status_code
            try:
                body = exc.response.json()
                body_str = json.dumps(body)
            except Exception:
                body_str = exc.response.text
            logger.info(
                "component_request %s %s %s → %d (terminal, not retried)",
                component_id,
                method_upper,
                path,
                status,
            )
            return _format_body(status, body_str)
        except Exception as exc:
            # All retries exhausted or non-retryable error.
            logger.error(
                "component_request %s %s %s failed after retries: %s",
                component_id,
                method_upper,
                path,
                exc,
            )
            return f"Error calling {component_id} {method_upper} {path}: {exc}"

    # Success (2xx / 3xx).
    status = resp.status_code
    try:
        body = resp.json()
        body_str = json.dumps(body)
    except Exception:
        body_str = resp.text
    logger.info(
        "component_request %s %s %s → %d (ok)",
        component_id,
        method_upper,
        path,
        status,
    )
    return _format_body(status, body_str)


def build_component_access_tools(
    settings: CentralDeploySettings,
) -> list[Callable[..., Any]]:
    """Return component-access tool(s) for the agent.

    When ``settings.url`` is empty, returns ``[]`` — no tools, no
    system-prompt injection.

    The roster is fetched once at agent construction time and refreshed
    on each tool call if the TTL has expired. Every component — including
    ``github`` — is reached exclusively through the roster: there is no
    per-component fallback, so the roster's skill document is always the
    single, authoritative description of what a component supports.
    """
    if not settings.url:
        return []

    from .roster import fetch_roster

    # We need a mutable container so the closure can refresh the roster
    # between calls.
    _state: dict[str, Any] = {"entries": []}
    _creds = settings.component_credentials

    async def _refresh() -> None:
        _state["entries"] = await fetch_roster(settings)

    async def component_request(
        component_id: str,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        max_response_chars: int | None = None,
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
            max_response_chars: Optional per-call truncation limit for the
                response body.  When omitted the configured default
                (component_response_max_chars) is used.  Set to a small
                value (e.g. 2000) to get a compact summary of a large
                resource like a ticket history; follow up with a larger
                limit (or omit it) to read the full response.

        Returns:
            The component's HTTP status code and response body (truncated
            if very long), or an error message.

        """
        # Refresh the roster on every call (TTL-gated internally).
        await _refresh()
        limit = (
            max_response_chars
            if max_response_chars is not None
            else settings.component_response_max_chars
        )
        return await _component_request_impl(
            _state["entries"],
            component_id,
            method,
            path,
            json_body,
            read_response_max_chars=limit,
            component_credentials=_creds,
        )

    return [component_request]

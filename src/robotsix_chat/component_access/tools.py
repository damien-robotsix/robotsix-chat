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

# Default per-request HTTP timeout (seconds).  Overridden at tool-construction
# time by the configured ``CentralDeploySettings.component_request_timeout``.
_DEFAULT_REQUEST_TIMEOUT = 60.0


def _build_component_retry_config(request_timeout: float) -> RetryConfig:
    """Build the RetryConfig for component calls.

    ``stop_after_delay`` is set to 3x the per-request timeout so the
    retry loop terminates in bounded wall-clock time even when every
    attempt exhausts its full HTTP timeout.
    """
    return RetryConfig(
        max_retries=2,
        backoff_base=1.0,
        backoff_cap=10.0,
        jitter_factor=0.5,
        stop_after_delay=request_timeout * 3,
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
    params: dict[str, str] | None = None,
    read_response_max_chars: int = _TRUNCATE_LENGTH,
    component_credentials: dict[str, Any] | None = None,
    component_fallbacks: dict[str, str] | None = None,
    request_timeout: float = _DEFAULT_REQUEST_TIMEOUT,
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
        msg = (
            f"Error: unknown component_id '{component_id}'. "
            f"Known components: {', '.join(known) if known else '(none)'}."
        )
        msg += (
            " To add a fallback for this component, set "
            f"central_deploy.component_fallbacks.{component_id} "
            "to its base URL in your config file "
            '(e.g. "http://mill:8080").'
        )
        return msg

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
    headers: dict[str, str] = {
        "Accept": "application/json",
        # Prevent transparent gzip/deflate on the response so that
        # proxies which already decompress the body but forward a
        # Content-Encoding header don't trigger zlib errors.
        "Accept-Encoding": "identity",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    def _safe_text(response: httpx.Response) -> str:
        """Read ``response.text``, falling back to empty string on any error.

        Catches zlib decompression errors caused by proxies that forward
        a ``Content-Encoding: gzip`` header on an already-decompressed body.
        """
        try:
            return response.text
        except Exception:
            logger.warning(
                "component_request %s response body read failed for %s %s — "
                "may be a Content-Encoding mismatch (proxy double-decompression)",
                component_id,
                method_upper,
                path,
                exc_info=True,
            )
            return ""

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
        header_name = auth_meta.get("header_name", "")
        token = creds.header_token.get_secret_value() if creds is not None else ""
        if not (header_name and token):
            return (
                f"Error: component '{component_id}' requires a "
                f"{header_name or '?'} header but no token is available — "
                f"central_deploy.component_credentials.{component_id}."
                "header_token is not set."
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
        if not body_str:
            body_str = "(empty response body)"
        elif len(body_str) > limit:
            body_str = body_str[:limit] + (
                f"\n\n... (truncated at {limit} chars, original length {len(body_str)})"
            )
        return f"HTTP {status}\n{body_str}"

    async with httpx.AsyncClient(
        timeout=request_timeout, follow_redirects=True
    ) as client:
        retry_client = RetryClient(
            client,
            config=_build_component_retry_config(request_timeout),
        )
        try:
            resp = await retry_client.request(
                method_upper,
                url,
                params=params,
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
                body_str = _safe_text(exc.response)
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
                body_str = _safe_text(exc.response)
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

            # DNS / connection failure — try a baked-in fallback URL if one
            # is configured for this component.  This keeps CI diagnostics
            # and other critical call paths working when central-deploy's
            # internal compose network is unreachable.
            is_connection_error = isinstance(
                exc,
                (
                    httpx.ConnectError,
                    httpx.ConnectTimeout,
                ),
            ) or "Name or service not known" in str(exc)
            fallback_base = (component_fallbacks or {}).get(component_id, "")
            if is_connection_error and fallback_base:
                fallback_url = f"{fallback_base.rstrip('/')}/{path.lstrip('/')}"
                logger.info(
                    "component_request %s connection failed, retrying with fallback %s",
                    component_id,
                    fallback_base,
                )
                try:
                    async with httpx.AsyncClient(
                        timeout=request_timeout, follow_redirects=True
                    ) as fallback_client:
                        fallback_resp = await fallback_client.request(
                            method_upper,
                            fallback_url,
                            params=params,
                            headers=headers,
                            json=json_body,
                            auth=auth_arg,
                        )
                    fallback_status = fallback_resp.status_code
                    try:
                        fallback_body = fallback_resp.json()
                        fallback_body_str = json.dumps(fallback_body)
                    except Exception:
                        fallback_body_str = _safe_text(fallback_resp)
                    logger.info(
                        "component_request %s %s %s → %d (ok, via fallback)",
                        component_id,
                        method_upper,
                        path,
                        fallback_status,
                    )
                    return _format_body(fallback_status, fallback_body_str)
                except Exception as fallback_exc:
                    logger.warning(
                        "component_request %s fallback retry also failed: %s",
                        component_id,
                        fallback_exc,
                    )

            # Build a hint for DNS / connection failures so the agent
            # knows to try direct tools that bypass central-deploy.
            hint = ""
            if is_connection_error:
                hint = (
                    "\n\nThis component is unreachable via central-deploy "
                    "(DNS / connection error). If direct tools are available "
                    "for this component (e.g. fetch_job_log, "
                    "fetch_workflow_run_annotations, check_workflow_run for "
                    "GitHub API operations), prefer those — they bypass "
                    "central-deploy and use the GitHub App installation token "
                    "directly."
                )

            return (
                f"Error calling {component_id} {method_upper} {path}: "
                f"{str(exc) or type(exc).__name__}{hint}"
            )

    # Success (2xx / 3xx).
    status = resp.status_code
    try:
        body = resp.json()
        body_str = json.dumps(body)
    except Exception:
        body_str = _safe_text(resp)
    logger.info(
        "component_request %s %s %s → %d (ok)",
        component_id,
        method_upper,
        path,
        status,
    )
    return _format_body(status, body_str)


def _coerce_json_object(
    value: dict[str, Any] | str | None, param: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Accept a JSON-encoded string where a JSON object is expected.

    Weaker fallback-tier models (the keyed pydantic-ai path) sometimes pass
    ``json_body``/``params`` as a *string* of JSON instead of an object.
    pydantic-ai validates tool arguments against the schema BEFORE the tool
    body runs, and two failed validations kill the whole turn with
    ``UnexpectedModelBehavior: Tool 'component_request' exceeded max retries``.
    Widening the annotation to accept ``str`` moves the problem into the tool,
    where a bad payload becomes a plain error string the model can react to.

    Returns ``(object, None)`` on success or ``(None, error_message)``.
    """
    if value is None or isinstance(value, dict):
        return value, None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        return None, (
            f"Error: {param} must be a JSON object (got an undecodable string: {exc})."
        )
    if not isinstance(decoded, dict):
        return None, (
            f"Error: {param} must be a JSON object, got {type(decoded).__name__}."
        )
    return decoded, None


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

    _timeout = settings.component_request_timeout

    async def component_request(
        component_id: str,
        method: str,
        path: str,
        json_body: dict[str, Any] | str | None = None,
        params: dict[str, str] | str | None = None,
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
                A JSON-encoded string is also accepted and decoded.
            params: Optional query-string parameters as key/value pairs
                (e.g. ``{"limit": "5", "state": "open"}``).
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
        # The wrapper must never raise an exception — pydantic-ai treats
        # any raised exception from a tool callable as a retryable failure,
        # burning agent-level retries until "exceeded max retries count".
        # A swallowed exception surfaces as a clear error string instead.
        json_body, body_err = _coerce_json_object(json_body, "json_body")
        if body_err:
            return body_err
        params_obj, params_err = _coerce_json_object(params, "params")
        if params_err:
            return params_err
        params = (
            {k: str(v) for k, v in params_obj.items()}
            if params_obj is not None
            else None
        )
        try:
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
                params=params,
                read_response_max_chars=limit,
                component_credentials=_creds,
                component_fallbacks=settings.component_fallbacks,
                request_timeout=_timeout,
            )
        except Exception as exc:
            logger.error(
                "component_request %s %s %s unexpected error: %s",
                component_id,
                method,
                path,
                exc,
                exc_info=True,
            )
            return (
                f"Error calling {component_id} {method.upper()} {path}: "
                f"{type(exc).__name__}: {exc}"
            )

    return [component_request]

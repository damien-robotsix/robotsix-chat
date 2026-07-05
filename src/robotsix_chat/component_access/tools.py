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

if TYPE_CHECKING:
    from robotsix_chat.config import CentralDeploySettings

logger = logging.getLogger(__name__)

_TRUNCATE_LENGTH = 8000


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

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method_upper == "GET":
                resp = await client.get(url, headers=headers)
            elif method_upper == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                resp = await client.request(
                    method_upper, url, headers=headers, json=json_body
                )
    except Exception as exc:
        return f"Error calling {component_id} {method_upper} {path}: {exc}"

    # Try to parse JSON body; fall back to text.
    try:
        body = resp.json()
        body_str = json.dumps(body)
    except Exception:
        body_str = resp.text

    if len(body_str) > _TRUNCATE_LENGTH:
        body_str = body_str[:_TRUNCATE_LENGTH] + (
            f"\n\n... (truncated at {_TRUNCATE_LENGTH} chars, "
            f"original length {len(body_str)})"
        )

    return f"HTTP {resp.status_code}\n{body_str}"


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

"""Shared helpers used across multiple route modules.

These are small, standalone utilities that multiple endpoint files depend on.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse


def _sse_frame(payload: object) -> bytes:
    """Return an SSE ``data:`` frame with a JSON-serialised *payload*."""
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _parse_json_body(request: Request) -> dict[str, Any] | JSONResponse:
    """Parse and type-guard a request's JSON body.

    Returns the parsed ``dict`` on success, or a ``JSONResponse`` error
    ready to return directly from an endpoint.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError, ValueError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    return body


def _get_session_id(request: Request) -> str | JSONResponse:
    """Extract ``session_id`` from query params with ``client_id`` fallback.

    Returns the session id string on success, or a ``JSONResponse`` error
    ready to return directly from an endpoint.
    """
    session_id = request.query_params.get("session_id")
    if not session_id:
        session_id = request.query_params.get("client_id")
    if not session_id:
        return JSONResponse(
            {"error": "session_id query parameter is required"}, status_code=400
        )
    return session_id


async def health_endpoint(_request: Request) -> JSONResponse:
    """Liveness probe — returns 200 ``{"status": "ok"}``."""
    return JSONResponse({"status": "ok"})


async def ui_endpoint(request: Request) -> HTMLResponse:
    """Serve the self-contained browser chat UI at ``GET /``."""
    from .. import _load_ui_html  # lazy import for patchability

    timeout = request.app.state.idle_timeout_minutes
    return HTMLResponse(_load_ui_html(timeout))

"""Shared helpers used across multiple route modules.

These are small, standalone utilities that multiple endpoint files depend on.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)


def build_transcript(turns: Iterable[tuple[str, str]], *, max_len: int = 2000) -> str:
    """Build a compact conversation transcript from (user, assistant) pairs.

    Assistant replies longer than *max_len* are truncated with an ellipsis.
    """
    parts: list[str] = []
    for user_msg, asst_msg in turns:
        parts.append(f"User: {user_msg}")
        if asst_msg:
            truncated = (
                asst_msg[:max_len] + "\u2026" if len(asst_msg) > max_len else asst_msg
            )
            parts.append(f"Assistant: {truncated}")
    return "\n".join(parts)


def _sse_frame(payload: object) -> bytes:
    """Return an SSE ``data:`` frame with a JSON-serialised *payload*."""
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _parse_json_body(request: Request) -> dict[str, Any]:
    """Parse and type-guard a request's JSON body.

    Returns the parsed ``dict`` on success, or raises ``HTTPException``
    with status 400 on parse or type errors.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError, ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")

    return body


def _get_session_id(request: Request) -> str:
    """Extract ``session_id`` from query params with ``client_id`` fallback.

    Returns the session id string on success, or raises ``HTTPException``
    with status 400 when neither param is present.
    """
    session_id = request.query_params.get("session_id")
    if not session_id:
        session_id = request.query_params.get("client_id")
    if not session_id:
        raise HTTPException(
            status_code=400, detail="session_id query parameter is required"
        )
    return session_id


async def health_endpoint(request: Request) -> JSONResponse:
    """Liveness probe — 200 ``{"status": "ok", "memory": {...}}``.

    Stays ``status: ok`` (a memory freeze must not fail the liveness probe and
    have the orchestrator kill the container — the store's own guarded
    self-restart handles recovery), but embeds the memory backend's health so a
    frozen store is externally observable (``memory.degraded``).
    """
    payload: dict[str, object] = {"status": "ok"}
    try:
        memory = getattr(request.app.state, "memory", None)
        status_fn = getattr(memory, "status", None)
        if callable(status_fn):
            snapshot = status_fn()
            # Only embed a real (JSON-serialisable) status mapping — guards
            # against a mock/None backend serialising into a 500.
            if isinstance(snapshot, dict):
                payload["memory"] = snapshot
    except Exception:  # never let health reporting raise (probe must stay 200)
        logger.debug("health: memory status unavailable", exc_info=True)
    return JSONResponse(payload)


async def ui_endpoint(request: Request) -> HTMLResponse:
    """Serve the self-contained browser chat UI at ``GET /``."""
    from .. import _load_ui_html  # lazy import for patchability

    timeout = request.app.state.idle_timeout_minutes
    return HTMLResponse(_load_ui_html(timeout))

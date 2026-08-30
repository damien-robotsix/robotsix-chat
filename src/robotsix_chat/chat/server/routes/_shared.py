"""Shared helpers used across multiple route modules.

These are small, standalone utilities that multiple endpoint files depend on.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

# Re-exported: the transcript builder lives with the summariser so it can
# render the per-turn actions log without a routes import cycle.
from robotsix_chat.chat.summarize import build_transcript as build_transcript

# Re-exported: the transcript builder lives with the summariser so it can
# render the per-turn actions log without a routes import cycle.

logger = logging.getLogger(__name__)

# Heuristic length threshold (chars) above which a response that also
# matches a truncation pattern is considered likely truncated.
_TRUNCATION_LENGTH_THRESHOLD = 1500

# Patterns that suggest an LLM response was cut off by an output-length
# limit.  Each pattern is applied to the last ~120 chars of the text.
_TRUNCATION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r",\s*\.\.\.\s*$"),  # "foo, bar, ..."
    re.compile(r"\.\.\.\s*$"),  # "and then we saw..."
    re.compile(r",\s*$"),  # trailing comma mid-list
    re.compile(r"^\s*#\d+[,;]?\s*$", re.MULTILINE),
    # last line is a bare "#123" or "#123,"
]

_TRUNCATION_NOTE = (
    "\n\n[Note: the response above may have been cut off by an output "
    "length limit.  If you need the complete list, ask me to provide it "
    "as a separate artifact or narrow your query to a smaller scope.]"
)


def _detect_truncation(text: str) -> str | None:
    """Return a truncation-note suffix if *text* appears truncated, else ``None``.

    Applied after the LLM response is fully received; the heuristic only
    fires when the text is long *and* its tail matches a known truncation
    pattern (``...``, trailing comma, bare ``#NNN`` line, etc.).
    """
    if len(text) < _TRUNCATION_LENGTH_THRESHOLD:
        return None
    tail = text[-120:]
    for pat in _TRUNCATION_PATTERNS:
        if pat.search(tail):
            return _TRUNCATION_NOTE
    return None


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
    """Liveness probe — 200 ``{"status": "ok", "memory": {...}, "health": {...}}``.

    Stays ``status: ok`` (a subsystem freeze must not fail the liveness probe
    and have the orchestrator kill the container), but embeds the memory
    backend's health and the periodic health-check snapshot so degradation is
    externally observable.
    """
    payload: dict[str, object] = {"status": "ok"}
    # Memory backend status (existing).
    try:
        memory = getattr(request.app.state, "memory", None)
        status_fn = getattr(memory, "status", None)
        if callable(status_fn):
            snapshot = status_fn()
            if isinstance(snapshot, dict):
                payload["memory"] = snapshot
    except Exception:
        logger.debug("health: memory status unavailable", exc_info=True)
    # Periodic health-check snapshot (new).
    try:
        health_status = getattr(request.app.state, "health_status", None)
        if health_status is not None:
            payload["health"] = {
                "overall": health_status.overall.value,
                "last_run": health_status.last_run,
                "checks": [
                    {
                        "name": c.name,
                        "status": c.status.value,
                        "message": c.message,
                        "details": c.details,
                    }
                    for c in health_status.checks
                ],
            }
    except Exception:
        logger.debug("health: health status unavailable", exc_info=True)
    return JSONResponse(payload)


async def ui_endpoint(request: Request) -> HTMLResponse:
    """Serve the self-contained browser chat UI at ``GET /``."""
    from .. import _load_ui_html  # lazy import for patchability

    timeout = request.app.state.idle_timeout_minutes
    return HTMLResponse(_load_ui_html(timeout))

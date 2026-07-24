"""HTTP endpoints for the diagnostic event store.

``POST /diagnostics/events`` — record a diagnostic event (called by the mill
  or other external pipeline stages).
``GET /diagnostics/events`` — list recorded events, optionally filtered by
  category.
"""

from __future__ import annotations

from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.diagnostics import DiagnosticStore

from ._shared import _parse_json_body


async def diagnostics_create_endpoint(request: Request) -> JSONResponse:
    """Record a diagnostic event into the store.

    Expects a JSON body with:
        category (str, required): failure category (e.g. ``CI_FAILURE``)
        message (str, required): human-readable description
        details (dict, optional): structured context

    Returns the created event as JSON (201).
    """
    store: DiagnosticStore | None = request.app.state.diagnostic_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="diagnostic store is not available",
        )

    body = await _parse_json_body(request)

    category: Any = body.get("category")
    if not isinstance(category, str) or not category.strip():
        raise HTTPException(status_code=400, detail="'category' (str) is required")

    message: Any = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=400, detail="'message' (str) is required")

    details: Any = body.get("details")
    if details is not None and not isinstance(details, dict):
        raise HTTPException(status_code=400, detail="'details' must be a JSON object")

    bundle = store.record_event(
        category=category.strip(),
        message=message.strip(),
        details=details,
    )
    return JSONResponse(
        {
            "id": bundle.id,
            "category": bundle.category,
            "message": bundle.message,
            "details": bundle.details,
            "created_at": bundle.created_at,
        },
        status_code=201,
    )


async def diagnostics_list_endpoint(request: Request) -> JSONResponse:
    """List diagnostic events, optionally filtered by category.

    Query params:
        category (str, optional): filter by category (e.g. ``CI_FAILURE``)
    """
    store: DiagnosticStore | None = request.app.state.diagnostic_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="diagnostic store is not available",
        )

    category = request.query_params.get("category", "")
    events = store.list_events(category)
    return JSONResponse(
        [
            {
                "id": e.id,
                "category": e.category,
                "message": e.message,
                "details": e.details,
                "created_at": e.created_at,
            }
            for e in events
        ]
    )

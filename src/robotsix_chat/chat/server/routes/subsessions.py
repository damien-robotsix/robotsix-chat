"""Subsession endpoints — list, get, transcript, message, close."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse

from ._shared import _get_session_id, _parse_json_body

if TYPE_CHECKING:
    from robotsix_chat.subsessions import (
        ParentDelivery,
        SubsessionInfo,
        SubsessionRegistry,
    )


def _get_subsession_registry(
    request: Request,
) -> SubsessionRegistry | JSONResponse:
    """Return the wired registry, or a ready-to-return 503 error response."""
    registry: SubsessionRegistry | None = request.app.state.subsession_registry
    if registry is None:
        return JSONResponse(
            {"error": "subsessions feature not enabled"}, status_code=503
        )
    return registry


def _resolve_subsession(
    request: Request,
) -> tuple[SubsessionRegistry, SubsessionInfo] | JSONResponse:
    """Resolve the subsession registry and look up the requested subsession.

    Returns ``(registry, info)`` on success, or a ready-to-return
    ``JSONResponse`` (503 or 404) when the lookup fails.
    """
    registry = _get_subsession_registry(request)
    if isinstance(registry, JSONResponse):
        return registry
    sub_id = request.path_params["sub_id"]
    info = registry.get(sub_id)
    if info is None:
        return JSONResponse(
            {"error": "unknown subsession", "subsession_id": sub_id},
            status_code=404,
        )
    return (registry, info)


async def subsessions_list_endpoint(request: Request) -> JSONResponse:
    """Return the whole subsession tree for a chat session.

    ``GET /subsessions?session_id=...`` returns ``{"subsessions": [...]}``
    — every subsession owned by the session (all kinds, all depths, all
    statuses; terminal entries are retained for a while so the panel can
    show recent history), sorted by ``created_at`` ascending, without
    transcripts.  Tolerates ``client_id`` as a legacy fallback.

    Returns 400 when ``session_id`` is missing and 503 when the
    subsession feature is not wired.
    """
    session_id = _get_session_id(request)
    if isinstance(session_id, JSONResponse):
        return session_id
    registry = _get_subsession_registry(request)
    if isinstance(registry, JSONResponse):
        return registry

    return JSONResponse(
        {
            "subsessions": [
                info.snapshot() for info in registry.list_for_owner(session_id)
            ]
        }
    )


async def subsessions_get_endpoint(request: Request) -> JSONResponse:
    """Return one subsession's full snapshot including its transcript.

    ``GET /subsessions/{sub_id}`` returns the snapshot dict plus a
    ``"transcript"`` list.  404 when the id is unknown.
    """
    result = _resolve_subsession(request)
    if isinstance(result, JSONResponse):
        return result
    _registry, info = result
    return JSONResponse(info.snapshot(with_transcript=True))


async def subsessions_transcript_endpoint(request: Request) -> JSONResponse:
    """Return one subsession's transcript only.

    ``GET /subsessions/{sub_id}/transcript`` returns
    ``{"subsession_id": ..., "transcript": [{role, text, timestamp}, ...]}``.
    404 when the id is unknown.
    """
    result = _resolve_subsession(request)
    if isinstance(result, JSONResponse):
        return result
    _registry, info = result
    return JSONResponse(
        {
            "subsession_id": info.id,
            "transcript": [entry.as_dict() for entry in info.transcript],
        }
    )


async def subsessions_message_endpoint(request: Request) -> JSONResponse:
    """Queue a user message for a running subsession.

    ``POST /subsessions/{sub_id}/message`` with body ``{"text": "..."}``
    enqueues the message (role ``"user"``) for delivery at the
    subsession's next turn boundary and returns 202
    ``{"subsession_id": ..., "status": "queued"}``.

    Returns 400 for a missing/empty ``text``, 404 for an unknown id, and
    409 when the subsession is no longer active.
    """
    result = _resolve_subsession(request)
    if isinstance(result, JSONResponse):
        return result
    registry, info = result

    body = await _parse_json_body(request)
    if isinstance(body, JSONResponse):
        return body
    text = body.get("text")
    if not text or not isinstance(text, str):
        return JSONResponse(
            {"error": "'text' field is required and must be a non-empty string"},
            status_code=400,
        )

    if not registry.enqueue_message(info.id, "user", text):
        return JSONResponse(
            {"error": "subsession is not active", "subsession_id": info.id},
            status_code=409,
        )
    return JSONResponse({"subsession_id": info.id, "status": "queued"}, status_code=202)


async def subsessions_close_endpoint(request: Request) -> JSONResponse:
    """Close a subsession from the UI (user-initiated external close).

    ``POST /subsessions/{sub_id}/close`` cancels the worker, marks the
    subsession closed, delivers a best-effort summary to its parent
    conversation, and returns ``{"subsession_id": ..., "closed": true,
    "summary": "..."}``.

    Idempotent: an already-terminal subsession returns 200 with
    ``"closed": false`` and its current status.  404 for an unknown id.
    """
    result = _resolve_subsession(request)
    if isinstance(result, JSONResponse):
        return result
    registry, info = result

    closed = registry.cancel_and_close(
        info.id, reason="closed by user", closed_by="user"
    )
    if closed is None:
        return JSONResponse(
            {
                "subsession_id": info.id,
                "closed": False,
                "status": info.status.value,
            }
        )

    delivery: ParentDelivery | None = request.app.state.subsession_delivery
    if delivery is not None:
        await delivery.deliver_summary(
            closed, closed.summary or "", closed.close_reason or "closed"
        )
    return JSONResponse(
        {"subsession_id": info.id, "closed": True, "summary": closed.summary}
    )

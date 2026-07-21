"""Session endpoints — list, create, delete, close, and history."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.chat.conversation import ConversationStore

from ._shared import _get_session_id, _parse_json_body, build_transcript
from .chat import ChatAgent

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from robotsix_chat.subsessions import SubsessionRegistry


def _cleanup_session(session_id: str, request: Request) -> int:
    """Close every subsession owned by *session_id* (best-effort).

    Returns the number of subsessions closed; ``0`` when the subsession
    registry is not wired.
    """
    registry: SubsessionRegistry | None = request.app.state.subsession_registry
    if registry is None:
        return 0
    return registry.close_all_for_owner(session_id, reason="session closed")


def _require_owner_id(request: Request) -> str:
    """Extract and validate the ``owner_id`` query parameter.

    Returns the owner id, or raises an ``HTTPException(400)``
    when missing.
    """
    owner_id = request.query_params.get("owner_id")
    if not owner_id:
        raise HTTPException(
            status_code=400,
            detail="owner_id query parameter is required",
        )
    return owner_id


async def history_endpoint(request: Request) -> JSONResponse:
    """Return a session's stored conversation history as JSON.

    ``GET /history?session_id=...`` returns ``{"turns": [[user, assistant], ...]}``.
    Also tolerates ``client_id`` as a legacy fallback (treated as ``session_id``).
    """
    session_id = _get_session_id(request)

    store: ConversationStore = request.app.state.conversation_store
    turns = store.history(session_id)
    return JSONResponse({"turns": turns})


async def sessions_list_endpoint(request: Request) -> JSONResponse:
    """List all sessions for an owner.

    ``GET /sessions?owner_id=...`` returns::

        {
          "sessions": [
            {
              "session_id": "...", "title": "...",
              "last_active": 1.0, "turn_count": 3, "closed": false
            },
            ...
          ],
          "active_session_id": "..."
        }

    Sorted by ``last_active`` descending.  If the owner has no sessions, a
    default empty session is lazily created and returned (so the list is
    never empty).
    """
    owner_id = _require_owner_id(request)

    store: ConversationStore = request.app.state.conversation_store
    sessions, active_id = store.list_sessions(owner_id)

    # Annotate autonomous sessions so the UI can render the [AUTONOMOUS] badge.
    runner = request.app.state.autonomous_runner
    if runner is not None:
        for s in sessions:
            sid = s.get("session_id")
            if isinstance(sid, str) and runner.is_autonomous(sid):
                s["autonomous"] = True
                state = runner.get_state(sid)
                if state is not None:
                    s["autonomous_state"] = state.value
                aq = runner.get_session(sid)
                if aq is not None:
                    s["autonomous_plan_text"] = aq.plan_text
                    s["autonomous_turn_count"] = aq.auto_turn_count
                    s["autonomous_max_turns"] = (
                        request.app.state.settings.autonomous.max_auto_turns
                    )
                    s["autonomous_session_color"] = (
                        request.app.state.settings.autonomous.session_color
                    )

    return JSONResponse({"sessions": sessions, "active_session_id": active_id})


async def sessions_create_endpoint(request: Request) -> JSONResponse:
    """Create a new empty session for an owner.

    ``POST /sessions`` with body ``{"owner_id": "..."}`` returns::

        {"session_id": "...", "title": "New chat", "last_active": 1.0, "turn_count": 0}

    Pass ``"autonomous": true`` to create an autonomous session instead
    (requires ``autonomous.enabled`` in config).

    The new session is marked as the owner's active session.
    """
    body = await _parse_json_body(request)

    owner_id = body.get("owner_id")
    if not owner_id or not isinstance(owner_id, str):
        raise HTTPException(
            status_code=400,
            detail="'owner_id' field is required and must be a string",
        )

    autonomous = body.get("autonomous", False)
    runner = request.app.state.autonomous_runner

    if autonomous:
        if runner is None:
            raise HTTPException(
                status_code=404,
                detail="autonomous sessions are not enabled",
            )
        aq = runner.create_session(owner_id)
        return JSONResponse(
            {
                "session_id": aq.session_id,
                "title": "Autonomous chat",
                "last_active": 0.0,
                "turn_count": 0,
                "autonomous": True,
                "autonomous_state": aq.state.value,
            }
        )

    store: ConversationStore = request.app.state.conversation_store
    session = store.create_session(owner_id)
    return JSONResponse(session)


async def sessions_delete_endpoint(request: Request) -> JSONResponse:
    """Close (delete) a session and stop its background work.

    ``DELETE /sessions/{session_id}?owner_id=...`` closes every subsession
    owned by the session, deletes the session and its history, and returns::

        {
          "deleted": true,
          "active_session_id": "...",   # the owner's new active session
          "subsessions_closed": 1
        }

    ``owner_id`` is required (query param).  Returns 404 when the session is
    not found / not owned by *owner_id*.  Closing subsessions is best-effort
    and runs even when the conversation delete is a no-op (so orphaned work
    can still be cleaned up).
    """
    session_id = request.path_params["session_id"]
    owner_id = _require_owner_id(request)

    # 1. Close the session's subsessions.
    subsessions_closed = _cleanup_session(session_id, request)

    # 2. Delete the conversation/session itself.
    store: ConversationStore = request.app.state.conversation_store

    # Capture history before deletion (for the feedback run).
    deletion_turns = store.history(session_id)

    result = store.delete_session(owner_id, session_id)

    if not result.get("deleted"):
        return JSONResponse(
            {
                "error": "session not found",
                "session_id": session_id,
                "subsessions_closed": subsessions_closed,
            },
            status_code=404,
        )

    # Schedule a feedback run for the deleted session.
    feedback_runner = request.app.state.feedback_runner
    if feedback_runner is not None and deletion_turns:
        feedback_runner.schedule("session_end", session_id, deletion_turns)

    return JSONResponse(
        {
            "deleted": True,
            "active_session_id": result.get("active_session_id", ""),
            "subsessions_closed": subsessions_closed,
        }
    )


async def sessions_close_endpoint(request: Request) -> JSONResponse:
    """Close (mark as closed) a session and stop its background work.

    ``POST /sessions/{session_id}/close?owner_id=...`` closes every
    subsession owned by the session, marks the session as ``closed``
    (preventing it from spawning new work), and returns::

        {
          "closed": true,
          "session_id": "...",
          "subsessions_closed": 1
        }

    ``owner_id`` is required (query param).  Returns 404 when the session is
    not found / not owned by *owner_id*.  Closing subsessions is best-effort
    and runs even when the session is not found (so orphaned work can still
    be cleaned up).

    Unlike ``DELETE /sessions/{session_id}``, closing preserves the session's
    history and metadata — the session cannot spawn new background work but
    its conversation history remains available.
    """
    session_id = request.path_params["session_id"]
    owner_id = _require_owner_id(request)

    # 1. Close the session's subsessions.
    subsessions_closed = _cleanup_session(session_id, request)

    # 2. Mark the session as closed in the conversation store.
    store: ConversationStore = request.app.state.conversation_store
    result = store.close_session(owner_id, session_id)

    if not result.get("closed"):
        return JSONResponse(
            {
                "error": "session not found",
                "session_id": session_id,
                "subsessions_closed": subsessions_closed,
            },
            status_code=404,
        )

    # Schedule a feedback run for the closed session.
    feedback_runner = request.app.state.feedback_runner
    if feedback_runner is not None:
        turns = store.history(session_id)
        if turns:
            feedback_runner.schedule("session_end", session_id, turns)

    return JSONResponse(
        {
            "closed": True,
            "session_id": session_id,
            "subsessions_closed": subsessions_closed,
        }
    )


async def sessions_approve_endpoint(request: Request) -> JSONResponse:
    """Approve an autonomous session's plan and begin execution.

    ``POST /sessions/{session_id}/approve?owner_id=...``

    Validates ``owner_id`` (returns 403 on mismatch) and transitions the
    session from ``awaiting_approval`` to ``executing``.  The server then
    auto-continues execution in the background.

    Returns 200 ``{"approved": true}`` on success.
    Returns 403 ``{"error": "owner_id mismatch"}`` when the owner does not
        match the session's owner.
    Returns 409 ``{"error": "session is in state <state>, not awaiting_approval"}``
        when the session is not awaiting approval.
    Returns 404 when no autonomous runner is configured or the session is
        unknown.
    """
    from robotsix_chat.autonomous import AutonomousRunner

    session_id = request.path_params["session_id"]
    owner_id = _require_owner_id(request)

    runner: AutonomousRunner | None = request.app.state.autonomous_runner
    if runner is None:
        return JSONResponse(
            {"error": "autonomous sessions are not enabled"},
            status_code=404,
        )

    ok, reason = runner.approve(owner_id, session_id)
    if not ok:
        if reason == "owner_id mismatch":
            return JSONResponse({"error": reason}, status_code=403)
        if "not found" in reason:
            return JSONResponse({"error": reason}, status_code=404)
        return JSONResponse({"error": reason}, status_code=409)

    return JSONResponse({"approved": True})


async def sessions_reject_endpoint(request: Request) -> JSONResponse:
    """Reject an autonomous session's plan and reset to subject selection.

    ``POST /sessions/{session_id}/reject?owner_id=...``

    Validates ``owner_id`` (returns 403 on mismatch) and resets the session
    from ``awaiting_approval`` to ``selecting_subject``.

    Returns 200 ``{"rejected": true}`` on success.
    Returns 403 ``{"error": "owner_id mismatch"}`` when the owner does not
        match the session's owner.
    Returns 409 ``{"error": "session is in state <state>, not awaiting_approval"}``
        when the session is not awaiting approval.
    Returns 404 when no autonomous runner is configured or the session is
        unknown.
    """
    from robotsix_chat.autonomous import AutonomousRunner

    session_id = request.path_params["session_id"]
    owner_id = _require_owner_id(request)

    runner: AutonomousRunner | None = request.app.state.autonomous_runner
    if runner is None:
        return JSONResponse(
            {"error": "autonomous sessions are not enabled"},
            status_code=404,
        )

    ok, reason = runner.reject(owner_id, session_id)
    if not ok:
        if reason == "owner_id mismatch":
            return JSONResponse({"error": reason}, status_code=403)
        if "not found" in reason:
            return JSONResponse({"error": reason}, status_code=404)
        return JSONResponse({"error": reason}, status_code=409)

    return JSONResponse({"rejected": True})


async def summary_endpoint(request: Request) -> JSONResponse:
    """Generate a quick, free-form conversation summary.

    ``POST /summary`` with JSON body ``{"session_id": "..."}`` returns
    ``{"summary": "..."}`` — a short plain-text summary, empty when there
    is no history yet.

    Deliberately unconstrained: an earlier version forced a fixed 5-field
    JSON schema, which made the cheap summary-tier model (reasoning
    nominally disabled) ramble at length trying to satisfy the schema and
    frequently run past its token budget before producing valid JSON —
    slow and often empty. Plain prose has no schema to fail.

    The summary is regenerated from the full server-side history on
    every call — callers should invoke it after each assistant turn to
    keep the display current.
    """
    agent: ChatAgent = request.app.state.summary_agent
    store: ConversationStore = request.app.state.conversation_store

    body = await _parse_json_body(request)

    session_id = body.get("session_id")
    if not session_id or not isinstance(session_id, str):
        raise HTTPException(status_code=400, detail="session_id is required")

    turns = store.history(session_id)
    if not turns:
        return JSONResponse({"summary": ""})

    transcript = build_transcript(turns)

    _summary_prompt = (
        "Write a brief, plain-text summary of the conversation below — "
        "what it's about, what's currently in progress, and anything "
        "blocking or worth remembering. If any unresolved operator "
        "prerequisites are identified (actions only a human can take, "
        "such as provisioning credentials, granting permissions, or "
        "updating infrastructure), call them out explicitly so the "
        "operator is reminded. A few sentences of prose. No headers, "
        "no bullet points, no JSON, no markdown fences — just plain "
        "text.\n\nConversation:\n"
    )
    prompt = f"{_summary_prompt}{transcript}\n\nSummary:"

    reply_parts: list[str] = []
    try:
        async for token in agent.stream(
            prompt,
            history=None,
            session_id=None,
            client_id=None,
        ):
            reply_parts.append(token)
    except Exception:
        logger.exception("Summary generation failed")
        raise HTTPException(
            status_code=500, detail="summary generation failed"
        ) from None

    return JSONResponse({"summary": "".join(reply_parts).strip()})

"""Session endpoints — list, create, delete, close, and history."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from typing import TYPE_CHECKING

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.chat.conversation import ConversationStore

from ._shared import _get_session_id, _parse_json_body
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
    if isinstance(session_id, JSONResponse):
        return session_id

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
    return JSONResponse({"sessions": sessions, "active_session_id": active_id})


async def sessions_create_endpoint(request: Request) -> JSONResponse:
    """Create a new empty session for an owner.

    ``POST /sessions`` with body ``{"owner_id": "..."}`` returns::

        {"session_id": "...", "title": "New chat", "last_active": 1.0, "turn_count": 0}

    The new session is marked as the owner's active session.
    """
    body = await _parse_json_body(request)
    if isinstance(body, JSONResponse):
        return body

    owner_id = body.get("owner_id")
    if not owner_id or not isinstance(owner_id, str):
        return JSONResponse(
            {"error": "'owner_id' field is required and must be a string"},
            status_code=400,
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

    return JSONResponse(
        {
            "closed": True,
            "session_id": session_id,
            "subsessions_closed": subsessions_closed,
        }
    )


async def summary_endpoint(request: Request) -> JSONResponse:
    """Generate a structured conversation summary.

    ``POST /summary`` with JSON body ``{"session_id": "..."}`` returns a
    JSON object with string fields ``purpose``, ``pending_work``,
    ``pending_questions``, ``blockers``, and ``relevant_info``.  Each
    field is an empty string when nothing relevant was found.

    The summary is regenerated from the full server-side history on
    every call — callers should invoke it after each assistant turn to
    keep the display current.
    """
    agent: ChatAgent = request.app.state.summary_agent
    store: ConversationStore = request.app.state.conversation_store

    body = await _parse_json_body(request)
    if isinstance(body, JSONResponse):
        return body

    session_id = body.get("session_id")
    if not session_id or not isinstance(session_id, str):
        return JSONResponse({"error": "session_id is required"}, status_code=400)

    turns = store.history(session_id)
    if not turns:
        return JSONResponse(
            {
                "purpose": "",
                "pending_work": "",
                "pending_questions": "",
                "blockers": "",
                "relevant_info": "",
            }
        )

    # Build a compact transcript.  Long assistant replies are truncated
    # to keep the prompt within reasonable bounds.
    transcript_parts: list[str] = []
    for user_msg, asst_msg in turns:
        transcript_parts.append(f"User: {user_msg}")
        if asst_msg:
            truncated = asst_msg[:2000] + "…" if len(asst_msg) > 2000 else asst_msg
            transcript_parts.append(f"Assistant: {truncated}")
    transcript = "\n".join(transcript_parts)

    _SUMMARY_PROMPT = (
        "Summarize the following conversation between a user and an AI "
        "assistant.  Return ONLY a single JSON object (no markdown fences, "
        "no other text) with exactly these five string fields:\n\n"
        '- "purpose": what the session is about / the goal of the current '
        "work.  Empty string if unclear.\n"
        '- "pending_work": what is currently in progress or still to be '
        "done.  Empty string if none.\n"
        '- "pending_questions": any question the assistant is waiting on '
        "the user to answer.  Empty string if none.\n"
        '- "blockers": anything blocking the current task.  Empty string '
        "if none.\n"
        '- "relevant_info": any other relevant information (links, ticket '
        "ids, background jobs running, etc.).  Empty string if none.\n\n"
        "Conversation:\n"
    )
    prompt = f"{_SUMMARY_PROMPT}{transcript}\n\nJSON summary:"

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
        return JSONResponse({"error": "summary generation failed"}, status_code=500)

    reply = "".join(reply_parts).strip()

    # Parse JSON from the reply.  The agent may wrap it in markdown fences
    # or add explanatory text — try several extraction strategies.
    summary: dict[str, object] = {}
    try:
        summary = json.loads(reply)
    except json.JSONDecodeError:
        # Try to extract from markdown code fences.
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", reply, re.DOTALL)
        if fence_match:
            with contextlib.suppress(json.JSONDecodeError):
                summary = json.loads(fence_match.group(1))
    if not summary:
        # Last resort: find the first JSON object containing "purpose".
        brace_match = re.search(r'\{[^{}]*"purpose"[^{}]*\}', reply, re.DOTALL)
        if brace_match:
            with contextlib.suppress(json.JSONDecodeError):
                summary = json.loads(brace_match.group())

    # Ensure all expected fields exist with string values.
    _SUMMARY_FIELDS = (
        "purpose",
        "pending_work",
        "pending_questions",
        "blockers",
        "relevant_info",
    )
    result: dict[str, str] = {}
    for field in _SUMMARY_FIELDS:
        value = summary.get(field, "")
        result[field] = str(value) if value else ""

    return JSONResponse(result)

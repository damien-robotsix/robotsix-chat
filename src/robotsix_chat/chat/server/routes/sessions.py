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
    runner = request.app.state.autonomous_runner

    # Never lazily create a browser default session for any autonomous
    # pseudo-owner — that husk would surface in the operator's merged list as
    # an empty, un-closable "New chat".  The runner owns these sessions.
    create_default = runner is None or not runner.is_autonomous_owner(owner_id)
    sessions, active_id = store.list_sessions(owner_id, create_default=create_default)

    # Annotate autonomous sessions so the UI can render the [AUTONOMOUS] badge.
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

    runner = request.app.state.autonomous_runner
    is_autonomous = runner is not None and runner.is_autonomous(session_id)
    is_autonomous_owner = runner is not None and runner.is_autonomous_owner(owner_id)

    # 1. Close the session's subsessions.
    subsessions_closed = _cleanup_session(session_id, request)

    # 2. Delete the conversation/session itself.
    store: ConversationStore = request.app.state.conversation_store

    # Capture history before deletion (for the feedback run).
    deletion_turns = store.history(session_id)

    # Never spawn an empty husk under the autonomous pseudo-owner; the runner
    # starts a fresh, properly-tracked replacement below instead.
    result = store.delete_session(
        owner_id, session_id, create_replacement=not is_autonomous_owner
    )

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

    # -- session carryover persistence ------------------------------------
    # Save an action-plan summary to the knowledge store so the assistant
    # can pick up pending work in a new session.
    await _persist_carryover(request, store, session_id, owner_id)

    # Autonomous cleanup: forget the runner's record and auto-restart so the
    # operator always has one live autonomous run (auto-restart always).
    if is_autonomous and runner is not None:
        runner.forget_session(session_id)
        runner.ensure_active_session(owner_id)

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

    runner = request.app.state.autonomous_runner
    is_autonomous = runner is not None and runner.is_autonomous(session_id)

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

    # -- session carryover persistence ------------------------------------
    # Save an action-plan summary to the knowledge store so the assistant
    # can pick up pending work in a new session.
    await _persist_carryover(request, store, session_id, owner_id)

    # Autonomous cleanup: forget the runner's record (the store keeps the
    # closed history) and auto-restart so the operator always has one live
    # autonomous run (auto-restart always).
    if is_autonomous and runner is not None:
        runner.forget_session(session_id)
        runner.ensure_active_session(owner_id)

    return JSONResponse(
        {
            "closed": True,
            "session_id": session_id,
            "subsessions_closed": subsessions_closed,
        }
    )


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
            trace_name="session-summary",
        ):
            reply_parts.append(token)
    except Exception:
        logger.exception("Summary generation failed")
        raise HTTPException(
            status_code=500, detail="summary generation failed"
        ) from None

    return JSONResponse({"summary": "".join(reply_parts).strip()})


# -- session carryover -----------------------------------------------------

# Well-known note topic for session carryover in the knowledge store.
_CARRYOVER_TOPIC = "session-carryover"


async def _persist_carryover(
    request: Request,
    store: ConversationStore,
    session_id: str,
    owner_id: str,
) -> None:
    """Generate a carryover action-plan summary and save it to the knowledge store.

    The summary captures what the assistant was planning to do next so a
    new session can pick up pending work.  A no-op when knowledge is
    disabled, the summary agent is missing, or the session has no turns.
    """
    knowledge_store = request.app.state.knowledge_store
    if knowledge_store is None:
        return

    summary_agent: ChatAgent | None = request.app.state.summary_agent
    if summary_agent is None:
        return

    turns = store.history(session_id)
    if not turns:
        return

    # Avoid circular import: _generate_carryover_summary lives in .chat
    from .chat import _generate_carryover_summary

    try:
        summary = await _generate_carryover_summary(summary_agent, turns)
    except Exception:
        logger.exception(
            "Carryover summary generation failed for session %s", session_id
        )
        return

    if not summary:
        return

    try:
        # Find any existing carryover note for this owner and update it,
        # or create a new one if none exists.  We use list-filtered-by-topic
        # to locate the note since KnowledgeStore.add() generates a random id.
        existing = knowledge_store.list(_CARRYOVER_TOPIC)
        if existing:
            knowledge_store.update(existing[0].id, summary)
            logger.debug(
                "Updated carryover note %s for owner %s",
                existing[0].id,
                owner_id,
            )
        else:
            entry = knowledge_store.add(_CARRYOVER_TOPIC, summary)
            logger.debug(
                "Created carryover note %s for owner %s",
                entry.id,
                owner_id,
            )
    except Exception:
        logger.exception("Failed to persist carryover note for owner %s", owner_id)


# ---------------------------------------------------------------------------
# Autonomous session definition endpoints (read-only reflection of config)
# ---------------------------------------------------------------------------


async def autonomous_definitions_list_endpoint(request: Request) -> JSONResponse:
    """List autonomous session definitions.

    ``GET /autonomous/definitions`` returns::

        {
          "definitions": [
            {
              "name": "default",
              "prompt": "",
              "trigger_type": "periodic",
              "trigger_interval_seconds": 45.0,
              "enabled": true,
              "owner_id": "autonomous",
              "active_session_id": "..."
            },
            ...
          ]
        }

    Each definition is annotated with its derived ``owner_id`` and the
    ``active_session_id`` of any currently-open session for that definition
    (``null`` when none is active).
    """
    runner = request.app.state.autonomous_runner
    if runner is None:
        return JSONResponse(
            {"error": "autonomous sessions are not enabled"}, status_code=404
        )

    definitions = []
    for name in runner.definition_names:
        owner_id = runner.owner_id_for_definition(name)
        defn = runner.get_definition(name) or {}
        active_session_id = runner.active_session_id_for_definition(name)
        # Build refinement summary when a refinement store is present.
        refinement = None
        ref_store = runner.refinement_store
        if ref_store is not None:
            ref_state = ref_store.get_state(name, defn.get("prompt", ""))
            pending_count = sum(1 for e in ref_state.entries if e.status == "pending")
            accepted_count = sum(1 for e in ref_state.entries if e.status == "accepted")
            refinement = {
                "self_refine": defn.get("self_refine", False),
                "self_refine_require_approval": defn.get(
                    "self_refine_require_approval", False
                ),
                "effective_prompt": ref_store.effective_prompt(
                    name, defn.get("prompt", "")
                ),
                "base_prompt": defn.get("prompt", ""),
                "accepted_addendum": ref_state.accepted_addendum,
                "pending_count": pending_count,
                "accepted_count": accepted_count,
                "total_entries": len(ref_state.entries),
            }
        definitions.append(
            {
                "name": name,
                "prompt": defn.get("prompt", ""),
                "trigger_type": defn.get("trigger_type", "periodic"),
                "trigger_interval_seconds": defn.get("trigger_interval_seconds", 45.0),
                "enabled": defn.get("enabled", True),
                "self_refine": defn.get("self_refine", False),
                "self_refine_require_approval": defn.get(
                    "self_refine_require_approval", False
                ),
                "owner_id": owner_id,
                "active_session_id": active_session_id,
                "refinement": refinement,
            }
        )

    return JSONResponse({"definitions": definitions})


async def autonomous_definitions_run_endpoint(request: Request) -> JSONResponse:
    """Manually trigger a one-shot run of an autonomous session definition.

    ``POST /autonomous/definitions/{name}/run`` queues a new run for the
    named definition (if enabled).  Returns::

        {
          "started": true,
          "session_id": "...",
          "definition_name": "..."
        }

    Returns ``404`` when the definition is not found, ``409`` when the
    definition already has an active session.
    """
    name = request.path_params["name"]
    runner = request.app.state.autonomous_runner
    if runner is None:
        return JSONResponse(
            {"error": "autonomous sessions are not enabled"}, status_code=404
        )

    if runner.get_definition(name) is None:
        return JSONResponse({"error": f"unknown definition {name!r}"}, status_code=404)

    owner_id = runner.owner_id_for_definition(name)

    # Check for an existing open session.
    active_id = runner.active_session_id_for_definition(name)
    if active_id is not None:
        aq = runner.get_session(active_id)
        state_value = aq.state.value if aq is not None else "unknown"
        return JSONResponse(
            {
                "error": (
                    f"definition {name!r} already has an active session "
                    f"({active_id}, state={state_value})"
                ),
                "session_id": active_id,
            },
            status_code=409,
        )

    # Start a new session.
    aq = runner.ensure_active_session(
        owner_id,
        schedule_kickoff=True,
        definition_name=name,
    )

    return JSONResponse(
        {
            "started": True,
            "session_id": aq.session_id,
            "definition_name": name,
        }
    )


# -- autonomous refinement endpoints ---------------------------------------


async def autonomous_refinements_list_endpoint(request: Request) -> JSONResponse:
    """List refinement entries for an autonomous session definition.

    ``GET /autonomous/definitions/{name}/refinements`` returns::

        {
          "definition_name": "...",
          "base_prompt": "...",
          "accepted_addendum": "...",
          "effective_prompt": "...",
          "entries": [
            {
              "id": "...",
              "timestamp": 1234567890.0,
              "status": "accepted",
              "feedback_summary": "...",
              "proposed_addendum": "...",
              "previous_addendum": "...",
              "session_id": "..."
            },
            ...
          ]
        }
    """
    name = request.path_params["name"]
    runner = request.app.state.autonomous_runner
    if runner is None:
        return JSONResponse(
            {"error": "autonomous sessions are not enabled"}, status_code=404
        )

    ref_store = runner.refinement_store
    if ref_store is None:
        return JSONResponse(
            {"error": "refinement store is not available"}, status_code=404
        )

    defn = runner.get_definition(name)
    if defn is None:
        return JSONResponse({"error": f"unknown definition {name!r}"}, status_code=404)

    base_prompt = defn.get("prompt", "")
    state = ref_store.get_state(name, base_prompt)
    entries = [
        {
            "id": e.id,
            "timestamp": e.timestamp,
            "status": e.status,
            "feedback_summary": e.feedback_summary,
            "proposed_addendum": e.proposed_addendum,
            "previous_addendum": e.previous_addendum,
            "session_id": e.session_id,
        }
        for e in state.entries
    ]

    return JSONResponse(
        {
            "definition_name": name,
            "base_prompt": state.base_prompt,
            "accepted_addendum": state.accepted_addendum,
            "effective_prompt": ref_store.effective_prompt(name, base_prompt),
            "entries": entries,
        }
    )


async def autonomous_refinements_accept_endpoint(request: Request) -> JSONResponse:
    """Accept a pending refinement.

    ``POST /autonomous/definitions/{name}/refinements/{refinement_id}/accept``

    Returns ``{"accepted": true}`` on success, ``404`` when the refinement
    is not found or is not pending.
    """
    name = request.path_params["name"]
    refinement_id = request.path_params["refinement_id"]
    runner = request.app.state.autonomous_runner
    if runner is None:
        return JSONResponse(
            {"error": "autonomous sessions are not enabled"}, status_code=404
        )

    ref_store = runner.refinement_store
    if ref_store is None:
        return JSONResponse(
            {"error": "refinement store is not available"}, status_code=404
        )

    if ref_store.accept_refinement(name, refinement_id):
        return JSONResponse({"accepted": True})
    return JSONResponse(
        {"error": f"refinement {refinement_id!r} not found or not pending"},
        status_code=404,
    )


async def autonomous_refinements_reject_endpoint(request: Request) -> JSONResponse:
    """Reject a pending refinement.

    ``POST /autonomous/definitions/{name}/refinements/{refinement_id}/reject``

    Returns ``{"rejected": true}`` on success, ``404`` when the refinement
    is not found or is not pending.
    """
    name = request.path_params["name"]
    refinement_id = request.path_params["refinement_id"]
    runner = request.app.state.autonomous_runner
    if runner is None:
        return JSONResponse(
            {"error": "autonomous sessions are not enabled"}, status_code=404
        )

    ref_store = runner.refinement_store
    if ref_store is None:
        return JSONResponse(
            {"error": "refinement store is not available"}, status_code=404
        )

    if ref_store.reject_refinement(name, refinement_id):
        return JSONResponse({"rejected": True})
    return JSONResponse(
        {"error": f"refinement {refinement_id!r} not found or not pending"},
        status_code=404,
    )


async def autonomous_refinements_reset_endpoint(request: Request) -> JSONResponse:
    """Reset all refinements for a definition — clears the addendum.

    ``POST /autonomous/definitions/{name}/refinements/reset``

    Returns ``{"reset": true}`` on success.
    """
    name = request.path_params["name"]
    runner = request.app.state.autonomous_runner
    if runner is None:
        return JSONResponse(
            {"error": "autonomous sessions are not enabled"}, status_code=404
        )

    ref_store = runner.refinement_store
    if ref_store is None:
        return JSONResponse(
            {"error": "refinement store is not available"}, status_code=404
        )

    ref_store.reset_refinements(name)
    return JSONResponse({"reset": True})

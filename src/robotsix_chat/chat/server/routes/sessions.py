"""Session endpoints — list, create, delete, close, and history."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.autonomous.models import AutonomousState
from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import session_model_frame
from robotsix_chat.config.autonomous_models import (
    DEFAULT_TRIGGER_INTERVAL_SECONDS,
)
from robotsix_chat.config.constants import (
    FRONTIER_MODEL_LEVEL,
    level_display_name,
    level_needs_api_key,
)
from robotsix_chat.subsessions.registry import OWNER_CLOSED_REASON

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
    return registry.close_all_for_owner(session_id, reason=OWNER_CLOSED_REASON)


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

    When the session has been compacted (idle summarisation), the response
    also carries ``compacted_summary`` (the summary text) and
    ``compacted_turn_index`` (how many leading ``turns`` it covers), so the
    UI can open the session on its summary and keep the covered turns
    behind an explicit expand — exactly what the agent itself sees.
    Sessions that were never compacted get neither key.
    """
    session_id = _get_session_id(request)

    store: ConversationStore = request.app.state.conversation_store
    turns = store.history(session_id)
    payload: dict[str, object] = {"turns": turns}
    session = store.get_session(session_id)
    summary = getattr(session, "compacted_summary", None)
    index = getattr(session, "compacted_turn_index", 0)
    if isinstance(summary, str) and summary and isinstance(index, int) and index > 0:
        payload["compacted_summary"] = summary
        payload["compacted_turn_index"] = min(index, len(turns))
    return JSONResponse(payload)


async def sessions_list_endpoint(request: Request) -> JSONResponse:
    """List all sessions for an owner.

    ``GET /sessions?owner_id=...`` returns::

        {
          "sessions": [
            {
              "session_id": "...", "title": "...",
              "last_active": 1.0, "turn_count": 3, "closed": false,
              "model_level": 3, "model_name": "...",
              "model_escalated": false
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
                    s["autonomous_turn_count"] = aq.auto_turn_count

    # Resolve each session's effective model for the UI badge. ``model_level``
    # is None until the agent escalates the session, so fall back to the
    # server's configured chat level and mark only real escalations.
    configured = getattr(request.app.state, "chat_model_level", None)
    for s in sessions:
        raw = s.get("model_level")
        escalated = isinstance(raw, int)
        level = raw if escalated else configured
        if isinstance(level, int):
            s["model_level"] = level
            s["model_name"] = level_display_name(level)
            s["model_escalated"] = escalated
        else:
            # No agent on this app (test doubles) — omit rather than guess.
            s.pop("model_level", None)

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

    # 1. Close the session's subsessions.
    subsessions_closed = _cleanup_session(session_id, request)

    # 2. Delete the conversation/session itself.
    store: ConversationStore = request.app.state.conversation_store

    # Resolve the concrete owner scope before deleting.  ``GET /sessions``
    # merges the bootstrap ``autonomous`` owner with every per-preset
    # ``autonomous:<name>`` sub-scope into a single list, and the client
    # routes the delete back to the bootstrap owner it fetched the list
    # from.  A completed preset session actually lives under its sub-scope
    # owner, so a delete aimed at the bootstrap owner is a silent no-op
    # (404) and the card reappears on the next refresh.  Re-target the
    # delete to the session's real owner only when the caller supplied the
    # merged bootstrap owner, so ordinary operator sessions keep their
    # ownership check intact.
    delete_owner_id = owner_id
    if (
        runner is not None
        and owner_id == runner.bootstrap_owner
        and runner.is_autonomous_owner(owner_id)
    ):
        actual_owner = store.owner_for_session(session_id)
        if actual_owner is not None and runner.is_autonomous_owner(actual_owner):
            delete_owner_id = actual_owner

    is_autonomous_owner = runner is not None and runner.is_autonomous_owner(
        delete_owner_id
    )

    # Capture history before deletion (for the feedback run).
    deletion_turns = store.history(session_id)

    # Never spawn an empty husk under the autonomous pseudo-owner; the runner
    # starts a fresh, properly-tracked replacement below instead.
    result = store.delete_session(
        delete_owner_id, session_id, create_replacement=not is_autonomous_owner
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
    await _persist_carryover(request, store, session_id, delete_owner_id)

    # A session the operator chats with is dual-owned: ``record`` also adopts
    # it under the autonomous scope that drives it, so the session lives in
    # both the operator's and an ``autonomous[:<preset>]`` registry.  The
    # browser's ``GET /sessions`` merges the operator's own scope with every
    # autonomous scope into one list, so the primary delete above (scoped to
    # the operator) leaves the autonomous copy behind — it resurfaces in the
    # merged list on the next refresh and the discard looks like a no-op until
    # a second delete (now routed to the autonomous scope) finally removes it.
    # When a real operator discards the session, also purge it from any
    # autonomous scope that still holds it so a single delete removes it for
    # good.  Deletes routed to an autonomous owner are left untouched so an
    # autonomous run's own retirement never destroys the operator's copy.
    if runner is not None and not is_autonomous_owner:
        for other_owner in store.owner_ids_for(session_id):
            if other_owner != delete_owner_id and runner.is_autonomous_owner(
                other_owner
            ):
                store.delete_session(other_owner, session_id, create_replacement=False)

    # Autonomous cleanup: forget the runner's record and auto-restart so the
    # operator always has one live autonomous run (auto-restart always).
    if is_autonomous and runner is not None:
        was_countdown = runner.get_state(session_id) is AutonomousState.completed
        runner.forget_session(session_id)
        # A completed autonomous session is in its inter-run countdown: the
        # ``_auto_restart`` task scheduled at completion will spawn the fresh
        # session when the next run actually fires.  Deleting it must hide the
        # entry immediately WITHOUT auto-restarting right now — otherwise the
        # card reappears instantly and the discard looks like a no-op.
        if not was_countdown:
            runner.schedule_restart(delete_owner_id)

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
    # closed history) and schedule a throttled restart when needed.
    if is_autonomous and runner is not None:
        was_countdown = runner.get_state(session_id) is AutonomousState.completed
        runner.forget_session(session_id)
        # A completed autonomous session is in its inter-run countdown: the
        # ``_auto_restart`` task scheduled at completion will spawn the fresh
        # session when the next run actually fires.  Closing one must hide the
        # entry immediately WITHOUT restarting right now — otherwise the card
        # reappears instantly and the close looks like a no-op.
        # An executing session closed by the operator restarts after the
        # preset's ``trigger_interval_seconds`` throttle (immediate only for
        # on_close presets).
        if not was_countdown:
            runner.schedule_restart(owner_id)

    return JSONResponse(
        {
            "closed": True,
            "session_id": session_id,
            "subsessions_closed": subsessions_closed,
        }
    )


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
              "trigger_interval_seconds": 3600.0,
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
                "trigger_interval_seconds": defn.get(
                    "trigger_interval_seconds", DEFAULT_TRIGGER_INTERVAL_SECONDS
                ),
                "model_level": defn.get("model_level"),
                "max_runs": defn.get("max_runs", 0),
                "total_runs": runner._total_runs_for(name),
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


# ---------------------------------------------------------------------------
# Per-session model selection
# ---------------------------------------------------------------------------


def _build_model_options(
    request: Request,
) -> tuple[list[dict[str, object]], int | None, dict[str, object]]:
    """Return the selectable model levels, default level, and failover state.

    The list is sourced from robotsix-llmio's tier config (levels 1..
    :data:`FRONTIER_MODEL_LEVEL`), resolved against the provider slot the
    failover tracker currently designates as active — so ``name`` and
    ``provider`` reflect what would actually serve the next turn.  A level
    is ``available`` when its active slot needs no API key (the keyless
    claudeSDK default) or when one is configured.  The ``failover`` dict is
    llmio's :func:`~robotsix_llmio.core.failover.get_failover_status`
    snapshot — the UI's source for the failover badge.
    """
    from robotsix_llmio import default_tier_config
    from robotsix_llmio.core.failover import get_failover_status

    api_key_available = bool(
        getattr(request.app.state, "chat_api_key_available", False)
    )
    default_level = getattr(request.app.state, "chat_model_level", None)
    tier_config = default_tier_config()
    models: list[dict[str, object]] = []
    for level in range(1, FRONTIER_MODEL_LEVEL + 1):
        needs_key = level_needs_api_key(level)
        try:
            provider = tier_config.for_level(level).provider
        except ValueError:  # pragma: no cover - levels are enum-bounded
            provider = ""
        models.append(
            {
                "level": level,
                "name": level_display_name(level),
                "provider": provider,
                "needs_api_key": needs_key,
                "available": (not needs_key) or api_key_available,
            }
        )
    failover = get_failover_status().model_dump(mode="json")
    return (
        models,
        default_level if isinstance(default_level, int) else None,
        failover,
    )


async def models_list_endpoint(request: Request) -> JSONResponse:
    """List the model levels the operator can select for a session.

    ``GET /models`` returns::

        {
          "models": [
            {"level": 1, "name": "...", "provider": "claudeSDK",
             "needs_api_key": false, "available": true},
            ...
          ],
          "default_level": 2,
          "failover": {"active_slot": "default", "failover_active": false,
                       "failover_until": null, ...}
        }

    ``default_level`` is the server's configured chat level — the level a
    session runs at until the operator (or the agent) pins a different one.
    ``failover`` is llmio's provider-failover snapshot; while
    ``failover_active`` is true every level is served by the fallback
    (OpenRouter) slot until ``failover_until``.
    """
    models, default_level, failover = _build_model_options(request)
    return JSONResponse(
        {"models": models, "default_level": default_level, "failover": failover}
    )


async def session_model_set_endpoint(request: Request) -> JSONResponse:
    """Pin a session to a selected model level.

    ``POST /sessions/{session_id}/model`` with body ``{"level": N}`` pins the
    session to level *N* for the rest of its lifetime (applies from the next
    turn).  Returns the resolved model and broadcasts a ``session_model``
    frame on ``GET /events`` so every attached view updates its badge live.

    Rejects an out-of-range level (400) or one that needs an API key the
    server does not have (409), and returns 404 for an unknown session.
    """
    session_id = request.path_params["session_id"]
    body = await _parse_json_body(request)

    raw = body.get("level")
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise HTTPException(
            status_code=400,
            detail="'level' field is required and must be an integer",
        )
    level = raw
    if level < 1 or level > FRONTIER_MODEL_LEVEL:
        raise HTTPException(
            status_code=400,
            detail=f"'level' must be between 1 and {FRONTIER_MODEL_LEVEL}",
        )

    api_key_available = bool(
        getattr(request.app.state, "chat_api_key_available", False)
    )
    if level_needs_api_key(level) and not api_key_available:
        raise HTTPException(
            status_code=409,
            detail=f"model level {level} requires an API key that is not configured",
        )

    store: ConversationStore = request.app.state.conversation_store
    if not store.set_model_level(session_id, level):
        raise HTTPException(status_code=404, detail="session not found")

    name = level_display_name(level)
    default_level = getattr(request.app.state, "chat_model_level", None)
    # "escalated" drives the UI badge that flags a session running off the
    # server default; an explicit selection back to the default is not one.
    escalated = not isinstance(default_level, int) or level != default_level

    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is not None:
        event_bus.publish(
            session_id,
            session_model_frame(
                session_id=session_id,
                model_level=level,
                model_name=name,
                escalated=escalated,
            ),
        )

    return JSONResponse(
        {
            "session_id": session_id,
            "model_level": level,
            "model_name": name,
            "escalated": escalated,
        }
    )

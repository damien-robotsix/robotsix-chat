"""Session endpoints — list, create, delete, close, and history."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import session_model_frame
from robotsix_chat.config.constants import (
    FRONTIER_MODEL_LEVEL,
    level_display_name,
    level_needs_api_key,
)
from robotsix_chat.periodic import PERIODIC_OWNER
from robotsix_chat.subsessions.registry import OWNER_CLOSED_REASON

from ._shared import _get_session_id, _parse_json_body
from .chat import ChatAgent

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from robotsix_chat.subsessions import SubsessionRegistry


def _app_tier_config(request: Request) -> Any:
    """Chat's own llmio tier config (incl. ``llmio_tier_overrides``).

    Every place that renders a model NAME for a level must resolve it
    against this config, not llmio's baked defaults — an operator override
    (e.g. binding fallback level 2 to a different snapshot) otherwise shows
    one model in the session badge while another actually serves the turn.
    Returns ``None`` when tier resolution is unavailable (test doubles) so
    ``level_display_name`` falls back to the defaults.
    """
    try:
        from robotsix_llmio.config import load_tier_config

        from robotsix_chat.llm.agent import _merge_tier_overrides

        overrides = getattr(request.app.state, "llmio_tier_overrides", None)
        return load_tier_config(_merge_tier_overrides(overrides, None))
    except Exception:  # pragma: no cover - display-only resolution
        return None


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

    ``GET /history?session_id=...`` returns the session's turns as
    ``{"turns": [[user, assistant], ...]}``. Also tolerates ``client_id`` as a
    legacy fallback (treated as ``session_id``).

    Response fields (all except ``turns`` are optional compaction metadata):

    - ``turns`` — ``array[array[string]]``: ``[user, assistant]`` message pairs,
      newest last.
    - ``compacted_summary`` — ``string``: summary text of the leading turns.
      Present only when the session was compacted *and* a usable summary exists.
    - ``compacted_turn_index`` — ``integer``: how many leading ``turns`` the
      summary covers. Present whenever the session has advanced past compaction
      (``> 0``).
    - ``compacted_summary_missing`` — ``boolean``: ``true`` exactly when the
      session has advanced past compaction (``compacted_turn_index > 0``) but no
      usable ``compacted_summary`` is available (it was never persisted, or
      failed to persist and is empty). Appears only in that case; clients can
      use it to render a fallback (e.g. a banner / graceful degradation)
      instead of a bare compacted session with no explanation.

    Compacted session with a usable summary (flag absent)::

        {
          "turns": [["After the summary", "A reply"], ["More", "And more"]],
          "compacted_summary": "Earlier we agreed on X.",
          "compacted_turn_index": 2
        }

    Compacted session with no usable summary (flag present)::

        {
          "turns": [["After the summary", "A reply"], ["More", "And more"]],
          "compacted_turn_index": 2,
          "compacted_summary_missing": true
        }

    A session that was never compacted returns only ``turns`` — neither
    ``compacted_summary`` nor ``compacted_turn_index`` nor
    ``compacted_summary_missing`` is present.
    """
    session_id = _get_session_id(request)

    store: ConversationStore = request.app.state.conversation_store
    turns = store.history(session_id)
    payload: dict[str, object] = {"turns": turns}
    session = store.get_session(session_id)
    summary = getattr(session, "compacted_summary", None)
    index = getattr(session, "compacted_turn_index", 0)
    if isinstance(index, int) and index > 0:
        if isinstance(summary, str) and summary:
            payload["compacted_summary"] = summary
            payload["compacted_turn_index"] = min(index, len(turns))
        else:
            # Session advanced past compaction but has no usable summary —
            # signal it so clients can render a fallback instead of a bare
            # compacted session with no explanation.
            payload["compacted_summary_missing"] = True
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

    # Never lazily create a browser default session under the periodic owner
    # — the scheduler creates its sessions when a preset fires, and an empty
    # husk would surface in the sidebar as an un-closable "New chat".
    sessions, active_id = store.list_sessions(
        owner_id, create_default=owner_id != PERIODIC_OWNER
    )

    # Resolve each session's effective model for the UI badge. ``model_level``
    # is None until the agent escalates the session, so fall back to the
    # server's configured chat level and mark only real escalations.
    configured = getattr(request.app.state, "chat_model_level", None)
    tier_config = _app_tier_config(request)
    for s in sessions:
        raw = s.get("model_level")
        escalated = isinstance(raw, int)
        level = raw if escalated else configured
        if isinstance(level, int):
            s["model_level"] = level
            s["model_name"] = level_display_name(level, tier_config)
            s["model_escalated"] = escalated
        else:
            # No agent on this app (test doubles) — omit rather than guess.
            s.pop("model_level", None)

    return JSONResponse({"sessions": sessions, "active_session_id": active_id})


async def sessions_create_endpoint(request: Request) -> JSONResponse:
    """Create a new empty session for an owner.

    ``POST /sessions`` with body ``{"owner_id": "..."}`` returns::

        {"session_id": "...", "title": "New chat", "last_active": 1.0, "turn_count": 0}

    The new session is marked as the owner's active session.
    """
    body = await _parse_json_body(request)

    owner_id = body.get("owner_id")
    if not owner_id or not isinstance(owner_id, str):
        raise HTTPException(
            status_code=400,
            detail="'owner_id' field is required and must be a string",
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
    delete_owner_id = owner_id

    # Capture history before deletion (for the feedback run).
    deletion_turns = store.history(session_id)

    # Never spawn an empty husk under the periodic owner — the scheduler
    # creates sessions only when a preset fires.
    result = store.delete_session(
        delete_owner_id, session_id, create_replacement=owner_id != PERIODIC_OWNER
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

    # -- session carryover persistence ------------------------------------
    # Save an action-plan summary to the knowledge store so the assistant
    # can pick up pending work in a new session.
    await _persist_carryover(request, store, session_id, owner_id)

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
# Periodic session preset endpoints (read-only reflection of config + manual run)
# ---------------------------------------------------------------------------


async def periodic_definitions_list_endpoint(request: Request) -> JSONResponse:
    """List periodic session presets.

    ``GET /periodic/definitions`` returns::

        {
          "definitions": [
            {
              "name": "calendar-agenda",
              "initial_prompt": "...",
              "schedule_interval_seconds": 86400.0,
              "anchor_utc": "2026-09-03T06:00:00+00:00",
              "model_level": null,
              "enabled": true,
              "last_fired_at": 1756725600.0,
              "last_session_id": "...",
              "runs": 12
            },
            ...
          ]
        }
    """
    scheduler = request.app.state.periodic_scheduler
    if scheduler is None:
        return JSONResponse(
            {"error": "periodic sessions are not enabled"}, status_code=404
        )

    definitions = []
    for name in scheduler.definition_names:
        defn = scheduler.get_definition(name)
        if defn is None:
            continue
        state = scheduler.state_for(name)
        definitions.append(
            {
                "name": defn.name,
                "initial_prompt": defn.initial_prompt,
                "schedule_interval_seconds": defn.schedule_interval_seconds,
                # Serialise to an ISO 8601 string: the raw datetime is not
                # JSON-serialisable, and the string matches what the operator
                # configured (``JSONResponse`` has no datetime encoder).
                "anchor_utc": (
                    defn.anchor_utc.isoformat() if defn.anchor_utc is not None else None
                ),
                "model_level": defn.model_level,
                "enabled": defn.enabled,
                "last_fired_at": state.get("last_fired_at"),
                "last_session_id": state.get("last_session_id"),
                "runs": state.get("runs", 0),
            }
        )

    return JSONResponse({"definitions": definitions})


async def periodic_definitions_run_endpoint(request: Request) -> JSONResponse:
    """Manually fire a periodic session preset now.

    ``POST /periodic/definitions/{name}/run`` creates a fresh session seeded
    with the preset's initial prompt (the same thing a scheduled firing
    does). Returns::

        {"started": true, "session_id": "...", "definition_name": "..."}

    Returns ``404`` when the preset is unknown, ``409`` when the preset's
    previous session is still processing a turn.
    """
    name = request.path_params["name"]
    scheduler = request.app.state.periodic_scheduler
    if scheduler is None:
        return JSONResponse(
            {"error": "periodic sessions are not enabled"}, status_code=404
        )
    if scheduler.get_definition(name) is None:
        return JSONResponse({"error": f"unknown definition {name!r}"}, status_code=404)

    session_id = await scheduler.fire(name, manual=True)
    if session_id is None:
        return JSONResponse(
            {
                "error": (
                    f"preset {name!r}'s previous session is still processing a turn"
                )
            },
            status_code=409,
        )
    return JSONResponse(
        {"started": True, "session_id": session_id, "definition_name": name}
    )


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
    from robotsix_llmio.config import load_tier_config
    from robotsix_llmio.core.failover import get_failover_status

    from robotsix_chat.llm.agent import _merge_tier_overrides

    api_key_available = bool(
        getattr(request.app.state, "chat_api_key_available", False)
    )
    default_level = getattr(request.app.state, "chat_model_level", None)
    # Chat's own tier config (incl. any llmio_tier_overrides from settings),
    # so the selector and badge show what actually serves the next turn.
    state_overrides = getattr(request.app.state, "llmio_tier_overrides", None)
    tier_config = load_tier_config(_merge_tier_overrides(state_overrides, None))
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
                "name": level_display_name(level, tier_config),
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

    name = level_display_name(level, _app_tier_config(request))
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

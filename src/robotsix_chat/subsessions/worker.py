"""Subsession worker — spawn validation, the turn loop, and startup resume.

A subsession runs as a single coroutine (`_subsession_worker`) that loops
over **agent turns**.  llmio has no mid-run message injection, so steering
messages (from the parent agent or, for ``user_chat``, from the user) are
queued in the registry inbox and drained at turn boundaries:

* ``task``    — one turn; extra turns only while steering messages arrive.
* ``user_chat`` — turn per inbox batch; waits (cancellable) between turns.
* ``periodic`` — turn per tick; sleeps on the inbox event so a steering
  message wakes it early.  ``NO_CHANGE`` replies are suppressed (not
  delivered to the parent) and auto-stop the loop after N in a row.

Every kind can end itself by calling its ``complete_subsession`` tool —
the tool flips the shared :class:`CloseState`, checked after each turn.
External closes cancel the task (plain asyncio cancellation; the agent's
``finally: handle.close()`` reaps the LLM handle).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from robotsix_chat.chat.events import subsession_result_frame

from .delivery import ParentDelivery
from .models import (
    ACTIVE_STATUSES,
    InboxMessage,
    SubsessionCapacityError,
    SubsessionDepthError,
    SubsessionInfo,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionLevelError,
    SubsessionStatus,
    TranscriptEntry,
)
from .registry import SubsessionRegistry

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.events import EventSink
    from robotsix_chat.chat.server.routes import ChatAgent
    from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)

# Prior turns replayed to the subsession agent are capped so a
# long-running periodic/user_chat subsession cannot grow its own prompt
# without bound.
_MAX_WORKER_HISTORY_TURNS = 20

# Reply sentinel a periodic subsession uses to report "nothing changed".
_NO_CHANGE_SENTINEL = "NO_CHANGE"


def _is_no_change(reply: str) -> bool:
    """Whether *reply* is the periodic no-change sentinel."""
    return reply.strip().upper().startswith(_NO_CHANGE_SENTINEL)


@dataclass(frozen=True)
class SubsessionContext:
    """Identity captured lexically in agent tool closures.

    ``subsession_id is None`` means the agent IS the main chat agent
    (depth 0); its spawned children then have ``parent_id=None`` and
    their summaries deliver straight to the owning chat session.
    """

    owner_session_id: str
    subsession_id: str | None
    depth: int


@dataclass
class CloseState:
    """Mutable close-request holder shared between a worker and its agent.

    The ``complete_subsession`` tool sets :attr:`requested` (and the
    summary); the worker checks it after every turn.
    """

    requested: bool = False
    summary: str | None = None


@dataclass
class SubsessionEnv:
    """Shared runtime dependencies for spawning and running subsessions.

    ``agent_factory(settings, model_level, ctx, close_state)`` must build
    a fully-tooled agent for the subsession: the standard tool suite plus
    the depth-aware subsession tools (spawn/message/close/list when depth
    allows, ``complete_subsession`` always).
    """

    settings: Settings
    registry: SubsessionRegistry
    delivery: ParentDelivery
    conversation_store: ConversationStore
    agent_factory: Callable[[Settings, int, SubsessionContext, CloseState], ChatAgent]
    event_sink: EventSink | None = None
    # Strong refs to worker tasks spawned via spawn_subsession (belt and
    # braces alongside the registry's _running map).
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)


def spawn_subsession(
    *,
    env: SubsessionEnv,
    kind: SubsessionKind,
    owner_session_id: str,
    parent_id: str | None,
    depth: int,
    title: str,
    prompt: str,
    model_level: int,
    interval_seconds: float | None = None,
    include_previous_result: bool = False,
    max_runs: int | None = None,
    sub_id: str | None = None,
    completed_runs: set[int] | None = None,
) -> str:
    """Validate, register, and launch a subsession worker; return its id.

    Raises :class:`SubsessionCapacityError`, :class:`SubsessionDepthError`,
    :class:`SubsessionLevelError`, or :class:`SubsessionIntervalError` on
    invalid requests — the tool layer maps these to polite refusals.

    Idempotent: when *sub_id* is given and already registered (e.g. a
    duplicate resume), the existing worker is left alone and the id is
    returned immediately — no second worker is launched.
    """
    # Idempotency guard: if the subsession already exists (duplicate
    # spawn / resume race), return the existing id without launching
    # a second worker.  Must precede validation so a duplicate resume
    # never fails on capacity / depth / level checks.
    if sub_id is not None and env.registry.get(sub_id) is not None:
        return sub_id

    cfg = env.settings.subsessions
    if env.registry.count_active() >= cfg.max_concurrent:
        raise SubsessionCapacityError(
            f"subsession capacity reached ({cfg.max_concurrent} active)"
        )
    if depth > cfg.max_depth:
        raise SubsessionDepthError(
            f"maximum subsession nesting depth is {cfg.max_depth}"
        )
    _validate_model_level(env.settings, model_level)
    if kind is SubsessionKind.PERIODIC:
        if interval_seconds is None or interval_seconds < cfg.min_interval_seconds:
            raise SubsessionIntervalError(
                f"periodic interval must be >= {cfg.min_interval_seconds} seconds"
            )
    else:
        interval_seconds = None

    info = env.registry.create(
        kind=kind,
        owner_session_id=owner_session_id,
        parent_id=parent_id,
        depth=depth,
        title=title,
        prompt=prompt,
        model_level=model_level,
        interval_seconds=interval_seconds,
        include_previous_result=include_previous_result,
        max_runs=max_runs,
        sub_id=sub_id,
        completed_runs=completed_runs,
    )
    task = asyncio.create_task(_subsession_worker(env, info.id))
    env.registry.attach_task(info.id, task)
    env._tasks.add(task)
    task.add_done_callback(env._tasks.discard)
    return info.id


def _validate_model_level(settings: Settings, model_level: int) -> None:
    """Reject invalid levels and key-bearing levels without a key."""
    from robotsix_chat.config import level_needs_api_key

    if model_level not in (1, 2, 3):
        raise SubsessionLevelError("model_level must be between 1 and 3")
    if (
        level_needs_api_key(model_level)
        and not settings.llmio_api_key.get_secret_value()
    ):
        raise SubsessionLevelError(
            f"model level {model_level} needs an API key which is not "
            "configured — use level 3 or 4"
        )


def _render_turn_input(messages: list[InboxMessage]) -> str:
    """Merge an inbox batch into one turn input, labelled by role."""
    if len(messages) == 1:
        return messages[0].text
    return "\n\n".join(f"[{m.role}] {m.text}" for m in messages)


def _build_periodic_input(
    info: SubsessionInfo,
    previous_result: str | None,
    steering: list[InboxMessage],
) -> str:
    """Compose one periodic tick's turn input."""
    parts = [info.prompt]
    if info.include_previous_result and previous_result is not None:
        parts.append(f"Previous run result:\n{previous_result}")
    if steering:
        parts.append(
            "New instructions received since the last run:\n"
            + _render_turn_input(steering)
        )
    parts.append(
        f"Reply exactly {_NO_CHANGE_SENTINEL} if nothing changed since the "
        "previous run. Call complete_subsession when the monitored "
        "condition reaches a terminal state."
    )
    return "\n\n".join(parts)


async def _run_turn(
    agent: ChatAgent,
    turn_input: str,
    history: list[tuple[str, str]],
    sub_id: str,
) -> str:
    """Run one agent turn and return the reply text."""
    parts = [
        chunk
        async for chunk in agent.stream(
            turn_input,
            history=history[-_MAX_WORKER_HISTORY_TURNS:] or None,
            session_id=sub_id,
            client_id=sub_id,
        )
    ]
    return "".join(parts)


async def _run_task_turn(
    env: SubsessionEnv, sub_id: str, reply: str
) -> list[InboxMessage]:
    """Handle TASK post-turn: drain inbox; return pending messages or close.

    Returns a non-empty list if steering messages arrived mid-turn
    (the worker should continue), or an empty list after closing the
    subsession (the worker should stop).
    """
    pending = env.registry.drain_inbox(sub_id)
    if pending:
        return pending  # a steering message arrived mid-turn
    closed = env.registry.mark_closed(
        sub_id, summary=reply, reason="completed", closed_by="agent"
    )
    if closed is not None:
        await env.delivery.deliver_summary(closed, reply, "completed")
    return []


async def _run_user_chat_turn(env: SubsessionEnv, sub_id: str) -> list[InboxMessage]:
    """Handle USER_CHAT post-turn: wait for inbox, drain, return pending."""
    env.registry.set_status(sub_id, SubsessionStatus.WAITING)
    await env.registry.wait_for_inbox(sub_id, timeout=None)
    return env.registry.drain_inbox(sub_id)


async def _run_periodic_turn(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    reply: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str, int] | None:
    """Handle PERIODIC post-turn: update status, deliver, check limits, sleep.

    Returns ``None`` when the worker should stop (max_runs / auto_stop
    triggered), or ``(pending, previous_result, consecutive_no_change)``
    to continue.
    """
    registry = env.registry
    suppressed = _is_no_change(reply)
    consecutive_no_change = 0 if not suppressed else consecutive_no_change + 1
    runs = info.runs + 1
    if info.interval_seconds is None:  # pragma: no cover - spawn validates
        raise RuntimeError("periodic subsession without an interval")
    registry.set_status(
        sub_id,
        SubsessionStatus.SLEEPING,
        runs=runs,
        next_run_at=registry.now() + info.interval_seconds,
        last_result=reply,
    )
    if not suppressed:
        if env.event_sink is not None:
            env.event_sink.publish(
                info.owner_session_id,
                subsession_result_frame(
                    sub_id,
                    info.kind.value,
                    info.title,
                    runs,
                    reply,
                    info.parent_id,
                ),
            )
        await env.delivery.deliver_result(info, runs, reply)
    previous_result = reply

    if info.max_runs is not None and runs >= info.max_runs:
        summary = f"Reached the {info.max_runs}-run limit. Last: {reply}"
        closed = registry.mark_closed(
            sub_id, summary=summary, reason="max_runs", closed_by="system"
        )
        if closed is not None:
            await env.delivery.deliver_summary(closed, summary, "max_runs")
        return None

    no_change_cap = env.settings.subsessions.auto_stop_no_change_runs
    if consecutive_no_change >= no_change_cap:
        summary = f"Auto-stopped after {no_change_cap} consecutive no-change runs."
        closed = registry.mark_closed(
            sub_id,
            summary=summary,
            reason="no_change_auto_stop",
            closed_by="system",
        )
        if closed is not None:
            await env.delivery.deliver_summary(closed, summary, "no_change_auto_stop")
        return None

    # Sleep until the next tick, waking early on a steering message.
    woke = await registry.wait_for_inbox(sub_id, timeout=info.interval_seconds)
    pending = registry.drain_inbox(sub_id) if woke else []
    return pending, previous_result, consecutive_no_change


async def _subsession_worker(env: SubsessionEnv, sub_id: str) -> None:
    """Drive one subsession to a terminal state (see module docstring)."""
    registry = env.registry
    info = registry.get(sub_id)
    if info is None:  # pragma: no cover - spawn always registers first
        return
    close_state = CloseState()
    ctx = SubsessionContext(
        owner_session_id=info.owner_session_id,
        subsession_id=sub_id,
        depth=info.depth,
    )
    try:
        agent = env.agent_factory(env.settings, info.model_level, ctx, close_state)
        history: list[tuple[str, str]] = []
        previous_result: str | None = None
        consecutive_no_change = 0
        first_turn = True
        pending: list[InboxMessage] = []

        while True:
            # -- verify the subsession is still alive --------------------
            info = registry.get(sub_id)
            if info is None or not info.is_active:
                logger.warning(
                    "Subsession %s is no longer active — worker exiting.", sub_id
                )
                return

            registry.set_status(sub_id, SubsessionStatus.RUNNING)
            if info.kind is SubsessionKind.PERIODIC:
                # -- run guard: prevent duplicate execution of run N -----
                next_run = info.runs + 1
                if not registry.claim_run(sub_id, next_run):
                    logger.warning(
                        "Run %d of subsession %s was already executed; "
                        "skipping duplicate.",
                        next_run,
                        sub_id,
                    )
                    # Advance the run counter so we don't loop forever
                    # on the same run number.
                    registry.set_status(
                        sub_id,
                        SubsessionStatus.SLEEPING,
                        runs=next_run,
                        next_run_at=(registry.now() + (info.interval_seconds or 60.0)),
                        last_result=info.last_result,
                    )
                    # Sleep until the next tick (or a steering message).
                    woke = await registry.wait_for_inbox(
                        sub_id, timeout=info.interval_seconds
                    )
                    pending = registry.drain_inbox(sub_id) if woke else []
                    continue

                steering = [] if first_turn else pending
                turn_input = _build_periodic_input(info, previous_result, steering)
            elif first_turn:
                turn_input = info.prompt
            else:
                turn_input = _render_turn_input(pending)
            first_turn = False

            reply = await _run_turn(agent, turn_input, history, sub_id)
            history.append((turn_input, reply))
            # Inbox messages were transcripted at enqueue time; only the
            # assistant side is appended here.
            registry.append_transcript(sub_id, "assistant", reply)

            # -- agent-requested close (any kind) --------------------------
            if close_state.requested:
                summary = close_state.summary or reply
                closed = registry.mark_closed(
                    sub_id, summary=summary, reason="completed", closed_by="agent"
                )
                if closed is not None:
                    await env.delivery.deliver_summary(closed, summary, "completed")
                else:
                    # Parent link is gone — the close failed.  Append a
                    # transcript message so the agent learns the truth.
                    registry.append_transcript(
                        sub_id,
                        "system",
                        "complete_subsession failed: this subsession is no "
                        "longer active (its tree record may have been lost).",
                    )
                return

            # -- kind-specific continuation --------------------------------
            if info.kind is SubsessionKind.TASK:
                pending = await _run_task_turn(env, sub_id, reply)
                if pending:
                    continue
                return

            if info.kind is SubsessionKind.USER_CHAT:
                pending = await _run_user_chat_turn(env, sub_id)
                continue

            # -- PERIODIC ---------------------------------------------------
            result = await _run_periodic_turn(
                env, info, sub_id, reply, previous_result, consecutive_no_change
            )
            if result is None:
                return
            pending, previous_result, consecutive_no_change = result
            # Reap any orphaned timers on each scheduler tick.
            env.registry.reap_orphans()

    except asyncio.CancelledError:
        # External close already set the terminal state and (if wanted)
        # delivered the summary — nothing to do here.
        raise
    except Exception as exc:
        logger.exception("Subsession %s worker failed", sub_id)
        failed = registry.fail(sub_id, error=str(exc))
        if failed is not None:
            await env.delivery.deliver_summary(
                failed, failed.summary or f"Failed: {exc}", "failed"
            )


def resume_subsessions(env: SubsessionEnv) -> None:
    """Startup hook: resume periodic subsessions, report interrupted ones.

    * ``periodic`` entries that were active at shutdown are respawned
      under their original id with the remaining run budget.
    * active ``task`` / ``user_chat`` entries cannot be resumed (their
      in-flight agent state is gone) — they are re-registered as
      ``INTERRUPTED`` and a summary is delivered to the owning main
      session (a nested parent's worker is also gone pre-resume).
    * terminal entries are re-registered as-is so the UI keeps its
      recent-history view after a restart.
    """
    for entry in env.registry.load_persisted():
        try:
            _resume_entry(env, entry)
        except Exception:
            logger.exception("Could not resume subsession entry %r", entry)


def _entry_str(entry: dict[str, object], key: str, default: str = "") -> str:
    """Coerce a persisted-entry field to ``str`` (typed JSON accessor)."""
    value = entry.get(key, default)
    return value if isinstance(value, str) else default


def _entry_int(entry: dict[str, object], key: str, default: int = 0) -> int:
    """Coerce a persisted-entry field to ``int``."""
    value = entry.get(key)
    return int(value) if isinstance(value, (int, float)) else default


def _entry_float(entry: dict[str, object], key: str, default: float = 0.0) -> float:
    """Coerce a persisted-entry field to ``float``."""
    value = entry.get(key)
    return float(value) if isinstance(value, (int, float)) else default


def _entry_opt_int(entry: dict[str, object], key: str) -> int | None:
    """Coerce a persisted-entry field to ``int | None``."""
    value = entry.get(key)
    return int(value) if isinstance(value, (int, float)) else None


def _entry_opt_float(entry: dict[str, object], key: str) -> float | None:
    """Coerce a persisted-entry field to ``float | None``."""
    value = entry.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _entry_opt_str(entry: dict[str, object], key: str) -> str | None:
    """Coerce a persisted-entry field to ``str | None``."""
    value = entry.get(key)
    return value if isinstance(value, str) else None


def _rebuild_completed_runs(entry: dict[str, object]) -> set[int]:
    """Reconstruct the ``completed_runs`` set from a persisted entry."""
    raw = entry.get("completed_runs")
    if isinstance(raw, list):
        return {int(v) for v in raw if isinstance(v, (int, float))}
    return set()


class _CommonEntryKwargs(TypedDict):
    """Typed dict for the common fields extracted from a persisted entry."""

    parent_id: str | None
    depth: int
    title: str
    prompt: str
    model_level: int
    interval_seconds: float | None
    include_previous_result: bool


def _entry_to_common_kwargs(entry: dict[str, object]) -> _CommonEntryKwargs:
    """Extract common SubsessionInfo/spawn_subsession fields from a persisted entry."""
    return {
        "parent_id": _entry_opt_str(entry, "parent_id"),
        "depth": _entry_int(entry, "depth", 1),
        "title": _entry_str(entry, "title"),
        "prompt": _entry_str(entry, "prompt"),
        "model_level": _entry_int(entry, "model_level", 3),
        "interval_seconds": _entry_opt_float(entry, "interval_seconds"),
        "include_previous_result": bool(entry.get("include_previous_result")),
    }


def _resume_entry(env: SubsessionEnv, entry: dict[str, object]) -> None:
    """Resume a single persisted registry entry (see resume_subsessions)."""
    status = _entry_str(entry, "status")
    kind = SubsessionKind(_entry_str(entry, "kind", "task"))
    sub_id = _entry_str(entry, "subsession_id")
    owner = _entry_str(entry, "owner_session_id")
    if not sub_id or not owner:
        return
    if status not in {s.value for s in ACTIVE_STATUSES}:
        _restore_entry(env.registry, entry)
        return

    if kind is SubsessionKind.PERIODIC:
        max_runs = _entry_opt_int(entry, "max_runs")
        runs = _entry_int(entry, "runs")
        remaining = None if max_runs is None else max(1, max_runs - runs)
        spawn_subsession(
            env=env,
            kind=kind,
            owner_session_id=owner,
            **_entry_to_common_kwargs(entry),
            max_runs=remaining,
            sub_id=sub_id,
            completed_runs=_rebuild_completed_runs(entry),
        )
        return

    # task / user_chat: mark interrupted and report to the main session.
    info = _restore_entry(env.registry, entry, force_active=True)
    if info is None:
        return
    last = SubsessionRegistry.last_assistant_text(info)
    summary = "Interrupted by a server restart."
    if last:
        summary += f" Last state: {last[:500]}"
    env.registry.mark_interrupted(sub_id, summary=summary)
    # Deliver to the owning main session — a nested parent's worker did
    # not survive the restart either, so main-chat delivery is the only
    # reliable destination.
    env.conversation_store.record_for_session(
        owner,
        f"[Subsession {sub_id[:8]} ({kind.value}) '{info.title}' interrupted]",
        summary,
    )


def _restore_entry(
    registry: SubsessionRegistry,
    entry: dict[str, object],
    *,
    force_active: bool = False,
) -> SubsessionInfo | None:
    """Re-register a persisted entry without launching a worker.

    Rebuilds the ``SubsessionInfo`` (including its transcript tail) via
    :meth:`SubsessionRegistry.restore`.  With *force_active* the entry is
    restored as RUNNING so a subsequent ``mark_interrupted`` transition
    is valid.
    """
    sub_id = _entry_str(entry, "subsession_id")
    if not sub_id or registry.get(sub_id) is not None:
        return None
    try:
        status = (
            SubsessionStatus.RUNNING
            if force_active
            else SubsessionStatus(_entry_str(entry, "status"))
        )
        info = SubsessionInfo(
            id=sub_id,
            kind=SubsessionKind(_entry_str(entry, "kind", "task")),
            owner_session_id=_entry_str(entry, "owner_session_id"),
            **_entry_to_common_kwargs(entry),
            status=status,
            created_at=_entry_float(entry, "created_at"),
            last_activity_at=_entry_float(entry, "last_activity_at"),
            runs=_entry_int(entry, "runs"),
            max_runs=_entry_opt_int(entry, "max_runs"),
            last_result=_entry_opt_str(entry, "last_result"),
            summary=_entry_opt_str(entry, "summary"),
            close_reason=_entry_opt_str(entry, "close_reason"),
            error=_entry_opt_str(entry, "error"),
            completed_runs=_rebuild_completed_runs(entry),
        )
    except ValueError:
        logger.warning("Skipping malformed persisted subsession %r", sub_id)
        return None
    transcript = entry.get("transcript")
    if isinstance(transcript, list):
        for item in transcript:
            if isinstance(item, dict):
                info.transcript.append(
                    TranscriptEntry(
                        role=_entry_str(item, "role"),
                        text=_entry_str(item, "text"),
                        timestamp=_entry_float(item, "timestamp"),
                    )
                )
    registry.restore(info)
    return info

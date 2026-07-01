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
from typing import TYPE_CHECKING

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
    agent_factory: Callable[
        ["Settings", int, SubsessionContext, CloseState], "ChatAgent"
    ]
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
) -> str:
    """Validate, register, and launch a subsession worker; return its id.

    Raises :class:`SubsessionCapacityError`, :class:`SubsessionDepthError`,
    :class:`SubsessionLevelError`, or :class:`SubsessionIntervalError` on
    invalid requests — the tool layer maps these to polite refusals.
    """
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
    )
    task = asyncio.create_task(_subsession_worker(env, info.id))
    env.registry.attach_task(info.id, task)
    env._tasks.add(task)
    task.add_done_callback(env._tasks.discard)
    return info.id


def _validate_model_level(settings: Settings, model_level: int) -> None:
    """Reject invalid levels and key-bearing levels without a key."""
    from robotsix_chat.config import level_needs_api_key

    if model_level not in (1, 2, 3, 4):
        raise SubsessionLevelError("model_level must be between 1 and 4")
    if level_needs_api_key(model_level) and not settings.llmio_api_key:
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
            registry.set_status(sub_id, SubsessionStatus.RUNNING)
            if info.kind is SubsessionKind.PERIODIC:
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
                return

            # -- kind-specific continuation --------------------------------
            if info.kind is SubsessionKind.TASK:
                pending = registry.drain_inbox(sub_id)
                if pending:
                    continue  # a steering message arrived mid-turn
                closed = registry.mark_closed(
                    sub_id, summary=reply, reason="completed", closed_by="agent"
                )
                if closed is not None:
                    await env.delivery.deliver_summary(closed, reply, "completed")
                return

            if info.kind is SubsessionKind.USER_CHAT:
                registry.set_status(sub_id, SubsessionStatus.WAITING)
                await registry.wait_for_inbox(sub_id, timeout=None)
                pending = registry.drain_inbox(sub_id)
                continue

            # -- PERIODIC ---------------------------------------------------
            suppressed = _is_no_change(reply)
            consecutive_no_change = 0 if not suppressed else consecutive_no_change + 1
            runs = info.runs + 1
            assert info.interval_seconds is not None  # validated at spawn
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
                return
            no_change_cap = env.settings.subsessions.auto_stop_no_change_runs
            if consecutive_no_change >= no_change_cap:
                summary = (
                    f"Auto-stopped after {no_change_cap} consecutive "
                    "no-change runs."
                )
                closed = registry.mark_closed(
                    sub_id,
                    summary=summary,
                    reason="no_change_auto_stop",
                    closed_by="system",
                )
                if closed is not None:
                    await env.delivery.deliver_summary(
                        closed, summary, "no_change_auto_stop"
                    )
                return

            # Sleep until the next tick, waking early on a steering message.
            woke = await registry.wait_for_inbox(
                sub_id, timeout=info.interval_seconds
            )
            pending = registry.drain_inbox(sub_id) if woke else []

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


def _resume_entry(env: SubsessionEnv, entry: dict[str, object]) -> None:
    """Resume a single persisted registry entry (see resume_subsessions)."""
    status = str(entry.get("status", ""))
    kind = SubsessionKind(str(entry.get("kind", "task")))
    sub_id = str(entry.get("subsession_id", ""))
    owner = str(entry.get("owner_session_id", ""))
    if not sub_id or not owner:
        return
    active = status in {s.value for s in ACTIVE_STATUSES}
    if not active:
        _restore_entry(env.registry, entry)
        return

    if kind is SubsessionKind.PERIODIC:
        max_runs = entry.get("max_runs")
        runs = int(entry.get("runs", 0) or 0)
        remaining = None if max_runs is None else max(1, int(max_runs) - runs)  # type: ignore[arg-type]
        spawn_subsession(
            env=env,
            kind=kind,
            owner_session_id=owner,
            parent_id=entry.get("parent_id") if isinstance(entry.get("parent_id"), str) else None,  # type: ignore[arg-type]
            depth=int(entry.get("depth", 1) or 1),
            title=str(entry.get("title", "")),
            prompt=str(entry.get("prompt", "")),
            model_level=int(entry.get("model_level", 3) or 3),
            interval_seconds=float(entry.get("interval_seconds") or 0) or None,
            include_previous_result=bool(entry.get("include_previous_result")),
            max_runs=remaining,
            sub_id=sub_id,
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
        f"[Subsession {sub_id[:8]} ({kind.value}) "
        f"'{entry.get('title', '')}' interrupted]",
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
    sub_id = str(entry.get("subsession_id", ""))
    if not sub_id or registry.get(sub_id) is not None:
        return None
    try:
        status = (
            SubsessionStatus.RUNNING
            if force_active
            else SubsessionStatus(str(entry.get("status")))
        )
        info = SubsessionInfo(
            id=sub_id,
            kind=SubsessionKind(str(entry.get("kind", "task"))),
            owner_session_id=str(entry.get("owner_session_id", "")),
            parent_id=entry.get("parent_id") if isinstance(entry.get("parent_id"), str) else None,  # type: ignore[arg-type]
            depth=int(entry.get("depth", 1) or 1),
            title=str(entry.get("title", "")),
            prompt=str(entry.get("prompt", "")),
            model_level=int(entry.get("model_level", 3) or 3),
            status=status,
            created_at=float(entry.get("created_at", 0.0) or 0.0),
            last_activity_at=float(entry.get("last_activity_at", 0.0) or 0.0),
            interval_seconds=(
                float(entry["interval_seconds"])  # type: ignore[arg-type]
                if entry.get("interval_seconds") is not None
                else None
            ),
            include_previous_result=bool(entry.get("include_previous_result")),
            runs=int(entry.get("runs", 0) or 0),
            max_runs=(
                int(entry["max_runs"])  # type: ignore[arg-type]
                if entry.get("max_runs") is not None
                else None
            ),
            last_result=(
                str(entry["last_result"])
                if entry.get("last_result") is not None
                else None
            ),
            summary=(
                str(entry["summary"]) if entry.get("summary") is not None else None
            ),
            close_reason=(
                str(entry["close_reason"])
                if entry.get("close_reason") is not None
                else None
            ),
            error=str(entry["error"]) if entry.get("error") is not None else None,
        )
    except (ValueError, TypeError):
        logger.warning("Skipping malformed persisted subsession %r", sub_id)
        return None
    transcript = entry.get("transcript")
    if isinstance(transcript, list):
        for item in transcript:
            if isinstance(item, dict):
                info.transcript.append(
                    TranscriptEntry(
                        role=str(item.get("role", "")),
                        text=str(item.get("text", "")),
                        timestamp=float(item.get("timestamp", 0.0) or 0.0),
                    )
                )
    registry.restore(info)
    return info

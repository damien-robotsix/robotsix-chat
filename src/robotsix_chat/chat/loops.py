"""Check-loop registry and worker for recurring background checks.

A **check loop** is a recurring background task that re-runs a delegated check
prompt on a cadence until it is stopped, hits a ``max_iterations`` cap, or
self-stops when the check reports a condition-met result.

Provide a ``CheckLoopRegistry`` (mirroring ``TaskRegistry``), an asyncio
``spawn_check_loop`` worker, JSON ``.data/`` persistence with a resume-on-startup
hook so a watchtower redeploy does not silently kill running loops, and
lifecycle frame builders exposed via ``chat/events.py``.

``time.monotonic()`` values persisted as ``next_run`` are NOT authoritative
across process restarts — on resume the cadence restarts fresh (next run =
now + interval).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .events import (
    EventSink,
    loop_failed_frame,
    loop_started_frame,
    loop_stopped_frame,
    loop_tick_frame,
)
from .runner import NULL_CHANNEL, DeliveryChannel

if TYPE_CHECKING:
    from robotsix_chat.chat.server import ChatAgent
    from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Enums, errors, and dataclasses
# ---------------------------------------------------------------------------


class LoopStatus(StrEnum):
    """Lifecycle status of a check loop."""

    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class LoopCapacityError(RuntimeError):
    """Raised when the check-loop concurrency cap is reached."""


class LoopIntervalError(ValueError):
    """Raised when the requested interval is below the minimum."""


@dataclass
class LoopInfo:
    """Public snapshot of a single check loop.

    Returned by :meth:`CheckLoopRegistry.get` and
    :meth:`CheckLoopRegistry.list_for_session`.
    """

    id: str
    session_id: str
    prompt: str
    interval_seconds: float
    status: LoopStatus
    iterations: int = 0
    max_iterations: int | None = None
    last_result: str | None = None
    next_run: float | None = None
    error: str | None = None
    stop_reason: str | None = None
    reason: str | None = None
    last_result_at: float | None = None
    verify_via_board: bool = False


# ---------------------------------------------------------------------------
# CheckLoopRegistry
# ---------------------------------------------------------------------------


class CheckLoopRegistry:
    """Track per-session check loops in memory with optional event publishing.

    Holds a strong reference to every in-flight :class:`asyncio.Task` so it is
    not garbage-collected before completion — mirroring
    :class:`~robotsix_chat.chat.tasks.TaskRegistry`.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
        event_sink: EventSink | None = None,
        store_path: Path | None = Path(".data/check_loops.json"),
    ) -> None:
        """Configure the clock, id factory, optional event sink, and JSON store path.

        When *event_sink* is provided, lifecycle frames are published on
        :meth:`register`, :meth:`record_tick`, :meth:`stop`, and :meth:`fail`.
        *store_path* defaults to ``.data/check_loops.json``; pass ``None`` to
        disable persistence entirely (useful in tests).
        """
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._event_sink = event_sink
        self._store_path = store_path
        # loop_id → LoopInfo (status + metadata snapshot).
        self._loops: dict[str, LoopInfo] = {}
        # loop_id → asyncio.Task (strong reference to prevent GC).
        self._running: dict[str, asyncio.Task[None]] = {}
        # session_id → set of loop_ids.
        self._by_session: dict[str, set[str]] = defaultdict(set)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def register(
        self,
        session_id: str,
        prompt: str,
        *,
        interval_seconds: float,
        max_iterations: int | None,
        coro: asyncio.Task[None],
        loop_id: str | None = None,
        reason: str | None = None,
        verify_via_board: bool = False,
        dedup: bool = True,
    ) -> str:
        """Register a new check loop for *session_id*.

        *coro* must be an already-scheduled :class:`asyncio.Task` (created via
        :func:`asyncio.create_task`). The registry stores a strong reference to
        it and arranges for that reference to be dropped when the task finishes.

        *reason* is an optional short human-readable summary of what this loop
        checks for.  When absent, the UI falls back to a truncated prompt.

        When *loop_id* is provided it is used directly; otherwise a new id is
        generated via the ``id_factory``.  This allows the resume hook to
        re-register a loop under its persisted id.

        When *dedup* is ``True`` (the default), any existing ``RUNNING`` loop
        for the same *session_id* and *prompt* (``prompt.strip()``) is stopped
        before the new loop is registered — preventing duplicate check loops.
        Pass ``dedup=False`` when restoring persisted loops on startup so
        siblings being resumed in the same pass are not superseded.

        Returns the loop id.
        """
        if dedup:
            new_key = prompt.strip()
            for existing_id in list(self._by_session.get(session_id, ())):
                existing = self._loops.get(existing_id)
                if (
                    existing is not None
                    and existing.status is LoopStatus.RUNNING
                    and existing.prompt.strip() == new_key
                ):
                    self.stop(existing_id, reason="superseded by a newer check loop")

        loop_id = loop_id if loop_id is not None else self._id_factory()
        info = LoopInfo(
            id=loop_id,
            session_id=session_id,
            prompt=prompt,
            interval_seconds=interval_seconds,
            max_iterations=max_iterations,
            status=LoopStatus.RUNNING,
            reason=reason,
            verify_via_board=verify_via_board,
        )
        self._loops[loop_id] = info
        self._running[loop_id] = coro
        self._by_session[session_id].add(loop_id)
        coro.add_done_callback(lambda _t: self._running.pop(loop_id, None))
        if self._event_sink is not None:
            self._event_sink.publish(
                session_id,
                loop_started_frame(
                    loop_id,
                    session_id,
                    prompt,
                    interval_seconds,
                    max_iterations,
                    reason=reason,
                ),
            )
        self._persist()
        return loop_id

    def record_tick(
        self,
        loop_id: str,
        *,
        result: str,
        next_run: float | None,
        publish: bool = True,
    ) -> None:
        """Record a completed iteration of *loop_id*.

        Increments ``iterations``, stores *result* and *next_run*, and
        publishes a ``loop_tick`` frame keyed by the loop's ``session_id``.

        When *publish* is ``False`` the internal state is updated but no
        SSE frame is emitted — useful for suppressed (no-change) ticks.
        """
        info = self._loops.get(loop_id)
        if info is None:
            return
        info.iterations += 1
        info.last_result = result
        info.next_run = next_run
        info.last_result_at = time.time()
        if publish and self._event_sink is not None:
            self._event_sink.publish(
                info.session_id,
                loop_tick_frame(
                    loop_id,
                    iteration=info.iterations,
                    result=result,
                    next_run=next_run,
                    last_result_at=info.last_result_at,
                ),
            )
        self._persist()

    def stop(self, loop_id: str, *, reason: str) -> None:
        """Stop *loop_id* with the given *reason*.

        Flips status to ``STOPPED``, cancels the tracked :class:`asyncio.Task`
        if still running, and publishes a ``loop_stopped`` frame.  Idempotent:
        calling ``stop`` on an already-stopped loop is a no-op.
        """
        info = self._loops.get(loop_id)
        if info is None or info.status != LoopStatus.RUNNING:
            return
        info.status = LoopStatus.STOPPED
        info.stop_reason = reason
        task = self._running.get(loop_id)
        if task is not None and not task.done():
            task.cancel()
        if self._event_sink is not None:
            self._event_sink.publish(
                info.session_id,
                loop_stopped_frame(loop_id, reason=reason, iterations=info.iterations),
            )
        self._persist()

    def fail(self, loop_id: str, *, error: str) -> None:
        """Mark *loop_id* as failed with the given *error*.

        Flips status to ``FAILED`` and publishes a ``loop_failed`` frame.
        Only transitions from ``RUNNING``; a stopped/completed loop is not
        re-marked.
        """
        info = self._loops.get(loop_id)
        if info is None or info.status != LoopStatus.RUNNING:
            return
        info.status = LoopStatus.FAILED
        info.error = error
        if self._event_sink is not None:
            self._event_sink.publish(
                info.session_id,
                loop_failed_frame(loop_id, error=error),
            )
        self._persist()

    def get(self, loop_id: str) -> LoopInfo | None:
        """Return the current snapshot of *loop_id*, or ``None``."""
        return self._loops.get(loop_id)

    def count_running(self) -> int:
        """Return the number of in-flight check loops (process-wide)."""
        return len(self._running)

    def list_for_session(self, session_id: str) -> list[LoopInfo]:
        """Return all loops for *session_id*."""
        ids = self._by_session.get(session_id, set())
        return [self._loops[lid] for lid in ids if lid in self._loops]

    def stop_all_for_session(self, session_id: str, *, reason: str) -> int:
        """Stop every ``RUNNING`` loop owned by *session_id*.

        Used when a session is closed/deleted so its recurring checks do not
        outlive it.  Each loop is stopped via :meth:`stop` (cancels the task,
        publishes a ``loop_stopped`` frame, persists).  Already-stopped loops
        are skipped.  Returns the number of loops actually stopped.
        """
        stopped = 0
        for loop_id in list(self._by_session.get(session_id, ())):
            info = self._loops.get(loop_id)
            if info is not None and info.status is LoopStatus.RUNNING:
                self.stop(loop_id, reason=reason)
                stopped += 1
        return stopped

    def snapshot(self) -> list[LoopInfo]:
        """Return a read-only list of all loop snapshots (process-wide).

        Does not mutate any internal state.  Each :class:`LoopInfo` is a
        frozen-in-time snapshot; the caller may safely iterate.
        """
        return list(self._loops.values())

    # ------------------------------------------------------------------
    # persistence helpers
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Write the full set of loops to the JSON store file.

        When *store_path* is ``None`` persistence is silently skipped.
        """
        if self._store_path is None:
            return
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create parent dir for %s", self._store_path)
            return

        entries: list[dict[str, object]] = []
        for info in self._loops.values():
            entries.append(
                {
                    "id": info.id,
                    "session_id": info.session_id,
                    "prompt": info.prompt,
                    "interval_seconds": info.interval_seconds,
                    "max_iterations": info.max_iterations,
                    "iterations": info.iterations,
                    "status": info.status.value,
                    "last_result": info.last_result,
                    "reason": info.reason,
                    "last_result_at": info.last_result_at,
                    "verify_via_board": info.verify_via_board,
                }
            )
        try:
            self._store_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("Failed to persist check loops to %s", self._store_path)


# ---------------------------------------------------------------------------
# BoardReadProbe — lightweight counter for consult_mill invocations
# ---------------------------------------------------------------------------


class BoardReadProbe:
    """Lightweight counter for tracking consult_mill invocations during a tick.

    A fresh instance (or reset via ``count = 0``) is used per check-loop
    iteration so each tick starts from zero.
    """

    def __init__(self) -> None:
        """Create a probe with ``count`` at zero."""
        self.count = 0

    def note(self) -> None:
        """Increment the invocation counter."""
        self.count += 1


def _wrap_consult_in_place(tools: list[Any], probe: BoardReadProbe) -> None:
    """Replace any ``consult_mill`` tool in *tools* with a probe-instrumented version.

    The wrapped tool preserves the original's name, docstring, and return
    value.  Each invocation increments *probe*.  When no ``consult_mill``
    is found, *tools* is left unchanged.
    """
    for idx, t in enumerate(tools):
        if getattr(t, "__name__", None) == "consult_mill":
            _original = t

            async def _instrumented(
                request: str,
                _orig: Any = _original,
            ) -> str:
                probe.note()
                return cast(str, await _orig(request))

            _instrumented.__name__ = "consult_mill"
            _instrumented.__doc__ = _original.__doc__
            tools[idx] = _instrumented
            return  # only one consult_mill expected


def _apply_board_read_gate(
    result_text: str,
    probe: BoardReadProbe | None,
    verify_via_board: bool,
    loop_id: str,
) -> str:
    """Apply the board-read gate to *result_text*.

    When *verify_via_board* is ``True`` and *probe* recorded zero
    ``consult_mill`` invocations during the tick, *result_text* is suppressed
    and replaced with a guardrail notice.  Otherwise it passes through
    unchanged.

    Returns the (possibly suppressed) result text.
    """
    if not verify_via_board or probe is None:
        return result_text
    if probe.count == 0 and result_text:
        logger.warning(
            "Check loop %s tick: status suppressed — "
            "consult_mill was not called. "
            "verify_via_board=True but the sub-agent "
            "produced output without reading the board.",
            loop_id,
        )
        return (
            "[guardrail] Status suppressed: the check "
            "produced a status report without reading "
            "the board (consult_mill was not called). "
            "No verified status is available this tick."
        )
    return result_text


# ---------------------------------------------------------------------------
# Guardrail header — prepended to the prompt when verify_via_board is active
# ---------------------------------------------------------------------------

_GUARDRAIL_HEADER = (
    "Before reporting ANY status (state, timestamps, ticket/thread state), "
    "you MUST call the consult_mill board-read tool and base your report "
    "solely on its reply. Never invent, assume, or narrate a state change "
    "or timestamp you have not read from the board. If you cannot read the "
    "board, say so explicitly instead of guessing.\n\n"
)


# ---------------------------------------------------------------------------
# Board-read gate setup helper
# ---------------------------------------------------------------------------


def _setup_board_read_gate(
    settings: Settings,
    agent_factory: Callable[[Settings], ChatAgent],
    loop_id: str | None,
) -> tuple[BoardReadProbe, Callable[[Settings], ChatAgent]]:
    """Build board-read-probe instrumentation when ``verify_via_board`` is enabled.

    Returns ``(probe, agent_factory)`` where *agent_factory* is wrapped to
    count ``consult_mill`` invocations via the *probe*.  Also warns when the
    mill is disabled (no board tool → every tick will be suppressed).
    """
    probe = BoardReadProbe()
    _base_factory = agent_factory

    if not settings.mill.enabled:
        logger.warning(
            "Check loop %s: verify_via_board=True but mill is "
            "disabled — no consult_mill tool is available; "
            "every tick will be suppressed.",
            loop_id,
        )

    def _gated_agent_factory(s: Settings) -> ChatAgent:
        agent = _base_factory(s)
        if hasattr(agent, "_tools") and agent._tools is not None:
            _wrap_consult_in_place(agent._tools, probe)
        return agent

    return probe, _gated_agent_factory


# ---------------------------------------------------------------------------
# check-loop worker — top-level async function (was a 227-line nested closure)
# ---------------------------------------------------------------------------


async def _check_loop_worker(
    *,
    id_future: asyncio.Future[str],
    probe: BoardReadProbe | None,
    registry: CheckLoopRegistry,
    settings: Settings,
    agent_factory: Callable[[Settings], ChatAgent],
    session_id: str,
    prompt: str,
    include_previous_result: bool,
    verify_via_board: bool,
    interval_seconds: float,
    suppress_when: Callable[[str], bool] | None,
    stop_when: Callable[[str], bool] | None,
    max_iterations: int | None,
    channel: DeliveryChannel,
) -> None:
    """Run the recurring check-loop tick cycle until stopped or capped."""
    loop_id = await id_future
    clock = registry._clock
    previous_result: str | None = None
    try:
        while True:
            if verify_via_board and probe is not None:
                probe.count = 0  # reset per tick

            # Build the effective prompt, combining the guardrail header
            # (when gated) with the previous result (when comparing).
            effective_prompt = prompt
            if verify_via_board:
                effective_prompt = _GUARDRAIL_HEADER + effective_prompt
            if include_previous_result and previous_result is not None:
                effective_prompt = (
                    f"Previous check result:\n{previous_result}\n\n"
                    f"---\n\nCurrent check prompt:\n{effective_prompt}"
                )

            agent = agent_factory(settings)
            result_text = "".join(
                [chunk async for chunk in agent.stream(effective_prompt)]
            )

            # --- gate: suppress unverified status ---
            if verify_via_board:
                result_text = _apply_board_read_gate(
                    result_text, probe, verify_via_board, loop_id
                )

            next_run = clock() + interval_seconds
            suppressed = suppress_when is not None and suppress_when(result_text)
            registry.record_tick(
                loop_id,
                result=result_text,
                next_run=next_run,
                publish=not suppressed,
            )

            # Re-read after record_tick (which incremented iterations).
            info = registry.get(loop_id)
            if info is None:
                return  # loop was removed

            # Best-effort: publish tick result into the conversation so the
            # foreground agent sees it in its next-turn history.
            # Skip when suppressed — the user shouldn't see no-change ticks.
            if not suppressed:
                try:
                    await channel.publish(
                        session_id,
                        loop_tick_frame(
                            loop_id,
                            iteration=info.iterations,
                            result=result_text,
                            next_run=next_run,
                        ),
                    )
                except Exception:
                    logger.exception(
                        "DeliveryChannel.publish failed for loop %s tick %s",
                        loop_id,
                        info.iterations,
                    )

            # Store for the next iteration's comparison.
            previous_result = result_text

            # Self-stop on condition-met.
            if stop_when is not None and stop_when(result_text):
                registry.stop(loop_id, reason="condition_met")
                return

            # Max-iterations cap.
            if max_iterations is not None and info.iterations >= max_iterations:
                registry.stop(loop_id, reason="max_iterations")
                return

            # Wait for the next interval.
            await asyncio.sleep(interval_seconds)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Check loop %s failed", loop_id)
        registry.fail(loop_id, error=str(exc))
        # Best-effort: publish loop failure into the conversation.
        try:
            await channel.publish(
                session_id,
                loop_failed_frame(loop_id, error=str(exc)),
            )
        except Exception:
            logger.exception(
                "DeliveryChannel.publish failed for loop %s failure",
                loop_id,
            )


# ---------------------------------------------------------------------------
# spawn_check_loop — worker
# ---------------------------------------------------------------------------


def spawn_check_loop(
    *,
    session_id: str,
    prompt: str,
    interval_seconds: float,
    settings: Settings,
    registry: CheckLoopRegistry,
    max_iterations: int | None = None,
    stop_when: Callable[[str], bool] | None = None,
    suppress_when: Callable[[str], bool] | None = None,
    include_previous_result: bool = False,
    agent_factory: Callable[[Settings], ChatAgent] | None = None,
    loop_id: str | None = None,
    channel: DeliveryChannel = NULL_CHANNEL,
    reason: str | None = None,
    verify_via_board: bool = False,
    dedup: bool = True,
) -> str:
    """Schedule a recurring check prompt; return the loop id immediately.

    The check agent is constructed via *agent_factory* using the provided
    *settings*. The worker coroutine runs off the request path with a strong
    reference held by *registry*.

    Every *interval_seconds* the check prompt is re-run to completion. The loop
    stops when one of: explicit :meth:`CheckLoopRegistry.stop`, *max_iterations*
    is reached, or *stop_when* returns ``True`` for the latest result.

    When *include_previous_result* is ``True``, each iteration after the first
    prepends the previous tick's result to the prompt so the sub-agent can
    compare against prior state.

    When *suppress_when* returns ``True`` for a result, the tick is recorded
    internally (iterations + last_result are updated) but NO SSE frame or
    conversation-store turn is published — the user sees no notification.
    This is useful for no-change checks where the result is uninteresting.

    When *loop_id* is provided it is used directly; otherwise a new id is
    generated.  This lets the resume hook re-register a loop under its
    persisted id.

    Each non-suppressed tick's result is published through *channel*
    (best-effort; errors are logged and swallowed) so the foreground agent
    sees it via
    :class:`~robotsix_chat.chat.delegation.ConversationDeliveryChannel`.
    Defaults to :data:`~robotsix_chat.chat.runner.NULL_CHANNEL` (no-op).

    When *dedup* is ``True`` (the default), :meth:`CheckLoopRegistry.register`
    stops any existing ``RUNNING`` loop with the same *session_id* and *prompt*
    before registering the new one.  This prevents duplicate check loops.
    Pass ``dedup=False`` when restoring persisted loops on startup.

    Raises :class:`LoopIntervalError` when *interval_seconds* is below
    :attr:`settings.min_check_loop_interval_seconds
    <robotsix_chat.config.Settings.min_check_loop_interval_seconds>`.

    Raises :class:`LoopCapacityError` when the process-wide concurrency cap
    (``settings.max_check_loops``) has been reached.
    """
    # Validation BEFORE spawning.
    if interval_seconds < settings.min_check_loop_interval_seconds:
        raise LoopIntervalError(
            f"check-loop interval must be at least "
            f"{settings.min_check_loop_interval_seconds} seconds, "
            f"got {interval_seconds!r}"
        )

    if registry.count_running() >= settings.max_check_loops:
        raise LoopCapacityError(
            f"check-loop limit reached ({settings.max_check_loops} concurrent)"
        )

    # Resolve agent factory lazily to avoid import cycle.
    if agent_factory is None:
        from robotsix_chat.chat.runner import _default_agent_factory

        agent_factory = _default_agent_factory

    # When verify_via_board is True, build a probe and wrap the agent factory
    # so consult_mill invocations are counted.
    if verify_via_board:
        probe, agent_factory = _setup_board_read_gate(settings, agent_factory, loop_id)
    else:
        probe = None

    # Race-free handshake: same pattern as spawn_subagent_task.
    id_future: asyncio.Future[str] = asyncio.Future()
    task = asyncio.create_task(
        _check_loop_worker(
            id_future=id_future,
            probe=probe,
            registry=registry,
            settings=settings,
            agent_factory=agent_factory,
            session_id=session_id,
            prompt=prompt,
            include_previous_result=include_previous_result,
            verify_via_board=verify_via_board,
            interval_seconds=interval_seconds,
            suppress_when=suppress_when,
            stop_when=stop_when,
            max_iterations=max_iterations,
            channel=channel,
        )
    )
    loop_id = registry.register(
        session_id,
        prompt,
        interval_seconds=interval_seconds,
        max_iterations=max_iterations,
        coro=task,
        loop_id=loop_id,
        reason=reason,
        verify_via_board=verify_via_board,
        dedup=dedup,
    )
    id_future.set_result(loop_id)
    return loop_id


# ---------------------------------------------------------------------------
# Resume hook — restore persisted loops after process restart
# ---------------------------------------------------------------------------


def resume_check_loops(
    registry: CheckLoopRegistry,
    settings: Settings,
    *,
    agent_factory: Callable[[Settings], ChatAgent] | None = None,
    channel: DeliveryChannel = NULL_CHANNEL,
) -> list[str]:
    """Read persisted loops and restart any that were ``RUNNING``.

    Loops persisted as ``STOPPED`` or ``FAILED`` are not restarted.

    The ``stop_when`` predicate is not serializable — resumed loops restart
    without a self-stop predicate.  Max-iterations and explicit stop still
    apply.

    *channel* is forwarded to :func:`spawn_check_loop` so resumed loops
    deliver tick results back into the conversation.

    Returns the list of loop ids that were resumed.
    """
    store_path = registry._store_path
    if store_path is None or not store_path.exists():
        return []

    try:
        raw = json.loads(store_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read check-loop persistence file %s", store_path)
        return []

    resumed: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "running":
            continue

        loop_id = entry.get("id")
        # Read the current "session_id" key, falling back to the legacy
        # "client_id" key for loops persisted before session-scoping landed.
        session_id = entry.get("session_id")
        if session_id is None:
            session_id = entry.get("client_id")
        prompt = entry.get("prompt")
        if (
            not isinstance(loop_id, str)
            or not isinstance(session_id, str)
            or not isinstance(prompt, str)
        ):
            continue

        interval_seconds = entry.get(
            "interval_seconds", settings.min_check_loop_interval_seconds
        )
        max_iterations = entry.get("max_iterations")
        iterations_already = entry.get("iterations", 0)

        # Compute remaining budget so resumed loops don't exceed the cap.
        remaining: int | None = None
        if isinstance(max_iterations, int) and isinstance(iterations_already, int):
            remaining = max_iterations - iterations_already
            if remaining <= 0:
                # Already hit the cap; skip.
                continue

        try:
            spawn_check_loop(
                session_id=session_id,
                prompt=prompt,
                interval_seconds=float(interval_seconds),
                settings=settings,
                registry=registry,
                max_iterations=remaining,
                # stop_when is intentionally None — not serializable.
                agent_factory=agent_factory,
                loop_id=loop_id,
                channel=channel,
                reason=entry.get("reason"),
                verify_via_board=entry.get("verify_via_board", False),
                dedup=False,
            )
        except (LoopCapacityError, LoopIntervalError) as exc:
            logger.warning("Could not resume check loop %s: %s", loop_id, exc)
            continue

        resumed.append(loop_id)

    return resumed

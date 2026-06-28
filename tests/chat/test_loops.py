"""Tests for :mod:`robotsix_chat.chat.loops` — check-loop registry and worker."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from robotsix_chat.chat.delegation import _terminal_result
from robotsix_chat.chat.events import (
    SSE_LOOP_FAILED_TYPE,
    SSE_LOOP_STARTED_TYPE,
    SSE_LOOP_STOPPED_TYPE,
    SSE_LOOP_TICK_TYPE,
    EventBus,
    loop_failed_frame,
    loop_started_frame,
    loop_stopped_frame,
    loop_tick_frame,
)
from robotsix_chat.chat.loops import (
    BoardReadProbe,
    CheckLoopRegistry,
    LoopCapacityError,
    LoopIntervalError,
    LoopStatus,
    _apply_board_read_gate,
    resume_check_loops,
    spawn_check_loop,
)
from tests.chat import _fake_coro

# ---------------------------------------------------------------------------
# Stubs / fakes — mirror test_tasks.py / test_runner.py patterns
# ---------------------------------------------------------------------------


class _FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _StubAgent:
    """An agent whose ``stream`` yields fixed chunks."""

    def __init__(self, chunks: list[str] | None = None) -> None:
        self.chunks = chunks or ["Hello", " ", "world!"]

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
    ) -> AsyncIterator[str]:
        for chunk in self.chunks:
            yield chunk


class _FailingAgent:
    """An agent whose ``stream`` always raises (as an async generator)."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
    ) -> AsyncIterator[str]:
        raise self.exc
        yield  # pragma: no cover


class _VariableAgent:
    """An agent that yields different results across calls.

    Each call pops the next result from *responses*.  When the list is
    exhausted, yields a default fallback.
    """

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.call_count = 0

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
    ) -> AsyncIterator[str]:
        self.call_count += 1
        result = self.responses.pop(0) if self.responses else "fallback"
        yield result


def _stub_settings(**overrides: Any) -> Any:
    """Build a stub settings object carrying ``max_check_loops`` and other attrs.

    Uses ``types.SimpleNamespace`` so the worker only reads attributes
    without needing the real ``Settings`` pydantic model (which does not
    yet have ``max_check_loops`` — that field lands in the parallel Settings
    epic child).
    """
    from types import SimpleNamespace

    defaults: dict[str, Any] = {
        "max_check_loops": 5,
        "min_check_loop_interval_seconds": 60.0,
        "llmio_model_level": 3,
        "llmio_api_key": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry(
    clock: _FakeClock | None = None,
    sink: EventBus | None = None,
    store_path: Path | None = None,
) -> CheckLoopRegistry:
    """Build a registry with deterministic loop ids (``L0``, ``L1``, …).

    When *store_path* is ``None``, persistence is disabled (the registry
    does not write to disk).  Pass an explicit ``tmp_path`` location to
    test persistence.
    """
    ids = count()
    return CheckLoopRegistry(
        clock=clock or _FakeClock(),
        id_factory=lambda: f"L{next(ids)}",
        event_sink=sink,
        store_path=store_path,  # None → no persistence
    )


# ---------------------------------------------------------------------------
# CheckLoopRegistry — unit tests (synchronous)
# ---------------------------------------------------------------------------


def test_register_returns_loop_id() -> None:
    """Registering a loop returns a unique id."""
    reg = _registry()
    lid = reg.register(
        "c1",
        "check",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    assert lid == "L0"


def test_register_stores_loop_info_with_running_status() -> None:
    """A newly-registered loop has status ``RUNNING``."""
    reg = _registry()
    lid = reg.register(
        "c1",
        "check db",
        interval_seconds=120.0,
        max_iterations=5,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    info = reg.get(lid)
    assert info is not None
    assert info.id == lid
    assert info.session_id == "c1"
    assert info.prompt == "check db"
    assert info.status == LoopStatus.RUNNING
    assert info.interval_seconds == 120.0
    assert info.max_iterations == 5
    assert info.iterations == 0
    assert info.last_result is None
    assert info.error is None
    assert info.stop_reason is None


def test_register_multiple_loops_get_distinct_ids() -> None:
    """Each registered loop receives a unique id."""
    reg = _registry()
    lid1 = reg.register(
        "c1",
        "loop 1",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    lid2 = reg.register(
        "c1",
        "loop 2",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    assert lid1 != lid2
    assert reg.get(lid1) is not None
    assert reg.get(lid2) is not None


def test_get_nonexistent_loop_returns_none() -> None:
    """Looking up a loop id that was never registered returns ``None``."""
    reg = _registry()
    assert reg.get("bogus") is None


def test_list_for_session_returns_loops() -> None:
    """``list_for_session`` returns all loops registered under a client."""
    reg = _registry()
    reg.register(
        "c1",
        "loop a",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    reg.register(
        "c1",
        "loop b",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    loops = reg.list_for_session("c1")
    assert len(loops) == 2
    prompts = {lp.prompt for lp in loops}
    assert prompts == {"loop a", "loop b"}


def test_list_for_session_unknown_client_returns_empty() -> None:
    """An unknown client yields an empty list, not an error."""
    reg = _registry()
    assert reg.list_for_session("nobody") == []


def test_list_for_session_isolated_per_client() -> None:
    """Loops for one client are not visible from another."""
    reg = _registry()
    reg.register(
        "c-a",
        "a-only",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    reg.register(
        "c-b",
        "b-only",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    a = reg.list_for_session("c-a")
    b = reg.list_for_session("c-b")
    assert [lp.prompt for lp in a] == ["a-only"]
    assert [lp.prompt for lp in b] == ["b-only"]


def test_record_tick_increments_iteration_and_stores_result() -> None:
    """Calling ``record_tick()`` increments iterations and stores result."""
    reg = _registry()
    lid = reg.register(
        "c1",
        "tick test",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    reg.record_tick(lid, result="all good", next_run=1100.0)
    info = reg.get(lid)
    assert info is not None
    assert info.iterations == 1
    assert info.last_result == "all good"
    assert info.next_run == 1100.0


def test_record_tick_sets_last_result_at() -> None:
    """Calling ``record_tick()`` sets ``last_result_at`` to a positive float."""
    import time as _time

    reg = _registry()
    before = _time.time()
    lid = reg.register(
        "c1",
        "timestamp test",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    reg.record_tick(lid, result="ok", next_run=0.0)
    after = _time.time()

    info = reg.get(lid)
    assert info is not None
    assert isinstance(info.last_result_at, float)
    assert info.last_result_at > 0
    assert before <= info.last_result_at <= after


def test_register_stores_reason() -> None:
    """``register`` stores the optional ``reason`` field."""
    reg = _registry()
    lid = reg.register(
        "c1",
        "check stocks",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
        reason="Monitor stock prices every minute",
    )
    info = reg.get(lid)
    assert info is not None
    assert info.reason == "Monitor stock prices every minute"


def test_register_reason_defaults_to_none() -> None:
    """When ``reason`` is omitted, it defaults to ``None``."""
    reg = _registry()
    lid = reg.register(
        "c1",
        "silent check",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    info = reg.get(lid)
    assert info is not None
    assert info.reason is None


def test_record_tick_ignores_unknown_id() -> None:
    """Calling ``record_tick()`` on an unknown id does not raise."""
    reg = _registry()
    reg.record_tick("no-such", result="x", next_run=0.0)  # no-op, no raise


def test_stop_transitions_to_stopped() -> None:
    """Calling ``stop()`` sets status to STOPPED."""
    reg = _registry()
    lid = reg.register(
        "c1",
        "to stop",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    reg.stop(lid, reason="user_requested")
    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    assert info.stop_reason == "user_requested"


def test_stop_is_idempotent() -> None:
    """Calling stop on an already-stopped loop is a no-op."""
    reg = _registry()
    lid = reg.register(
        "c1",
        "to stop",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    reg.stop(lid, reason="first")
    reg.stop(lid, reason="second")
    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    assert info.stop_reason == "first"


def test_stop_unknown_id_does_not_raise() -> None:
    """Calling ``stop()`` on an unknown id does not raise."""
    reg = _registry()
    reg.stop("no-such", reason="x")  # no-op


def test_stop_removes_from_running_count() -> None:
    """After stop + done callback, the loop is no longer in ``count_running()``."""
    reg = _registry()
    lid = reg.register(
        "c1",
        "running",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    assert reg.count_running() == 1

    reg.stop(lid, reason="done")
    # _FakeCoro doesn't support real cancel, but the done callback fires
    # and pops from _running. Simulate that:
    reg._running.pop(lid, None)
    assert reg.count_running() == 0


def test_fail_transitions_to_failed() -> None:
    """Calling ``fail()`` sets status to FAILED and stores the error."""
    reg = _registry()
    lid = reg.register(
        "c1",
        "risky",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    reg.fail(lid, error="boom")
    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.FAILED
    assert info.error == "boom"


def test_fail_does_not_transition_stopped_loop() -> None:
    """A stopped loop is not re-marked as failed."""
    reg = _registry()
    lid = reg.register(
        "c1",
        "x",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    reg.stop(lid, reason="explicit")

    reg.fail(lid, error="should be ignored")
    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    assert info.error is None


def test_fail_unknown_id_does_not_raise() -> None:
    """Calling ``fail()`` on an unknown id does not raise."""
    reg = _registry()
    reg.fail("no-such", error="x")  # no-op


def test_count_running_reflects_registered_loops() -> None:
    """``count_running()`` returns the number of in-flight loops."""
    reg = _registry()
    assert reg.count_running() == 0
    reg.register(
        "c1",
        "loop 1",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    assert reg.count_running() == 1
    reg.register(
        "c1",
        "loop 2",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    assert reg.count_running() == 2


def test_strong_reference_held_for_running_loop() -> None:
    """The registry holds a strong reference so a running loop is not GC'd."""
    reg = _registry()
    lid = reg.register(
        "c1",
        "long",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    assert lid in reg._running


# ---------------------------------------------------------------------------
# EventSink integration — lifecycle frames
# ---------------------------------------------------------------------------


def test_registry_publishes_started_frame_on_register() -> None:
    """Registering a loop publishes a ``loop_started`` frame to the EventBus."""
    bus = EventBus()
    reg = _registry(sink=bus)
    q = bus.subscribe("c1")

    lid = reg.register(
        "c1",
        "check x",
        interval_seconds=60.0,
        max_iterations=3,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    frame = q.get_nowait()
    assert frame == loop_started_frame(lid, "c1", "check x", 60.0, 3)


def test_registry_publishes_tick_frame_on_record_tick() -> None:
    """Recording a tick publishes a ``loop_tick`` frame."""
    bus = EventBus()
    reg = _registry(sink=bus)
    q = bus.subscribe("c1")

    lid = reg.register(
        "c1",
        "ticking",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    q.get_nowait()  # consume started frame

    reg.record_tick(lid, result="ok", next_run=1100.0)
    frame = q.get_nowait()
    last_result_at = frame["last_result_at"]
    assert isinstance(last_result_at, float)
    assert frame == loop_tick_frame(
        lid,
        iteration=1,
        result="ok",
        next_run=1100.0,
        last_result_at=last_result_at,
    )


def test_registry_publishes_stopped_frame_on_stop() -> None:
    """Stopping a loop publishes a ``loop_stopped`` frame."""
    bus = EventBus()
    reg = _registry(sink=bus)
    q = bus.subscribe("c1")

    lid = reg.register(
        "c1",
        "looping",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    q.get_nowait()  # consume started frame

    reg.stop(lid, reason="max_iterations")
    frame = q.get_nowait()
    assert frame == loop_stopped_frame(lid, reason="max_iterations", iterations=0)


def test_registry_publishes_failed_frame_on_fail() -> None:
    """Failing a loop publishes a ``loop_failed`` frame."""
    bus = EventBus()
    reg = _registry(sink=bus)
    q = bus.subscribe("c1")

    lid = reg.register(
        "c1",
        "fragile",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    q.get_nowait()  # consume started frame

    reg.fail(lid, error="timeout")
    frame = q.get_nowait()
    assert frame == loop_failed_frame(lid, error="timeout")


def test_registry_no_event_sink_no_publish() -> None:
    """When ``event_sink`` is None, no frames are published (no crash)."""
    reg = _registry(sink=None)
    lid = reg.register(
        "c1",
        "quiet",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    reg.record_tick(lid, result="x", next_run=0.0)
    reg.stop(lid, reason="x")
    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED


# ---------------------------------------------------------------------------
# Frame-builder shape tests
# ---------------------------------------------------------------------------


def test_loop_started_frame_shape() -> None:
    """``loop_started_frame`` returns the documented dict shape."""
    frame = loop_started_frame("L1", "c1", "check health", 30.0, 10)
    assert frame == {
        "type": SSE_LOOP_STARTED_TYPE,
        "loop_id": "L1",
        "session_id": "c1",
        "prompt": "check health",
        "interval_seconds": 30.0,
        "max_iterations": 10,
        "status": "running",
    }

    # With reason supplied.
    frame_reason = loop_started_frame(
        "L1", "c1", "check health", 30.0, 10, reason="Health check"
    )
    assert frame_reason["reason"] == "Health check"


def test_loop_tick_frame_shape() -> None:
    """``loop_tick_frame`` returns the documented dict shape."""
    frame = loop_tick_frame("L1", iteration=3, result="all ok", next_run=1500.0)
    assert frame == {
        "type": SSE_LOOP_TICK_TYPE,
        "loop_id": "L1",
        "iteration": 3,
        "result": "all ok",
        "next_run": 1500.0,
        "status": "running",
        "last_result_at": None,
    }


def test_loop_stopped_frame_shape() -> None:
    """``loop_stopped_frame`` returns the documented dict shape."""
    frame = loop_stopped_frame("L1", reason="max_iterations", iterations=5)
    assert frame == {
        "type": SSE_LOOP_STOPPED_TYPE,
        "loop_id": "L1",
        "reason": "max_iterations",
        "iterations": 5,
        "status": "stopped",
    }


def test_loop_failed_frame_shape() -> None:
    """``loop_failed_frame`` returns the documented dict shape."""
    frame = loop_failed_frame("L1", error="connection refused")
    assert frame == {
        "type": SSE_LOOP_FAILED_TYPE,
        "loop_id": "L1",
        "error": "connection refused",
        "status": "failed",
    }


# ---------------------------------------------------------------------------
# spawn_check_loop — async integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_runs_first_iteration_and_publishes_tick() -> None:
    """A stub agent runs to completion → first tick published."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings()

    called: list[str] = []

    class _SpyAgent:
        async def stream(
            self,
            message: str,
            *,
            history: list[tuple[str, str]] | None = None,
            session_id: str | None = None,
            client_id: str | None = None,
            images: list[tuple[str, bytes]] | None = None,
        ) -> AsyncIterator[str]:
            called.append(message)
            for c in ["all good"]:
                yield c

    lid = spawn_check_loop(
        session_id="c1",
        prompt="is it ok?",
        interval_seconds=60.0,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _SpyAgent(),
    )

    assert lid
    await asyncio.sleep(0.1)

    assert len(called) >= 1
    assert called[0] == "is it ok?"

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.RUNNING
    assert info.iterations == 1
    assert info.last_result == "all good"

    reg.stop(lid, reason="test teardown")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_tick_agent_can_self_stop_via_injected_tool() -> None:
    """The worker injects a loop-scoped ``stop_check_loop`` the tick agent can call.

    A tick sub-agent that detects a terminal condition and calls the injected
    stop tool halts the loop after recording its final tick — instead of
    re-reporting the same terminal state every interval forever.
    """
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings()

    class _SelfStoppingAgent:
        """Has a ``_tools`` list; calls the injected stop tool during stream."""

        def __init__(self) -> None:
            self._tools: list[Any] = []

        async def stream(
            self,
            message: str,
            *,
            history: list[tuple[str, str]] | None = None,
            session_id: str | None = None,
            client_id: str | None = None,
            images: list[tuple[str, bytes]] | None = None,
        ) -> AsyncIterator[str]:
            stop = next(
                t
                for t in self._tools
                if getattr(t, "__name__", "") == "stop_check_loop"
            )
            await stop()
            yield "terminal — stopping"

    lid = spawn_check_loop(
        session_id="c1",
        prompt="monitor until terminal",
        interval_seconds=60.0,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _SelfStoppingAgent(),
    )

    await asyncio.sleep(0.1)

    info = reg.get(lid)
    assert info is not None
    # Final tick recorded, then the loop self-stopped.
    assert info.iterations == 1
    assert info.last_result == "terminal — stopping"
    assert info.status == LoopStatus.STOPPED
    assert info.stop_reason == "condition_met"


@pytest.mark.asyncio
async def test_spawn_returns_immediately() -> None:
    """spawn_check_loop returns a loop_id synchronously before the agent runs."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings()

    started: asyncio.Event = asyncio.Event()
    finish: asyncio.Event = asyncio.Event()

    class _SlowAgent:
        async def stream(
            self,
            message: str,
            *,
            history: list[tuple[str, str]] | None = None,
            session_id: str | None = None,
            client_id: str | None = None,
            images: list[tuple[str, bytes]] | None = None,
        ) -> AsyncIterator[str]:
            started.set()
            await finish.wait()
            yield "done"

    agent = _SlowAgent()

    lid = spawn_check_loop(
        session_id="c1",
        prompt="slow",
        interval_seconds=60.0,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: agent,
    )

    assert lid
    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.RUNNING

    await asyncio.wait_for(started.wait(), timeout=1.0)
    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.RUNNING

    finish.set()
    await asyncio.sleep(0.1)

    info = reg.get(lid)
    assert info is not None
    assert info.iterations == 1
    assert info.last_result == "done"

    reg.stop(lid, reason="test teardown")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_explicit_stop_publishes_stopped_frame() -> None:
    """Calling registry.stop() publishes a loop_stopped frame."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings()
    q_sse = bus.subscribe("c1")

    lid = spawn_check_loop(
        session_id="c1",
        prompt="loop",
        interval_seconds=60.0,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["ok"]),
        max_iterations=100,
    )

    await asyncio.sleep(0.1)

    while not q_sse.empty():
        q_sse.get_nowait()

    reg.stop(lid, reason="explicit")

    stopped_frame = q_sse.get_nowait()
    assert stopped_frame["type"] == SSE_LOOP_STOPPED_TYPE
    assert stopped_frame["reason"] == "explicit"

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED

    await asyncio.sleep(0.1)
    assert reg.count_running() == 0


@pytest.mark.asyncio
async def test_max_iterations_cap_stops_loop() -> None:
    """After exactly N iterations the loop stops with reason max_iterations."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    q_sse = bus.subscribe("c1")

    lid = spawn_check_loop(
        session_id="c1",
        prompt="tick",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["ok"]),
        max_iterations=2,
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    assert info.stop_reason == "max_iterations"
    assert info.iterations == 2

    frames = []
    while not q_sse.empty():
        frames.append(q_sse.get_nowait())
    stopped_frames = [f for f in frames if f["type"] == SSE_LOOP_STOPPED_TYPE]
    assert len(stopped_frames) == 1
    assert stopped_frames[0]["reason"] == "max_iterations"
    assert stopped_frames[0]["iterations"] == 2


@pytest.mark.asyncio
async def test_stop_when_self_stops_loop() -> None:
    """A stop_when predicate returning True stops the loop (reason condition_met)."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)

    lid = spawn_check_loop(
        session_id="c1",
        prompt="check",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["CONDITION_MET"]),
        stop_when=lambda result: "CONDITION_MET" in result,
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    assert info.stop_reason == "condition_met"
    assert info.iterations == 1


@pytest.mark.asyncio
async def test_stop_when_terminal_keyword_self_stops() -> None:
    """A stop_when predicate using _terminal_result stops on 'closed'."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)

    lid = spawn_check_loop(
        session_id="c1",
        prompt="check ticket",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["The ticket is closed."]),
        stop_when=_terminal_result,
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    assert info.stop_reason == "condition_met"
    assert info.iterations == 1


@pytest.mark.asyncio
async def test_no_zombie_ticks_after_stop() -> None:
    """After stop_when fires, no additional tick iteration occurs.

    This is the regression test for the "stop attempted but loop keeps
    ticking" bug — a loop that reports a terminal state must not fire any
    further ticks, even if the underlying task cancellation races with the
    stop flag.
    """
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)

    lid = spawn_check_loop(
        session_id="c1",
        prompt="check ticket",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["The ticket is done."]),
        stop_when=_terminal_result,
        max_iterations=100,
    )

    # Wait for the loop to stop.
    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    stop_iterations = info.iterations

    # Sleep long enough that if the loop were still ticking it would have
    # advanced (interval is 0.01s but the worker's asyncio.sleep can't
    # start because the task was cancelled/stopped).
    await asyncio.sleep(0.2)

    # Iteration count must NOT have increased.
    info = reg.get(lid)
    assert info is not None
    assert info.iterations == stop_iterations, (
        f"Zombie tick detected: iterations went from {stop_iterations} "
        f"to {info.iterations} after stop"
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("The ticket is closed.", True),
        ("Status: done.", True),
        ("The issue has been resolved.", True),
        ("All tasks completed.", True),
        ("The ticket is not closed.", False),
        ("I can't tell if it's done.", False),
        ("Unable to verify closed status.", False),
        ("It failed to close.", False),
        ("Nothing is done yet.", False),
        ("NO_CHANGE: still open.", False),
        ("", False),
    ],
)
def test_terminal_result_predicate(text: str, expected: bool) -> None:
    """_terminal_result correctly distinguishes terminal from non-terminal text."""
    assert _terminal_result(text) == expected


@pytest.mark.asyncio
async def test_min_interval_rejection() -> None:
    """interval_seconds below MIN raises LoopIntervalError and creates no loop."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings()

    assert reg.count_running() == 0

    with pytest.raises(LoopIntervalError, match="interval must be at least"):
        spawn_check_loop(
            session_id="c1",
            prompt="too fast",
            interval_seconds=10.0,
            settings=settings,
            registry=reg,
            agent_factory=lambda s: _StubAgent(["nope"]),
        )

    assert reg.count_running() == 0


@pytest.mark.asyncio
async def test_capacity_rejection() -> None:
    """When at capacity, spawn_check_loop raises LoopCapacityError."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(max_check_loops=1)

    reg.register(
        "c1",
        "existing",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    assert reg.count_running() == 1

    with pytest.raises(LoopCapacityError, match="check-loop limit reached"):
        spawn_check_loop(
            session_id="c1",
            prompt="should reject",
            interval_seconds=60.0,
            settings=settings,
            registry=reg,
            agent_factory=lambda s: _StubAgent(["nope"]),
        )

    assert reg.count_running() == 1


@pytest.mark.asyncio
async def test_capacity_allows_when_below_cap() -> None:
    """spawn_check_loop succeeds when count is below the cap."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(max_check_loops=2)

    reg.register(
        "c1",
        "existing",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    assert reg.count_running() == 1

    lid = spawn_check_loop(
        session_id="c1",
        prompt="allowed",
        interval_seconds=60.0,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["ok"]),
    )

    assert lid
    assert reg.count_running() == 2

    reg.stop(lid, reason="cleanup")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_loop_failure_publishes_failed_frame() -> None:
    """When the agent raises, the loop is marked FAILED and a frame is published."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings()
    q_sse = bus.subscribe("c1")

    exc = ValueError("something broke")
    lid = spawn_check_loop(
        session_id="c1",
        prompt="will fail",
        interval_seconds=60.0,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _FailingAgent(exc),
    )

    await asyncio.sleep(0.15)

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.FAILED
    assert info.error == "something broke"

    frames = []
    while not q_sse.empty():
        frames.append(q_sse.get_nowait())
    failed_frames = [f for f in frames if f["type"] == SSE_LOOP_FAILED_TYPE]
    assert len(failed_frames) == 1
    assert failed_frames[0]["loop_id"] == lid
    assert failed_frames[0]["error"] == "something broke"


@pytest.mark.asyncio
async def test_cancelled_error_does_not_mark_failed() -> None:
    """When the loop task is cancelled (via registry.stop), it's STOPPED not FAILED."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings()

    lid = spawn_check_loop(
        session_id="c1",
        prompt="will be cancelled",
        interval_seconds=60.0,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _SpySleepAgent(),
        max_iterations=100,
    )

    await asyncio.sleep(0.1)

    reg.stop(lid, reason="user stop")
    await asyncio.sleep(0.1)

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    assert info.stop_reason == "user stop"
    assert info.error is None


class _SpySleepAgent:
    """Agent that sleeps forever — used for cancellation tests."""

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
    ) -> AsyncIterator[str]:
        yield "waiting"
        await asyncio.sleep(3600)
        yield "never"  # pragma: no cover


# ---------------------------------------------------------------------------
# task_id / loop_id handshake (race-free)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_id_handshake_consistent() -> None:
    """The loop_id the worker sees matches the returned id."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings()

    lid = spawn_check_loop(
        session_id="c1",
        prompt="handshake",
        interval_seconds=60.0,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["ok"]),
    )

    await asyncio.sleep(0.1)

    info = reg.get(lid)
    assert info is not None
    assert info.id == lid

    reg.stop(lid, reason="cleanup")
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# DeliveryChannel integration — tick results land in ConversationStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_with_channel_records_tick_in_store() -> None:
    """spawn_check_loop with a ConversationDeliveryChannel records tick result."""
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.delegation import ConversationDeliveryChannel

    store = ConversationStore(
        idle_reset_seconds=3600.0,
        max_history_turns=10,
        max_conversations=5,
    )
    store.create_session("c-chan")
    channel = ConversationDeliveryChannel(store)

    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)

    lid = spawn_check_loop(
        session_id="c-chan",
        prompt="check with channel",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["tick result"]),
        max_iterations=1,
        channel=channel,
    )

    # Wait for the single iteration to complete and the loop to stop.
    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED

    # The store should have one synthetic turn from the loop_tick publish,
    # recorded into the exact session that spawned the loop.
    history = store.history("c-chan")
    assert len(history) >= 1, f"Expected >= 1 turns, got {len(history)}: {history}"
    user_msg, assistant_msg = history[0]
    assert "Check loop" in user_msg
    assert lid in user_msg
    assert "tick 1" in user_msg
    assert assistant_msg == "tick result"


@pytest.mark.asyncio
async def test_spawn_channel_raise_does_not_kill_loop() -> None:
    """A DeliveryChannel that raises does not kill the loop — the loop finishes."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)

    class _BoomChannel:
        async def publish(self, client_id: str, frame: dict[str, object]) -> None:
            raise RuntimeError("channel offline")

    boom = _BoomChannel()

    lid = spawn_check_loop(
        session_id="c-boom",
        prompt="survive boom",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["still ok"]),
        max_iterations=1,
        channel=boom,
    )

    # Wait for the loop to finish its single iteration.
    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    # The loop should have completed normally (stopped by max_iterations),
    # NOT marked as FAILED.
    assert info.status == LoopStatus.STOPPED, (
        f"Expected STOPPED, got {info.status} (error={info.error!r})"
    )
    assert info.iterations == 1
    assert info.last_result == "still ok"


@pytest.mark.asyncio
async def test_spawn_with_channel_records_failure_in_store() -> None:
    """When the agent raises, the loop_failed frame is recorded in the store."""
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.delegation import ConversationDeliveryChannel

    store = ConversationStore(
        idle_reset_seconds=3600.0,
        max_history_turns=10,
        max_conversations=5,
    )
    store.create_session("c-fail-chan")
    channel = ConversationDeliveryChannel(store)

    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings()

    exc = ValueError("agent crash")
    lid = spawn_check_loop(
        session_id="c-fail-chan",
        prompt="will fail with channel",
        interval_seconds=60.0,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _FailingAgent(exc),
        channel=channel,
    )

    await asyncio.sleep(0.15)

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.FAILED

    # The store should have a loop_failed synthetic turn, recorded into the
    # exact session that spawned the loop.
    history = store.history("c-fail-chan")
    assert len(history) == 1
    user_msg, assistant_msg = history[0]
    assert "Check loop" in user_msg
    assert lid in user_msg
    assert "failed" in user_msg
    assert assistant_msg == "Error: agent crash"


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


def test_persistence_writes_on_register_stop_fail(tmp_path: Path) -> None:
    """Registering / stopping / failing loops writes them to the JSON store."""
    store = tmp_path / "loops.json"
    reg = _registry(store_path=store)

    lid1 = reg.register(
        "c1",
        "loop 1",
        interval_seconds=60,
        max_iterations=3,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    lid2 = reg.register(
        "c1",
        "loop 2",
        interval_seconds=120,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    assert store.exists()
    data = json.loads(store.read_text())
    assert len(data) == 2
    ids = {e["id"] for e in data}
    assert ids == {lid1, lid2}

    reg.stop(lid1, reason="max_iterations")
    data = json.loads(store.read_text())
    stopped = [e for e in data if e["id"] == lid1][0]
    assert stopped["status"] == "stopped"

    reg.fail(lid2, error="timeout")
    data = json.loads(store.read_text())
    failed = [e for e in data if e["id"] == lid2][0]
    assert failed["status"] == "failed"


def test_persistence_round_trips_reason_and_last_result_at(tmp_path: Path) -> None:
    """``reason`` and ``last_result_at`` survive a persist→load round-trip."""
    import json

    store = tmp_path / "loops.json"
    reg = _registry(store_path=store)

    lid = reg.register(
        "c1",
        "persist me",
        interval_seconds=90.0,
        max_iterations=3,
        coro=_fake_coro(),  # type: ignore[arg-type]
        reason="Check health every 90s",
    )
    reg.record_tick(lid, result="ok so far", next_run=2000.0)

    data = json.loads(store.read_text())
    entry = next(e for e in data if e["id"] == lid)
    assert entry["reason"] == "Check health every 90s"
    assert isinstance(entry["last_result_at"], int | float)
    assert entry["last_result_at"] > 0


@pytest.mark.asyncio
async def test_persistence_loads_missing_reason_and_last_result_at_defaults_to_none(
    tmp_path: Path,
) -> None:
    """A persisted file missing ``reason`` and ``last_result_at`` loads cleanly.

    The new fields default to ``None`` when absent — no error.
    """
    import json

    store = tmp_path / "loops.json"
    old_format_loop = {
        "id": "L-old",
        "client_id": "c-old",
        "prompt": "old format loop",
        "interval_seconds": 60.0,
        "max_iterations": None,
        "iterations": 0,
        "status": "running",
        "last_result": None,
    }
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps([old_format_loop], indent=2))

    reg = _registry(store_path=store)
    from robotsix_chat.chat.loops import resume_check_loops

    settings = _stub_settings()
    resumed = resume_check_loops(
        reg,
        settings,
        agent_factory=lambda s: _StubAgent(["resumed"]),
    )
    # The old-format loop was running → should resume.
    assert resumed == ["L-old"]
    info = reg.get("L-old")
    assert info is not None
    assert info.reason is None
    assert info.last_result_at is None

    # Cleanup.
    reg.stop("L-old", reason="test teardown")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_resume_restarts_running_loops(tmp_path: Path) -> None:
    """Resume re-registers loops that were RUNNING in the persisted file."""
    store = tmp_path / "loops.json"

    running_loop = {
        "id": "L-run",
        "client_id": "c1",
        "prompt": "resume me",
        "interval_seconds": 60.0,
        "max_iterations": 5,
        "iterations": 2,
        "status": "running",
        "last_result": "prior ok",
    }
    stopped_loop = {
        "id": "L-stop",
        "client_id": "c1",
        "prompt": "do not resume",
        "interval_seconds": 60.0,
        "max_iterations": None,
        "iterations": 3,
        "status": "stopped",
        "last_result": "done",
    }
    failed_loop = {
        "id": "L-fail",
        "client_id": "c1",
        "prompt": "broken",
        "interval_seconds": 60.0,
        "max_iterations": None,
        "iterations": 1,
        "status": "failed",
        "last_result": None,
    }
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps([running_loop, stopped_loop, failed_loop], indent=2))

    bus = EventBus()
    reg = _registry(sink=bus, store_path=store)
    settings = _stub_settings()

    resumed = resume_check_loops(
        reg,
        settings,
        agent_factory=lambda s: _StubAgent(["resumed ok"]),
    )

    assert len(resumed) == 1
    assert resumed[0] == "L-run"

    info = reg.get("L-run")
    assert info is not None
    assert info.status == LoopStatus.RUNNING
    assert info.prompt == "resume me"
    assert info.session_id == "c1"
    assert info.max_iterations == 3  # 5 - 2 remaining

    assert reg.get("L-stop") is None
    assert reg.get("L-fail") is None

    reg.stop("L-run", reason="test cleanup")


def test_resume_missing_file_is_noop(tmp_path: Path) -> None:
    """When the store file does not exist, resume returns empty."""
    store = tmp_path / "nonexistent.json"
    reg = _registry(store_path=store)
    settings = _stub_settings()

    resumed = resume_check_loops(reg, settings)
    assert resumed == []


def test_resume_skips_exhausted_max_iterations(tmp_path: Path) -> None:
    """A RUNNING loop whose iterations already reached max is not resumed."""
    store = tmp_path / "loops.json"
    running_loop = {
        "id": "L-done",
        "client_id": "c1",
        "prompt": "should not resume",
        "interval_seconds": 60.0,
        "max_iterations": 2,
        "iterations": 2,
        "status": "running",
        "last_result": "final",
    }
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps([running_loop], indent=2))

    reg = _registry(store_path=store)
    settings = _stub_settings()

    resumed = resume_check_loops(
        reg, settings, agent_factory=lambda s: _StubAgent(["ok"])
    )
    assert resumed == []
    assert reg.get("L-done") is None


def test_resume_handles_corrupt_file(tmp_path: Path) -> None:
    """A malformed JSON file does not crash resume."""
    store = tmp_path / "loops.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("not valid json")

    reg = _registry(store_path=store)
    settings = _stub_settings()

    resumed = resume_check_loops(reg, settings)
    assert resumed == []


# ---------------------------------------------------------------------------
# MIN_CHECK_LOOP_INTERVAL_SECONDS constant
# ---------------------------------------------------------------------------


def test_min_check_loop_interval_constant() -> None:
    """The default ``min_check_loop_interval_seconds`` in settings is 60.0."""
    settings = _stub_settings()
    assert settings.min_check_loop_interval_seconds == 60.0


# ---------------------------------------------------------------------------
# include_previous_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_previous_result_off_by_default() -> None:
    """When include_previous_result is False, the prompt is passed unchanged."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)

    prompts: list[str] = []

    class _PromptSpy:
        async def stream(
            self,
            message: str,
            *,
            history: list[tuple[str, str]] | None = None,
            session_id: str | None = None,
            client_id: str | None = None,
            images: list[tuple[str, bytes]] | None = None,
        ) -> AsyncIterator[str]:
            prompts.append(message)
            yield "ok"

    lid = spawn_check_loop(
        session_id="c1",
        prompt="original prompt",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _PromptSpy(),
        max_iterations=2,
        include_previous_result=False,
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    # Both iterations received the exact same prompt.
    assert len(prompts) >= 2, f"Expected >= 2 prompts, got {len(prompts)}: {prompts}"
    for p in prompts:
        assert p == "original prompt"


@pytest.mark.asyncio
async def test_include_previous_result_prepends_previous_to_prompt() -> None:
    """The second iteration receives the first result prepended to the prompt."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)

    prompts: list[str] = []

    class _PromptSpy:
        async def stream(
            self,
            message: str,
            *,
            history: list[tuple[str, str]] | None = None,
            session_id: str | None = None,
            client_id: str | None = None,
            images: list[tuple[str, bytes]] | None = None,
        ) -> AsyncIterator[str]:
            prompts.append(message)
            yield f"result-{len(prompts)}"

    lid = spawn_check_loop(
        session_id="c1",
        prompt="check board",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _PromptSpy(),
        max_iterations=2,
        include_previous_result=True,
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    assert len(prompts) >= 2, f"Expected >= 2 prompts, got {len(prompts)}: {prompts}"
    # First iteration: original prompt only.
    assert prompts[0] == "check board"
    # Second iteration: includes previous result.
    assert "Previous check result:" in prompts[1]
    assert "result-1" in prompts[1]
    assert "Current check prompt:" in prompts[1]
    assert "check board" in prompts[1]


# ---------------------------------------------------------------------------
# suppress_when
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suppress_when_suppresses_sse_frame() -> None:
    """When suppress_when returns True, no loop_tick SSE frame is published."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    q_sse = bus.subscribe("c1")

    lid = spawn_check_loop(
        session_id="c1",
        prompt="check",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["NO_CHANGE"]),
        max_iterations=1,
        suppress_when=lambda r: r.strip().upper().startswith("NO_CHANGE"),
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    # The loop still recorded the tick internally.
    info = reg.get(lid)
    assert info is not None
    assert info.iterations == 1
    assert info.last_result == "NO_CHANGE"

    # No loop_tick frame was published to the SSE bus.
    frames: list[dict[str, Any]] = []
    while not q_sse.empty():
        frames.append(q_sse.get_nowait())
    tick_frames = [f for f in frames if f["type"] == SSE_LOOP_TICK_TYPE]
    assert len(tick_frames) == 0, (
        f"Expected zero loop_tick frames when suppressed, got {tick_frames}"
    )

    # The loop_started and loop_stopped frames are still emitted.
    started = [f for f in frames if f["type"] == SSE_LOOP_STARTED_TYPE]
    assert len(started) == 1


@pytest.mark.asyncio
async def test_suppress_when_false_still_publishes() -> None:
    """When suppress_when returns False, the tick IS published normally."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    q_sse = bus.subscribe("c1")

    lid = spawn_check_loop(
        session_id="c1",
        prompt="check",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["CHANGE: ticket X blocked"]),
        max_iterations=1,
        suppress_when=lambda r: "NO_CHANGE" in r.upper(),
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.iterations == 1
    assert info.last_result == "CHANGE: ticket X blocked"

    frames: list[dict[str, Any]] = []
    while not q_sse.empty():
        frames.append(q_sse.get_nowait())
    tick_frames = [f for f in frames if f["type"] == SSE_LOOP_TICK_TYPE]
    assert len(tick_frames) == 1
    assert tick_frames[0]["result"] == "CHANGE: ticket X blocked"


@pytest.mark.asyncio
async def test_suppress_when_still_records_tick_internally() -> None:
    """Even when suppressed, the loop still tracks iterations and last_result."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)

    lid = spawn_check_loop(
        session_id="c1",
        prompt="poll",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["NO_CHANGE"]),
        max_iterations=1,
        suppress_when=lambda r: True,
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.iterations == 1
    assert info.last_result == "NO_CHANGE"
    # Still stopped by max_iterations (suppression doesn't affect lifecycle).
    assert info.status == LoopStatus.STOPPED
    assert info.stop_reason == "max_iterations"


@pytest.mark.asyncio
async def test_include_previous_result_and_suppress_together() -> None:
    """include_previous_result and suppress_when work together for change detection."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    q_sse = bus.subscribe("c1")

    # Shared call counter across agent instances.
    call_count = 0

    class _AlternatingAgent:
        async def stream(
            self,
            message: str,
            *,
            history: list[tuple[str, str]] | None = None,
            session_id: str | None = None,
            client_id: str | None = None,
            images: list[tuple[str, bytes]] | None = None,
        ) -> AsyncIterator[str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield "ticket X is open"
            else:
                yield "NO_CHANGE"

    lid = spawn_check_loop(
        session_id="c1",
        prompt="check tickets",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _AlternatingAgent(),
        max_iterations=2,
        include_previous_result=True,
        suppress_when=lambda r: r.strip().upper().startswith("NO_CHANGE"),
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.iterations == 2

    # First tick (not suppressed) published; second tick (suppressed) did not.
    frames: list[dict[str, Any]] = []
    while not q_sse.empty():
        frames.append(q_sse.get_nowait())
    tick_frames = [f for f in frames if f["type"] == SSE_LOOP_TICK_TYPE]
    assert len(tick_frames) == 1, (
        f"Expected exactly 1 tick frame (first tick not suppressed, second "
        f"suppressed), got {len(tick_frames)}: {tick_frames}"
    )
    assert tick_frames[0]["result"] == "ticket X is open"


@pytest.mark.asyncio
async def test_unchanged_loop_skips_llm_after_first_no_change() -> None:
    """N consecutive unchanged polls produce 1 (not N) substantive LLM calls.

    When the previous tick's result matches the no-change predicate, the
    worker skips the LLM invocation entirely on subsequent ticks — reusing
    the previous result — so input tokens are not wasted on a foregone
    NO_CHANGE reply.
    """
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    q_sse = bus.subscribe("c1")  # subscribe before spawning so events are captured

    # Shared call counter across agent instances (factory creates a fresh
    # instance each tick, but the counter is captured by closure).
    call_count = 0

    class _CountingNoChangeAgent:
        async def stream(
            self,
            message: str,
            *,
            history: list[tuple[str, str]] | None = None,
            session_id: str | None = None,
            client_id: str | None = None,
            images: list[tuple[str, bytes]] | None = None,
        ) -> AsyncIterator[str]:
            nonlocal call_count
            call_count += 1
            yield "NO_CHANGE"

    lid = spawn_check_loop(
        session_id="c1",
        prompt="check status",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _CountingNoChangeAgent(),
        max_iterations=5,
        suppress_when=lambda r: r.strip().upper().startswith("NO_CHANGE"),
    )

    for _ in range(80):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    assert info.stop_reason == "max_iterations"

    # All 5 iterations were recorded internally …
    assert info.iterations == 5, (
        f"Expected 5 iterations recorded, got {info.iterations}"
    )
    # … but the LLM was invoked only once (the first tick).
    assert call_count == 1, f"Expected 1 LLM call (first tick), got {call_count}"

    # All ticks produced NO_CHANGE → all suppressed → zero published frames.
    frames: list[dict[str, Any]] = []
    while not q_sse.empty():
        frames.append(q_sse.get_nowait())
    tick_frames = [f for f in frames if f["type"] == SSE_LOOP_TICK_TYPE]
    assert len(tick_frames) == 0, (
        f"Expected 0 published tick frames, got {len(tick_frames)}"
    )


# ---------------------------------------------------------------------------
# record_tick publish=False
# ---------------------------------------------------------------------------


def test_record_tick_publish_false_skips_event_sink() -> None:
    """record_tick with publish=False updates state but doesn't publish SSE."""
    bus = EventBus()
    reg = _registry(sink=bus)
    q = bus.subscribe("c1")

    lid = reg.register(
        "c1",
        "quiet",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    q.get_nowait()  # consume started frame

    reg.record_tick(lid, result="suppressed result", next_run=1100.0, publish=False)

    info = reg.get(lid)
    assert info is not None
    assert info.iterations == 1
    assert info.last_result == "suppressed result"

    # No SSE frame was published.
    frames: list[dict[str, Any]] = []
    while not q.empty():
        frames.append(q.get_nowait())
    tick_frames = [f for f in frames if f["type"] == SSE_LOOP_TICK_TYPE]
    assert len(tick_frames) == 0


def test_record_tick_publish_true_still_publishes() -> None:
    """record_tick with publish=True (default) still publishes SSE frame."""
    bus = EventBus()
    reg = _registry(sink=bus)
    q = bus.subscribe("c1")

    lid = reg.register(
        "c1",
        "loud",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    q.get_nowait()  # consume started frame

    reg.record_tick(lid, result="visible", next_run=1100.0, publish=True)

    frames: list[dict[str, Any]] = []
    while not q.empty():
        frames.append(q.get_nowait())
    tick_frames = [f for f in frames if f["type"] == SSE_LOOP_TICK_TYPE]
    assert len(tick_frames) == 1
    assert tick_frames[0]["result"] == "visible"
    assert tick_frames[0]["iteration"] == 1


# Board-read gate — unit tests
# ---------------------------------------------------------------------------

GUARDRAIL_NOTICE = (
    "[guardrail] Status suppressed: the check "
    "produced a status report without reading "
    "the board (consult_mill was not called). "
    "No verified status is available this tick."
)


def test_gate_suppresses_when_no_board_read() -> None:
    """When verify_via_board=True and probe.count==0, result is suppressed."""
    probe = BoardReadProbe()
    result = _apply_board_read_gate(
        "board status: awaiting user", probe, verify_via_board=True, loop_id="L-test"
    )
    assert result == GUARDRAIL_NOTICE


def test_gate_passes_through_when_board_read() -> None:
    """When verify_via_board=True and probe.count>0, result passes through."""
    probe = BoardReadProbe()
    probe.note()
    result = _apply_board_read_gate(
        "board status: ok", probe, verify_via_board=True, loop_id="L-test"
    )
    assert result == "board status: ok"


def test_gate_passes_multiple_reads() -> None:
    """Multiple board reads still produce a verified result."""
    probe = BoardReadProbe()
    probe.note()
    probe.note()
    probe.note()
    result = _apply_board_read_gate(
        "all clear", probe, verify_via_board=True, loop_id="L-test"
    )
    assert result == "all clear"


def test_gate_noop_when_verify_via_board_false() -> None:
    """When verify_via_board=False, the gate is a no-op regardless of probe count."""
    probe = BoardReadProbe()
    result = _apply_board_read_gate(
        "fabricated status", probe, verify_via_board=False, loop_id="L-test"
    )
    assert result == "fabricated status"

    probe.note()
    result = _apply_board_read_gate(
        "real status", probe, verify_via_board=False, loop_id="L-test"
    )
    assert result == "real status"


def test_gate_noop_when_probe_is_none() -> None:
    """When probe is None, the gate is a no-op (non-gated path)."""
    result = _apply_board_read_gate(
        "some text", None, verify_via_board=True, loop_id="L-test"
    )
    assert result == "some text"


def test_gate_noop_when_result_is_empty() -> None:
    """An empty result is not suppressed — it passes through unchanged."""
    probe = BoardReadProbe()
    result = _apply_board_read_gate("", probe, verify_via_board=True, loop_id="L-test")
    assert result == ""


# ---------------------------------------------------------------------------
# Board-read gate — integration tests via spawn_check_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gated_loop_no_board_read_suppresses_tick() -> None:
    """A gated loop whose sub-agent never calls consult_mill publishes guardrail."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    settings.mill = type("MillStub", (), {"enabled": True})()

    lid = spawn_check_loop(
        session_id="c-gate-1",
        prompt="report board status",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["board is in state X at 12:34"]),
        max_iterations=1,
        verify_via_board=True,
    )

    # Wait for the single iteration to complete.
    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    # The result should be the guardrail notice, NOT the fabricated text.
    assert info.last_result == GUARDRAIL_NOTICE
    assert "board is in state X" not in (info.last_result or "")


class _BoardReadingAgent:
    """A stub agent that calls its own consult_mill tool during stream.

    Used to test the verified tick path — the gated factory wraps
    consult_mill in-place, so calling it increments the probe.
    """

    def __init__(self, tools: list[Any] | None = None) -> None:
        self._tools = tools or []

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
    ) -> AsyncIterator[str]:
        # Actually call consult_mill if present, so the probe sees it.
        for tool in self._tools:
            if getattr(tool, "__name__", None) == "consult_mill":
                await tool("check board status")
                break
        yield "board status verified: all normal"


@pytest.mark.asyncio
async def test_gated_loop_with_board_read_passes_through() -> None:
    """A gated loop whose sub-agent calls consult_mill → result passes through."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    settings.mill = type("MillStub", (), {"enabled": True})()

    # Provide a factory that returns a _BoardReadingAgent with a fake
    # consult_mill.  The gated factory will wrap it in-place so the probe
    # increments.
    def _factory(s: Any) -> _BoardReadingAgent:
        async def _fake_consult(request: str) -> str:
            return "board OK"

        _fake_consult.__name__ = "consult_mill"
        return _BoardReadingAgent(tools=[_fake_consult])

    lid = spawn_check_loop(
        session_id="c-gate-verify",
        prompt="report board status",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=_factory,
        max_iterations=1,
        verify_via_board=True,
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    # The verified result passes through unchanged.
    assert info.last_result == "board status verified: all normal"
    assert "guardrail" not in (info.last_result or "").lower()


@pytest.mark.asyncio
async def test_gated_loop_no_board_read_stop_when_sees_guardrail() -> None:
    """stop_when runs against the effective (suppressed) result, not fabricated text."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    settings.mill = type("MillStub", (), {"enabled": True})()

    # A stop_when that would match the fabricated text — must NOT fire.
    lid = spawn_check_loop(
        session_id="c-gate-2",
        prompt="report board status",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["CONDITION_MET: done"]),
        stop_when=lambda r: "CONDITION_MET" in r,
        max_iterations=None,  # no cap — only stop_when should stop it
        verify_via_board=True,
    )

    # Let a few ticks run; the fake text should be suppressed each time
    # so stop_when never sees "CONDITION_MET".
    await asyncio.sleep(0.3)

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.RUNNING  # not stopped by fabricated text
    assert info.iterations >= 1
    # Each tick result should be the guardrail notice.
    assert info.last_result == GUARDRAIL_NOTICE

    # Cleanup.
    reg.stop(lid, reason="test teardown")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_non_gated_loop_unchanged() -> None:
    """When verify_via_board=False (default), tick behavior is unchanged."""
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)

    lid = spawn_check_loop(
        session_id="c-ungated",
        prompt="report whatever",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["fabricated status without board read"]),
        max_iterations=1,
        # verify_via_board defaults to False
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    # The fabricated text passes through unchanged (no gate).
    assert info.last_result == "fabricated status without board read"


# ---------------------------------------------------------------------------
# Board-read gate — flag round-trips through persist/resume
# ---------------------------------------------------------------------------


def test_verify_via_board_round_trips_through_persist(tmp_path: Path) -> None:
    """``verify_via_board`` survives a persist→load→resume round-trip."""
    import json

    store = tmp_path / "loops.json"
    reg = _registry(store_path=store)

    lid = reg.register(
        "c1",
        "gated check",
        interval_seconds=90.0,
        max_iterations=3,
        coro=_fake_coro(),  # type: ignore[arg-type]
        reason="Check ticket status",
        verify_via_board=True,
    )

    data = json.loads(store.read_text())
    entry = next(e for e in data if e["id"] == lid)
    assert entry["verify_via_board"] is True


@pytest.mark.asyncio
async def test_verify_via_board_defaults_to_false_when_missing(tmp_path: Path) -> None:
    """A persisted entry without ``verify_via_board`` defaults to ``False``."""
    import json

    store = tmp_path / "loops.json"
    old_format = {
        "id": "L-old2",
        "client_id": "c-old2",
        "prompt": "old format loop",
        "interval_seconds": 60.0,
        "max_iterations": None,
        "iterations": 0,
        "status": "running",
        "last_result": None,
        "reason": None,
    }
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps([old_format], indent=2))

    reg = _registry(store_path=store)
    settings = _stub_settings()

    resumed = resume_check_loops(
        reg,
        settings,
        agent_factory=lambda s: _StubAgent(["resumed"]),
    )
    assert resumed == ["L-old2"]
    info = reg.get("L-old2")
    assert info is not None
    assert info.verify_via_board is False

    reg.stop("L-old2", reason="test teardown")
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Board-read gate — previous_result bypass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gated_loop_previous_result_bypasses_gate() -> None:
    """When include_previous_result=True, ticks after the first bypass the gate.

    The first tick has no previous_result → guardrail forces a board read.
    Subsequent ticks have a previous_result (which embodies a recent board
    view) → the soft guardrail allows the agent to skip consult_mill, and
    the gate passes through without suppression.
    """
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    settings.mill = type("MillStub", (), {"enabled": True})()

    # An agent that NEVER calls consult_mill — would be suppressed on
    # every tick without the previous_result bypass.
    lid = spawn_check_loop(
        session_id="c-gate-prev",
        prompt="report board status",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["status: no change"]),
        max_iterations=3,
        verify_via_board=True,
        include_previous_result=True,
    )

    # Wait for all iterations to complete.
    for _ in range(80):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    assert info.iterations == 3

    # First tick: no previous result → guardrail suppresses output.
    # Second and third ticks: previous result exists → soft guardrail
    # allows pass-through.  The last_result should be the agent's
    # actual output (not the guardrail notice), because the gate
    # allowed it through.
    assert info.last_result == "status: no change"
    assert "guardrail" not in (info.last_result or "").lower()


@pytest.mark.asyncio
async def test_gated_loop_first_tick_still_gated() -> None:
    """Even with include_previous_result=True, the very first tick is gated.

    No previous_result exists yet → the guardrail header forces a board
    read, and the gate suppresses unverified output.
    """
    bus = EventBus()
    reg = _registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    settings.mill = type("MillStub", (), {"enabled": True})()

    lid = spawn_check_loop(
        session_id="c-gate-first",
        prompt="report board status",
        interval_seconds=0.01,
        settings=settings,
        registry=reg,
        agent_factory=lambda s: _StubAgent(["fabricated status"]),
        max_iterations=1,
        verify_via_board=True,
        include_previous_result=True,
    )

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = reg.get(lid)
        if info and info.status != LoopStatus.RUNNING:
            break

    info = reg.get(lid)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    # First tick: no previous_result yet → gate suppresses.
    assert "guardrail" in (info.last_result or "").lower()
    assert "fabricated status" not in (info.last_result or "")


# ---------------------------------------------------------------------------
# Soft guardrail header — unit test
# ---------------------------------------------------------------------------


def test_gate_bypasses_when_has_previous_result() -> None:
    """When has_previous_result=True, the gate passes through regardless of probe."""
    probe = BoardReadProbe()
    # probe.count == 0, but has_previous_result=True → passthrough
    result = _apply_board_read_gate(
        "status unchanged",
        probe,
        verify_via_board=True,
        loop_id="L-test",
        has_previous_result=True,
    )
    assert result == "status unchanged"


# ---------------------------------------------------------------------------
# Dedup — (client_id, prompt) supersede on register
# ---------------------------------------------------------------------------


def test_dedup_supersedes_same_prompt_same_client() -> None:
    """AC1: registering a same-(client_id, prompt) loop stops the old one."""
    reg = _registry()
    lid1 = reg.register(
        "c1",
        "check health",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    assert reg.get(lid1) is not None
    info1 = reg.get(lid1)
    assert info1 is not None
    assert info1.status == LoopStatus.RUNNING

    lid2 = reg.register(
        "c1",
        "  check health  ",  # different whitespace, same stripped
        interval_seconds=120,
        max_iterations=3,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    # Old loop is STOPPED with supersede reason.
    info1 = reg.get(lid1)
    assert info1 is not None
    assert info1.status == LoopStatus.STOPPED
    assert info1.stop_reason == "superseded by a newer check loop"

    # New loop is RUNNING.
    info2 = reg.get(lid2)
    assert info2 is not None
    assert info2.status == LoopStatus.RUNNING
    assert info2.prompt == "  check health  "  # original prompt preserved
    assert info2.interval_seconds == 120
    assert info2.max_iterations == 3

    # Exactly one RUNNING loop for this (client_id, prompt).
    running = [
        lp
        for lp in reg.list_for_session("c1")
        if lp.status == LoopStatus.RUNNING and lp.prompt.strip() == "check health"
    ]
    assert len(running) == 1
    assert running[0].id == lid2


@pytest.mark.asyncio
async def test_dedup_supersedes_cancels_task() -> None:
    """AC1: superseding a loop cancels its tracked asyncio.Task."""
    reg = _registry()

    # Create a real asyncio task that sleeps.
    async def _sleep_forever() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    task1 = asyncio.create_task(_sleep_forever())
    lid1 = reg.register(
        "c1",
        "cancel me",
        interval_seconds=60,
        max_iterations=None,
        coro=task1,
    )

    assert not task1.done()
    assert not task1.cancelled()

    task2 = asyncio.create_task(_sleep_forever())
    lid2 = reg.register(
        "c1",
        "cancel me",
        interval_seconds=60,
        max_iterations=None,
        coro=task2,
    )

    # Old task should be cancelled.
    await asyncio.sleep(0.05)
    assert task1.cancelled() or task1.done()

    # Old loop is STOPPED.
    info1 = reg.get(lid1)
    assert info1 is not None
    assert info1.status == LoopStatus.STOPPED
    assert info1.stop_reason == "superseded by a newer check loop"

    # New loop is RUNNING.
    info2 = reg.get(lid2)
    assert info2 is not None
    assert info2.status == LoopStatus.RUNNING

    # Cleanup: cancel the new task too.
    task2.cancel()
    await asyncio.sleep(0.05)


def test_dedup_distinct_prompts_coexist() -> None:
    """AC2: two loops under same client_id with different prompts both stay RUNNING."""
    reg = _registry()
    lid1 = reg.register(
        "c1",
        "loop one",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    lid2 = reg.register(
        "c1",
        "loop two",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    info1 = reg.get(lid1)
    assert info1 is not None
    assert info1.status == LoopStatus.RUNNING
    info2 = reg.get(lid2)
    assert info2 is not None
    assert info2.status == LoopStatus.RUNNING
    assert len(reg.list_for_session("c1")) == 2


def test_dedup_same_prompt_different_client_coexist() -> None:
    """AC3: two loops with same prompt but different client_id both stay RUNNING."""
    reg = _registry()
    lid_a = reg.register(
        "client-a",
        "check db",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    lid_b = reg.register(
        "client-b",
        "check db",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    info_a = reg.get(lid_a)
    assert info_a is not None
    assert info_a.status == LoopStatus.RUNNING
    info_b = reg.get(lid_b)
    assert info_b is not None
    assert info_b.status == LoopStatus.RUNNING

    # Each client sees only its own loop.
    assert len(reg.list_for_session("client-a")) == 1
    assert len(reg.list_for_session("client-b")) == 1


def test_dedup_terminal_loops_ignored() -> None:
    """AC4: STOPPED/FAILED same-prompt loops are not re-stopped and don't block."""
    reg = _registry()

    # Register and stop a loop.
    lid_stopped = reg.register(
        "c1",
        "check x",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    reg.stop(lid_stopped, reason="user request")
    info_stopped_pre = reg.get(lid_stopped)
    assert info_stopped_pre is not None
    assert info_stopped_pre.status == LoopStatus.STOPPED

    # Register and fail a loop.
    lid_failed = reg.register(
        "c1",
        "check x",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    reg.fail(lid_failed, error="boom")
    info_failed_pre = reg.get(lid_failed)
    assert info_failed_pre is not None
    assert info_failed_pre.status == LoopStatus.FAILED

    # Register a third loop with the same prompt — neither terminal loop
    # should be re-stopped, and the new loop registers successfully.
    lid_new = reg.register(
        "c1",
        "check x",
        interval_seconds=120,
        max_iterations=5,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    # Terminal loops unchanged.
    info_stopped = reg.get(lid_stopped)
    assert info_stopped is not None
    assert info_stopped.status == LoopStatus.STOPPED
    assert info_stopped.stop_reason == "user request"
    info_failed = reg.get(lid_failed)
    assert info_failed is not None
    assert info_failed.status == LoopStatus.FAILED
    assert info_failed.error == "boom"

    # New loop is RUNNING.
    info_new = reg.get(lid_new)
    assert info_new is not None
    assert info_new.status == LoopStatus.RUNNING

    # Only the new one is RUNNING for this prompt.
    running = [
        lp
        for lp in reg.list_for_session("c1")
        if lp.status == LoopStatus.RUNNING and lp.prompt.strip() == "check x"
    ]
    assert len(running) == 1
    assert running[0].id == lid_new


def test_dedup_false_skips_supersede() -> None:
    """AC5: register(..., dedup=False) never supersedes existing loops."""
    reg = _registry()
    lid1 = reg.register(
        "c1",
        "no dedup check",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    # Register again with dedup=False — old loop should stay RUNNING.
    lid2 = reg.register(
        "c1",
        "no dedup check",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
        dedup=False,
    )

    # Both loops are RUNNING.
    info1 = reg.get(lid1)
    assert info1 is not None
    assert info1.status == LoopStatus.RUNNING
    info2 = reg.get(lid2)
    assert info2 is not None
    assert info2.status == LoopStatus.RUNNING

    # Two loops for this client.
    assert len(reg.list_for_session("c1")) == 2


def test_dedup_event_sink_publishes_stopped_frame() -> None:
    """Superseding publishes a loop_stopped frame via the event sink."""
    bus = EventBus()
    reg = _registry(sink=bus)
    q = bus.subscribe("c1")

    lid1 = reg.register(
        "c1",
        "with events",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    q.get_nowait()  # consume started frame

    lid2 = reg.register(
        "c1",
        "with events",
        interval_seconds=120,
        max_iterations=5,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    # The stopped frame should be published for lid1.
    frames: list[dict[str, Any]] = []
    while not q.empty():
        frames.append(q.get_nowait())

    # We should see: started(lid2) and also stopped(lid1).
    started = [f for f in frames if f["type"] == SSE_LOOP_STARTED_TYPE]
    stopped = [f for f in frames if f["type"] == SSE_LOOP_STOPPED_TYPE]
    assert len(started) == 1
    assert started[0]["loop_id"] == lid2
    assert len(stopped) == 1
    assert stopped[0]["loop_id"] == lid1
    assert stopped[0]["reason"] == "superseded by a newer check loop"


# ---------------------------------------------------------------------------
# stop_all_for_session — session-close cleanup
# ---------------------------------------------------------------------------


def test_stop_all_for_session_stops_only_that_session() -> None:
    """Stops every RUNNING loop for the session, leaving other sessions alone."""
    reg = _registry()
    reg.register(
        "sess-a",
        "loop a1",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    reg.register(
        "sess-a",
        "loop a2",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    reg.register(
        "sess-b",
        "loop b1",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )

    stopped = reg.stop_all_for_session("sess-a", reason="session closed")

    assert stopped == 2
    assert all(
        loop.status == LoopStatus.STOPPED and loop.stop_reason == "session closed"
        for loop in reg.list_for_session("sess-a")
    )
    assert all(
        loop.status == LoopStatus.RUNNING for loop in reg.list_for_session("sess-b")
    )


def test_stop_all_for_session_unknown_returns_zero() -> None:
    """Stopping a session with no loops is a harmless no-op."""
    reg = _registry()
    assert reg.stop_all_for_session("ghost", reason="x") == 0


def test_stop_all_for_session_skips_already_stopped() -> None:
    """An already-stopped loop is not re-counted."""
    reg = _registry()
    lid = reg.register(
        "sess-a",
        "loop",
        interval_seconds=60,
        max_iterations=None,
        coro=_fake_coro(),  # type: ignore[arg-type]
    )
    reg.stop(lid, reason="manual")
    assert reg.stop_all_for_session("sess-a", reason="session closed") == 0

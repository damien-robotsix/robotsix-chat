"""Tests for :mod:`robotsix_chat.chat.runner` — background sub-agent spawning."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from robotsix_chat.chat.runner import (
    TaskCapacityError,
    spawn_subagent_task,
    task_completed_frame,
    task_failed_frame,
)
from robotsix_chat.chat.tasks import TaskRegistry, TaskStatus
from robotsix_chat.config import Settings
from tests.conftest import MockAgent

# ---------------------------------------------------------------------------
# Stubs / fakes
# ---------------------------------------------------------------------------


class _FakeDeliveryChannel:
    """A :class:`DeliveryChannel` that records every published frame."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, client_id: str, frame: dict[str, Any]) -> None:
        self.frames.append((client_id, frame))


class _BoomChannel:
    """A delivery channel whose ``publish`` always raises."""

    async def publish(self, client_id: str, frame: dict[str, Any]) -> None:
        raise RuntimeError("channel offline")


# ---------------------------------------------------------------------------
# Frame builder shape tests
# ---------------------------------------------------------------------------


def test_task_completed_frame_shape() -> None:
    """The completed frame has exactly the three expected keys."""
    frame = task_completed_frame("t42", "all done")
    assert frame == {"type": "task_completed", "task_id": "t42", "result": "all done"}


def test_task_failed_frame_shape() -> None:
    """The failed frame has exactly the three expected keys."""
    frame = task_failed_frame("t99", "timeout")
    assert frame == {"type": "task_failed", "task_id": "t99", "error": "timeout"}


# ---------------------------------------------------------------------------
# spawn_subagent_task — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_success_completes_and_publishes() -> None:
    """A stub agent runs to completion → registry updated + frame published."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()  # defaults (model_level=3)

    agent = MockAgent(["hi", " there"])

    def agent_factory(s: Settings) -> MockAgent:
        return agent

    tid = spawn_subagent_task(
        session_id="c1",
        prompt="greet",
        settings=settings,
        registry=registry,
        channel=channel,
        agent_factory=agent_factory,
    )

    # The task should return a non-empty id immediately.
    assert tid

    # Wait for the worker to finish.
    await asyncio.sleep(0.1)

    # Registry should show COMPLETED.
    info = registry.get(tid)
    assert info is not None
    assert info.status == TaskStatus.COMPLETED
    assert info.result == "hi there"

    # Channel should have received exactly one task_completed frame.
    assert len(channel.frames) == 1
    cid, frame = channel.frames[0]
    assert cid == "c1"
    assert frame["type"] == "task_completed"
    assert frame["task_id"] == tid
    assert frame["result"] == "hi there"


@pytest.mark.asyncio
async def test_spawn_success_returns_immediately() -> None:
    """spawn_subagent_task returns a task_id synchronously (no await needed)."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    # Use an agent that blocks until we unblock it — this proves the spawn
    # returns before the agent runs.
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
        ) -> AsyncIterator[str]:
            started.set()
            await finish.wait()
            yield "done"

    agent = _SlowAgent()

    def agent_factory(s: Settings) -> _SlowAgent:
        return agent

    tid = spawn_subagent_task(
        session_id="c1",
        prompt="slow",
        settings=settings,
        registry=registry,
        channel=channel,
        agent_factory=agent_factory,
    )

    # The function returned synchronously with a non-empty id.
    assert tid

    # The task should have been registered but not yet completed.
    info = registry.get(tid)
    assert info is not None
    assert info.status == TaskStatus.RUNNING

    # The worker should have started (it awaits the future, then calls stream).
    await asyncio.wait_for(started.wait(), timeout=1.0)

    # Still RUNNING — the agent hasn't yielded yet.
    info = registry.get(tid)
    assert info is not None
    assert info.status == TaskStatus.RUNNING

    # Let the agent finish.
    finish.set()
    await asyncio.sleep(0.1)

    # Now it should be COMPLETED.
    info = registry.get(tid)
    assert info is not None
    assert info.status == TaskStatus.COMPLETED
    assert info.result == "done"


# ---------------------------------------------------------------------------
# spawn_subagent_task — failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_failure_updates_registry_and_publishes() -> None:
    """A failing agent → registry shows FAILED + failure frame published."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()
    exc = ValueError("bad input")

    agent = MockAgent(error=exc)

    def agent_factory(s: Settings) -> MockAgent:
        return agent

    tid = spawn_subagent_task(
        session_id="c2",
        prompt="will fail",
        settings=settings,
        registry=registry,
        channel=channel,
        agent_factory=agent_factory,
    )

    assert tid

    # Wait for the worker to finish (it fails immediately).
    await asyncio.sleep(0.1)

    info = registry.get(tid)
    assert info is not None
    assert info.status == TaskStatus.FAILED
    assert info.error == "bad input"

    # Channel should have one task_failed frame.
    assert len(channel.frames) == 1
    cid, frame = channel.frames[0]
    assert cid == "c2"
    assert frame["type"] == "task_failed"
    assert frame["task_id"] == tid
    assert frame["error"] == "bad input"


@pytest.mark.asyncio
async def test_spawn_channel_error_is_suppressed() -> None:
    """When channel.publish raises, the error is logged not propagated."""
    registry = TaskRegistry()
    channel = _BoomChannel()
    settings = Settings()

    agent = MockAgent(["ok"])

    def agent_factory(s: Settings) -> MockAgent:
        return agent

    tid = spawn_subagent_task(
        session_id="c3",
        prompt="boom channel",
        settings=settings,
        registry=registry,
        channel=channel,
        agent_factory=agent_factory,
    )

    await asyncio.sleep(0.1)

    # The task must still be COMPLETED — channel failure does not undo success.
    info = registry.get(tid)
    assert info is not None
    assert info.status == TaskStatus.COMPLETED
    assert info.result == "ok"


# ---------------------------------------------------------------------------
# spawn_subagent_task — same-tier check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_factory_receives_settings() -> None:
    """The runner passes the exact Settings to the agent_factory."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings(llmio_model_level=2, llmio_api_key="test-key")

    received: list[Settings] = []

    def spy_factory(s: Settings) -> MockAgent:
        received.append(s)
        return MockAgent(["x"])

    _task_id = spawn_subagent_task(
        session_id="c4",
        prompt="tier check",
        settings=settings,
        registry=registry,
        channel=channel,
        agent_factory=spy_factory,
    )

    await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0] is settings
    assert received[0].llmio_model_level == 2


@pytest.mark.asyncio
async def test_default_agent_factory_uses_create_agent_from_settings() -> None:
    """The default factory routes through create_agent_from_settings."""
    import inspect

    from robotsix_chat.chat.runner import _default_agent_factory

    # Verify the default factory is exactly our module-level wrapper.
    assert _default_agent_factory.__name__ == "_default_agent_factory"
    # The default factory wraps create_agent_from_settings — confirm it's the
    # same function object reference used at module level.
    source = inspect.getsource(_default_agent_factory)
    assert "create_agent_from_settings" in source
    assert "settings" in source
    assert "model_level" not in source  # never hard-coded in the runner


# ---------------------------------------------------------------------------
# spawn_subagent_task — task-id handshake (race-free)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_id_handshake_consistent() -> None:
    """The task_id the worker sees matches the returned id."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    class _IdCapturingAgent:
        """Captures the task_id indirectly.

        The frame's task_id comes from the worker coroutine, which gets
        it via the id_future handshake.
        """

        async def stream(
            self,
            message: str,
            *,
            history: list[tuple[str, str]] | None = None,
            session_id: str | None = None,
            client_id: str | None = None,
        ) -> AsyncIterator[str]:
            yield "captured"

    # We'll verify consistency by checking the frame's task_id against the
    # returned id. The frame is built *inside* the worker, so it uses the
    # id the worker read from the future.
    tid = spawn_subagent_task(
        session_id="c5",
        prompt="handshake",
        settings=settings,
        registry=registry,
        channel=channel,
        agent_factory=lambda s: _IdCapturingAgent(),  # noqa: E731
    )

    await asyncio.sleep(0.1)

    assert len(channel.frames) == 1
    _, frame = channel.frames[0]
    assert frame["task_id"] == tid  # worker's id matches returned id


# ---------------------------------------------------------------------------
# spawn_subagent_task — concurrency cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_respects_capacity_limit() -> None:
    """``spawn_subagent_task`` raises ``TaskCapacityError`` when at capacity."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings(max_background_tasks=1)

    # Inject a running task so the cap is reached.
    _tid_existing = registry.register(
        "c1", "existing", asyncio.create_task(asyncio.sleep(0))
    )

    assert registry.count_running() == 1
    assert registry.count_running() >= settings.max_background_tasks

    with pytest.raises(TaskCapacityError, match="background-task limit reached"):
        spawn_subagent_task(
            session_id="c1",
            prompt="should be rejected",
            settings=settings,
            registry=registry,
            channel=channel,
            agent_factory=lambda s: MockAgent(["nope"]),
        )

    # No new task was scheduled — the registry count is unchanged.
    assert registry.count_running() == 1


@pytest.mark.asyncio
async def test_spawn_allows_when_below_capacity() -> None:
    """``spawn_subagent_task`` succeeds when count is below the cap."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings(max_background_tasks=2)

    # One running task — still below the cap of 2.
    registry.register("c1", "existing", asyncio.create_task(asyncio.sleep(0)))

    assert registry.count_running() == 1
    assert registry.count_running() < settings.max_background_tasks

    tid = spawn_subagent_task(
        session_id="c1",
        prompt="allowed",
        settings=settings,
        registry=registry,
        channel=channel,
        agent_factory=lambda s: MockAgent(["ok"]),
    )

    assert tid
    # A new running entry was added (the cap wasn't hit).
    assert registry.count_running() == 2

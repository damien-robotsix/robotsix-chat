"""End-to-end lifecycle tests for the background sub-agent subsystem.

Exercises the wired subsystem end to end: delegation via the
``delegate_task`` tool, the full ``task_started → task_completed`` lifecycle
on the happy path, and ``task_started → task_failed`` on the failure path,
with frames observed as delivered to a connected client/subscriber through
the EventBus.

Also exercises the ``ConversationDeliveryChannel`` integration: completed/
failed task results are recorded into the originating conversation's
``ConversationStore`` history.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.delegation import (
    ConversationDeliveryChannel,
    build_delegation_tools,
)
from robotsix_chat.chat.events import EventBus
from robotsix_chat.chat.tasks import TaskRegistry
from robotsix_chat.config import Settings
from tests.conftest import MockAgent

# ---------------------------------------------------------------------------
# EventBus → DeliveryChannel adapter
# ---------------------------------------------------------------------------


class _EventBusChannel:
    """Adapts an :class:`EventBus` to the :class:`DeliveryChannel` Protocol."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def publish(self, client_id: str, frame: dict[str, Any]) -> None:
        self._bus.publish(client_id, frame)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _consume_frame(
    q: asyncio.Queue[dict[str, object]], timeout: float = 2.0
) -> dict[str, object]:
    """Read one frame from *q* with a timeout (fail-fast on hang)."""
    return await asyncio.wait_for(q.get(), timeout=timeout)


def _drain_frames(
    q: asyncio.Queue[dict[str, object]], max_count: int = 8
) -> list[dict[str, object]]:
    """Drain all currently-available frames from *q* (non-blocking)."""
    frames: list[dict[str, object]] = []
    for _ in range(max_count):
        try:
            frames.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return frames


# ---------------------------------------------------------------------------
# Happy-path e2e
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_happy_path_task_started_to_completed() -> None:
    """Delegate a task → observe ``task_started`` then ``task_completed``."""
    bus = EventBus()
    # Wire both paths: registry.event_sink and the channel both publish to
    # the same EventBus, so duplicate frames are expected.  The test drains
    # all frames and asserts the required lifecycle types are present.
    registry = TaskRegistry(event_sink=bus)
    channel = _EventBusChannel(bus)
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        client_id="c1",
        agent_factory=lambda s: MockAgent(tokens=["result:", " 42"]),
    )
    delegate_task_fn = tools[0]

    q = bus.subscribe("c1")

    result = await delegate_task_fn("solve the ultimate question")

    assert isinstance(result, str)
    assert "task" in result.lower()

    # Let the worker coroutine finish.
    await asyncio.sleep(0.1)

    # Drain all frames — we expect at least task_started and task_completed.
    frames = _drain_frames(q)
    types = [f["type"] for f in frames]

    assert "task_started" in types, f"missing task_started in {types}"
    assert "task_completed" in types, f"missing task_completed in {types}"

    # The task_id should be consistent across frames for the same task.
    started = next(f for f in frames if f["type"] == "task_started")
    completed = next(f for f in frames if f["type"] == "task_completed")
    assert started["task_id"] == completed["task_id"]
    assert completed["result"] == "result: 42"

    # Registry shows COMPLETED.
    info = registry.get(str(started["task_id"]))
    assert info is not None
    assert info.status == "completed"


# ---------------------------------------------------------------------------
# Failure-path e2e
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_failure_path_task_started_to_failed() -> None:
    """Delegate a task that raises → observe ``task_started`` then ``task_failed``."""
    bus = EventBus()
    registry = TaskRegistry(event_sink=bus)
    channel = _EventBusChannel(bus)
    settings = Settings()

    exc = ValueError("bad input")

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        client_id="c2",
        agent_factory=lambda s: MockAgent(error=exc),
    )
    delegate_task_fn = tools[0]

    q = bus.subscribe("c2")

    result = await delegate_task_fn("risky operation")

    assert isinstance(result, str)

    # Let the worker finish (it fails immediately).
    await asyncio.sleep(0.1)

    frames = _drain_frames(q)
    types = [f["type"] for f in frames]

    assert "task_started" in types, f"missing task_started in {types}"
    assert "task_failed" in types, f"missing task_failed in {types}"

    started = next(f for f in frames if f["type"] == "task_started")
    failed = next(f for f in frames if f["type"] == "task_failed")
    assert started["task_id"] == failed["task_id"]
    assert failed["error"] == "bad input"

    info = registry.get(str(started["task_id"]))
    assert info is not None
    assert info.status == "failed"
    assert info.error == "bad input"


# ---------------------------------------------------------------------------
# Quick sanity: frames delivered only to the subscribed client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_frames_isolated_per_client() -> None:
    """Frames for one client are not delivered to another client's subscriber."""
    bus = EventBus()
    registry = TaskRegistry(event_sink=bus)
    channel = _EventBusChannel(bus)
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        client_id="client-a",
        agent_factory=lambda s: MockAgent(["ok"]),
    )
    delegate_task_fn = tools[0]

    q_a = bus.subscribe("client-a")
    q_b = bus.subscribe("client-b")

    await delegate_task_fn("task for a")

    await asyncio.sleep(0.1)

    # client-a gets frames.
    frames_a = _drain_frames(q_a)
    types_a = [f["type"] for f in frames_a]
    assert "task_started" in types_a
    assert "task_completed" in types_a

    # client-b gets nothing.
    assert q_b.empty()


# ---------------------------------------------------------------------------
# ConversationDeliveryChannel integration e2e
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_conversation_delivery_channel_completed() -> None:
    """Completed background-task result lands in the originating conversation store.

    The next agent turn will see it in history.
    """
    bus = EventBus()
    registry = TaskRegistry(event_sink=bus)
    store = ConversationStore(
        idle_reset_seconds=3600.0,
        max_history_turns=10,
    )
    # Create an owner + default session (simulates first GET /sessions).
    store.create_session("c-e2e")

    channel = ConversationDeliveryChannel(store)
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        client_id="c-e2e",
        agent_factory=lambda s: MockAgent(tokens=["findings: 99"]),
    )
    delegate_task_fn = tools[0]

    result = await delegate_task_fn("investigate the issue")
    assert isinstance(result, str)

    # Let the worker finish (it writes to the store via the channel).
    await asyncio.sleep(0.1)

    # The store now contains the synthetic turn in the active session.
    sessions, active_id = store.list_sessions("c-e2e")
    history = store.history(active_id)
    assert len(history) >= 1

    # The turn conveys the completed task result.
    user_msg, assistant_msg = history[-1]
    assert "Background task" in user_msg
    assert "completed" in user_msg
    assert assistant_msg == "findings: 99"


@pytest.mark.asyncio
async def test_e2e_conversation_delivery_channel_failed() -> None:
    """Failed background-task error lands in the originating conversation store."""
    bus = EventBus()
    registry = TaskRegistry(event_sink=bus)
    store = ConversationStore(
        idle_reset_seconds=3600.0,
        max_history_turns=10,
    )
    store.create_session("c-e2e-fail")

    channel = ConversationDeliveryChannel(store)
    settings = Settings()

    exc = ValueError("bad input")

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        client_id="c-e2e-fail",
        agent_factory=lambda s: MockAgent(error=exc),
    )
    delegate_task_fn = tools[0]

    await delegate_task_fn("risky work")

    await asyncio.sleep(0.1)

    sessions, active_id = store.list_sessions("c-e2e-fail")
    history = store.history(active_id)
    assert len(history) >= 1

    user_msg, assistant_msg = history[-1]
    assert "Background task" in user_msg
    assert "failed" in user_msg
    assert "Error: bad input" in assistant_msg

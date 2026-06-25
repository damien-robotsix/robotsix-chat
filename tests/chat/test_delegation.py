"""Tests for :mod:`robotsix_chat.chat.delegation` (delegate_task & start_check_loop)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from itertools import count
from typing import Any

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.delegation import (
    ConversationDeliveryChannel,
    NullDeliveryChannel,
    _is_board_action,
    build_check_loop_tools,
    build_delegation_tools,
)
from robotsix_chat.chat.events import (
    SSE_LOOP_STARTED_TYPE,
    SSE_LOOP_STOPPED_TYPE,
    SSE_LOOP_TICK_TYPE,
    EventBus,
)
from robotsix_chat.chat.loops import (
    CheckLoopRegistry,
)
from robotsix_chat.chat.runner import (
    task_started_frame,
)
from robotsix_chat.chat.server import create_agent_from_settings
from robotsix_chat.chat.tasks import TaskRegistry, TaskStatus
from robotsix_chat.config import Settings

# ---------------------------------------------------------------------------
# Fakes
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


# ---------------------------------------------------------------------------
# build_delegation_tools
# ---------------------------------------------------------------------------


def test_build_delegation_tools_returns_one_callable() -> None:
    """``build_delegation_tools`` returns exactly one callable."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(settings, registry, channel)
    assert len(tools) == 1
    assert callable(tools[0])


def test_delegate_task_has_docstring() -> None:
    """The delegate_task closure has a docstring (used as the LLM tool description)."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(settings, registry, channel)
    assert tools[0].__doc__ is not None
    assert "background" in tools[0].__doc__


# ---------------------------------------------------------------------------
# delegate_task — immediate return
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_task_returns_string_with_task_id() -> None:
    """Calling delegate_task returns a str containing the task id immediately."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    delegate_task = tools[0]

    result = await delegate_task("do some research")
    assert isinstance(result, str)
    assert "task" in result.lower()
    # The result mentions the task id.
    assert any(tid for tid in result.split() if len(tid) >= 8)


@pytest.mark.asyncio
async def test_delegate_task_does_not_await_completion() -> None:
    """The tool returns before the background agent finishes.

    We use an agent that blocks until we unblock it — the tool must return
    while the agent is still running.
    """
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

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
            await finish.wait()
            yield "finally done"

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        agent_factory=lambda s: _SlowAgent(),
    )
    delegate_task = tools[0]

    result = await delegate_task("slow work")

    # The tool returned immediately with a task id.
    assert isinstance(result, str)
    task_id = _extract_task_id(result)
    assert task_id

    # The registry shows the task is still RUNNING.
    info = registry.get(task_id)
    assert info is not None
    assert info.status == TaskStatus.RUNNING

    # Unblock the agent and let it finish.
    finish.set()
    await asyncio.sleep(0.1)

    # Now it should be COMPLETED.
    info = registry.get(task_id)
    assert info is not None
    assert info.status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# Registry tracking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_registered_with_correct_session_id() -> None:
    """The registered task's session_id matches the one passed to the factory."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        session_id="session-42",
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    delegate_task = tools[0]

    result = await delegate_task("session-aware work")
    task_id = _extract_task_id(result)

    info = registry.get(task_id)
    assert info is not None
    assert info.session_id == "session-42"
    assert info.prompt == "session-aware work"


@pytest.mark.asyncio
async def test_task_session_id_defaults_to_empty_string() -> None:
    """When ``session_id`` is not passed to the factory, the task uses ``""``."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    delegate_task = tools[0]

    result = await delegate_task("anonymous work")
    task_id = _extract_task_id(result)

    info = registry.get(task_id)
    assert info is not None
    assert info.session_id == ""


@pytest.mark.asyncio
async def test_returned_id_matches_registry_entry() -> None:
    """The task id in the return string matches an entry in the registry."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        agent_factory=lambda s: _StubAgent(["done"]),
    )
    delegate_task = tools[0]

    result = await delegate_task("find the answer")
    task_id = _extract_task_id(result)

    info = registry.get(task_id)
    assert info is not None
    assert info.prompt == "find the answer"
    assert info.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED)


# ---------------------------------------------------------------------------
# task_started frame publishing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_started_frame_published() -> None:
    """The fake channel receives a ``task_started`` frame."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        agent_factory=lambda s: _StubAgent(["done"]),
    )
    delegate_task = tools[0]

    await delegate_task("frame test")

    # At least one frame should be task_started.
    types = [f["type"] for _, f in channel.frames]
    assert "task_started" in types


@pytest.mark.asyncio
async def test_task_started_frame_has_expected_shape() -> None:
    """The published ``task_started`` frame has the documented shape."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        agent_factory=lambda s: _StubAgent(["done"]),
    )
    delegate_task = tools[0]

    result = await delegate_task("shape test")
    task_id = _extract_task_id(result)

    # Find the task_started frame.
    started_frames = [f for cid, f in channel.frames if f["type"] == "task_started"]
    assert len(started_frames) == 1
    frame = started_frames[0]
    assert frame["task_id"] == task_id
    assert frame["prompt"] == "shape test"


# ---------------------------------------------------------------------------
# Channel errors are suppressed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_publish_error_not_propagated() -> None:
    """When ``channel.publish`` raises, the tool still returns normally."""
    registry = TaskRegistry()
    channel = _BoomChannel()
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        agent_factory=lambda s: _StubAgent(["done"]),
    )
    delegate_task = tools[0]

    # Should not raise.
    result = await delegate_task("boom")
    task_id = _extract_task_id(result)
    assert task_id

    # The task is still registered (the error was swallowed).
    info = registry.get(task_id)
    assert info is not None
    assert info.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED)

    # Let the background agent finish so we don't leak tasks.
    await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# task_started_frame builder (runner.py)
# ---------------------------------------------------------------------------


def test_task_started_frame_builder_shape() -> None:
    """The runner's ``task_started_frame`` returns the expected dict."""
    frame = task_started_frame("t42", "do stuff")
    assert frame == {"type": "task_started", "task_id": "t42", "prompt": "do stuff"}


# ---------------------------------------------------------------------------
# Recursion guard: sub-agents have no delegate_task tool
# ---------------------------------------------------------------------------


def test_sub_agent_has_no_delegate_tool() -> None:
    """Verify sub-agents get no ``delegate_task`` tool.

    A sub-agent built via ``create_agent_from_settings(settings=...)``
    — without task_registry or delivery_channel — must NOT expose a
    ``delegate_task`` tool (neither in static tools nor in the per-request
    factory).
    """
    agent = create_agent_from_settings(settings=Settings())
    tools = agent._tools
    request_tools_factory = agent._request_tools_factory

    # Static tools: no delegate_task.
    if tools is not None:
        names = [getattr(t, "__name__", str(t)) for t in tools]
        assert "delegate_task" not in names, (
            "Sub-agent must not receive the delegate_task tool statically"
        )

    # Per-request factory: not set for sub-agents.
    assert request_tools_factory is None, (
        "Sub-agent must not have a request_tools_factory"
    )


def test_foreground_agent_gets_delegate_tool() -> None:
    """A foreground agent built with registry and channel gets the tool."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    agent = create_agent_from_settings(
        settings=settings,
        task_registry=registry,
        delivery_channel=channel,
    )
    # Static tools don't include delegate_task (it's in the per-request
    # factory so the closure captures client_id correctly).
    static_tools = agent._tools
    if static_tools is not None:
        names = [getattr(t, "__name__", str(t)) for t in static_tools]
        assert "delegate_task" not in names, (
            "delegate_task should be in request_tools_factory, not static tools"
        )

    # The per-request factory is set and, when called, returns the tool.
    assert agent._request_tools_factory is not None, (
        "Foreground agent must have a request_tools_factory"
    )
    per_req = agent._request_tools_factory("test-client")
    assert len(per_req) == 1
    assert per_req[0].__name__ == "delegate_task"


# ---------------------------------------------------------------------------
# NullDeliveryChannel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_delivery_channel_is_noop() -> None:
    """``NullDeliveryChannel.publish`` does not raise."""
    channel = NullDeliveryChannel()
    await channel.publish("c1", {"type": "task_started", "task_id": "x"})
    # No exception → success.


# ---------------------------------------------------------------------------
# ConversationDeliveryChannel
# ---------------------------------------------------------------------------


class TestConversationDeliveryChannel:
    """Tests for :class:`ConversationDeliveryChannel`."""

    @staticmethod
    def _store(**kwargs: Any) -> ConversationStore:
        return ConversationStore(
            idle_reset_seconds=3600.0,
            max_history_turns=10,
            max_conversations=5,
            **kwargs,
        )

    @staticmethod
    def _active_history(
        store: ConversationStore, session_id: str
    ) -> list[tuple[str, str]]:
        """Return the history of *session_id*.

        Background-task and loop results are recorded into the exact session
        that spawned them (``record_for_session``), so the published id IS the
        recording session id.
        """
        return store.history(session_id)

    @pytest.mark.asyncio
    async def test_completed_frame_records_turn(self) -> None:
        """A ``task_completed`` frame records a turn into the store."""
        store = self._store()
        store.create_session("c1")  # ensure the owner + default session exists
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "c1",
            {"type": "task_completed", "task_id": "t42", "result": "done: 42"},
        )

        history = self._active_history(store, "c1")
        assert len(history) == 1
        user_msg, assistant_msg = history[0]
        assert "t42" in user_msg
        assert "Background task" in user_msg
        assert "completed" in user_msg
        assert assistant_msg == "done: 42"

    @pytest.mark.asyncio
    async def test_failed_frame_records_turn(self) -> None:
        """A ``task_failed`` frame records a turn with the error."""
        store = self._store()
        store.create_session("c2")
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "c2",
            {"type": "task_failed", "task_id": "t99", "error": "timeout"},
        )

        history = self._active_history(store, "c2")
        assert len(history) == 1
        user_msg, assistant_msg = history[0]
        assert "t99" in user_msg
        assert "failed" in user_msg
        assert "Error: timeout" in assistant_msg

    @pytest.mark.asyncio
    async def test_started_frame_is_ignored(self) -> None:
        """A ``task_started`` frame does NOT create a history turn."""
        store = self._store()
        store.create_session("c3")
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "c3",
            {"type": "task_started", "task_id": "t1", "prompt": "do stuff"},
        )

        history = self._active_history(store, "c3")
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_unknown_frame_is_ignored(self) -> None:
        """An unknown frame type does NOT create a history turn."""
        store = self._store()
        store.create_session("c4")
        channel = ConversationDeliveryChannel(store)

        await channel.publish("c4", {"type": "some_unknown", "x": 1})

        history = self._active_history(store, "c4")
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_empty_client_id_is_noop(self) -> None:
        """An empty ``client_id`` is a no-op — no turn created."""
        store = self._store()
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "",
            {"type": "task_completed", "task_id": "t42", "result": "x"},
        )

        # No conversation was ever created for "".
        assert store.history("") == []

    @pytest.mark.asyncio
    async def test_nonexistent_session_is_lazily_created(self) -> None:
        """Publishing for a never-seen session lazily creates it and records.

        Under session-scoping a result is never silently dropped: if the tick
        fires for a session the store has not recorded a turn into yet, the
        session is created on demand so the result still lands.
        """
        store = self._store()
        channel = ConversationDeliveryChannel(store)

        # The store never had a create_session() call for "ghost".
        await channel.publish(
            "ghost",
            {"type": "task_completed", "task_id": "t1", "result": "landed"},
        )

        # record_for_session lazily creates the session and records the turn.
        history = store.history("ghost")
        assert len(history) == 1
        assert history[0][1] == "landed"

    @pytest.mark.asyncio
    async def test_completed_then_failed_only_last_recorded(self) -> None:
        """Two frames for the same client both land in history."""
        store = self._store()
        store.create_session("c5")
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "c5",
            {"type": "task_completed", "task_id": "ta", "result": "first"},
        )
        await channel.publish(
            "c5",
            {"type": "task_failed", "task_id": "tb", "error": "second"},
        )

        history = self._active_history(store, "c5")
        assert len(history) == 2
        assert history[0][1] == "first"
        assert "Error: second" in history[1][1]

    @pytest.mark.asyncio
    async def test_publish_does_not_raise_on_missing_keys(self) -> None:
        """Missing 'result' or 'error' keys don't crash publish."""
        store = self._store()
        store.create_session("c6")
        channel = ConversationDeliveryChannel(store)

        # task_completed without 'result' key — should default to "".
        await channel.publish(
            "c6",
            {"type": "task_completed", "task_id": "t1"},
        )

        history = self._active_history(store, "c6")
        assert len(history) == 1
        # assistant_reply is empty string (default from .get).
        assert history[0][1] == ""

    # -- loop_tick / loop_failed / loop_started / loop_stopped ---------

    @pytest.mark.asyncio
    async def test_loop_tick_frame_records_turn(self) -> None:
        """A ``loop_tick`` frame records a turn into the store."""
        store = self._store()
        store.create_session("c-loop")
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "c-loop",
            {
                "type": "loop_tick",
                "loop_id": "L42",
                "iteration": 3,
                "result": "price is $12.34",
                "next_run": 5000.0,
            },
        )

        history = self._active_history(store, "c-loop")
        assert len(history) == 1
        user_msg, assistant_msg = history[0]
        assert "L42" in user_msg
        assert "tick 3" in user_msg
        assert "Check loop" in user_msg
        assert assistant_msg == "price is $12.34"

    @pytest.mark.asyncio
    async def test_loop_failed_frame_records_turn(self) -> None:
        """A ``loop_failed`` frame records a turn with the error."""
        store = self._store()
        store.create_session("c-loop-fail")
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "c-loop-fail",
            {
                "type": "loop_failed",
                "loop_id": "L99",
                "error": "connection refused",
            },
        )

        history = self._active_history(store, "c-loop-fail")
        assert len(history) == 1
        user_msg, assistant_msg = history[0]
        assert "L99" in user_msg
        assert "failed" in user_msg
        assert "Check loop" in user_msg
        assert assistant_msg == "Error: connection refused"

    @pytest.mark.asyncio
    async def test_loop_started_frame_is_ignored(self) -> None:
        """A ``loop_started`` frame does NOT create a history turn."""
        store = self._store()
        store.create_session("c-loop-start")
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "c-loop-start",
            {
                "type": "loop_started",
                "loop_id": "L1",
                "prompt": "check weather",
                "interval_seconds": 120.0,
                "max_iterations": None,
            },
        )

        history = self._active_history(store, "c-loop-start")
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_loop_stopped_frame_is_ignored(self) -> None:
        """A ``loop_stopped`` frame does NOT create a history turn."""
        store = self._store()
        store.create_session("c-loop-stop")
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "c-loop-stop",
            {
                "type": "loop_stopped",
                "loop_id": "L1",
                "reason": "max_iterations",
                "iterations": 5,
            },
        )

        history = self._active_history(store, "c-loop-stop")
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_loop_tick_empty_client_id_is_noop(self) -> None:
        """An empty ``client_id`` is a no-op for loop_tick — no turn created."""
        store = self._store()
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "",
            {"type": "loop_tick", "loop_id": "L1", "iteration": 1, "result": "x"},
        )

        assert store.history("") == []

    @pytest.mark.asyncio
    async def test_loop_tick_missing_result_defaults_to_empty(self) -> None:
        """Missing 'result' key in loop_tick doesn't crash publish."""
        store = self._store()
        store.create_session("c-loop-missing")
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "c-loop-missing",
            {"type": "loop_tick", "loop_id": "L1", "iteration": 1},
        )

        history = self._active_history(store, "c-loop-missing")
        assert len(history) == 1
        assert history[0][1] == ""


# ---------------------------------------------------------------------------
# delegate_task — concurrency cap degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_task_at_capacity_returns_friendly_message() -> None:
    """When the cap is reached, ``delegate_task`` returns a friendly message.

    (no task id) and publishes **no** ``task_started`` frame.
    """
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings(max_background_tasks=1)

    # Fill the capacity so spawn_subagent_task will raise.
    registry.register("c1", "blocker", asyncio.create_task(asyncio.sleep(0)))
    assert registry.count_running() == 1

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        session_id="c1",
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    delegate_task_fn = tools[0]

    result = await delegate_task_fn("too many tasks")

    # Returns the friendly message — no task id substring.
    assert isinstance(result, str)
    assert "couldn't start" in result
    # Verify no 32-char hex task id in the response.

    assert not re.search(r"\b[0-9a-f]{32}\b", result)

    # No task_started frame was published to the channel.
    started_frames = [f for _, f in channel.frames if f["type"] == "task_started"]
    assert len(started_frames) == 0

    # The registry count is still 1 (no new task was registered).
    assert registry.count_running() == 1


# ---------------------------------------------------------------------------
# Helpers — check-loop tests
# ---------------------------------------------------------------------------


def _stub_settings(**overrides: Any) -> Any:
    """Build a stub settings object carrying all attrs needed by loops/agents.

    Uses ``types.SimpleNamespace`` so the worker only reads attributes
    without needing the real ``Settings`` pydantic model.
    """
    from types import SimpleNamespace

    defaults: dict[str, Any] = {
        "max_check_loops": 5,
        "min_check_loop_interval_seconds": 60.0,
        "llmio_model_level": 3,
        "llmio_api_key": "",
        "agent_instruction": "You are a test agent.",
        "mill": SimpleNamespace(enabled=False),
        "memory": SimpleNamespace(enabled=False),
        "calendar": SimpleNamespace(enabled=False),
        "refdocs": SimpleNamespace(enabled=False),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _loop_registry(
    sink: EventBus | None = None,
) -> CheckLoopRegistry:
    """Build a registry with deterministic loop ids (``L0``, ``L1``, …).

    Persistence is disabled (``store_path=None``).
    """
    ids = count()
    return CheckLoopRegistry(
        id_factory=lambda: f"L{next(ids)}",
        event_sink=sink,
        store_path=None,
    )


# ---------------------------------------------------------------------------
# build_check_loop_tools
# ---------------------------------------------------------------------------


def test_build_check_loop_tools_returns_three_callables() -> None:
    """``build_check_loop_tools`` returns three callables: start, stop, list."""
    registry = _loop_registry()
    settings = _stub_settings()

    tools = build_check_loop_tools(settings, registry)
    assert len(tools) == 3
    assert all(callable(t) for t in tools)
    names = [t.__name__ for t in tools]
    assert names == ["start_check_loop", "stop_check_loop", "list_check_loops"]


def test_start_check_loop_has_docstring() -> None:
    """The start_check_loop closure has a docstring (LLM tool description)."""
    registry = _loop_registry()
    settings = _stub_settings()

    tools = build_check_loop_tools(settings, registry)
    assert tools[0].__doc__ is not None
    assert "interval_seconds" in tools[0].__doc__
    assert "60 seconds" in tools[0].__doc__


# ---------------------------------------------------------------------------
# start_check_loop — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_check_loop_returns_string_with_loop_id() -> None:
    """Calling start_check_loop returns a str containing the loop id immediately."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        session_id="browser-42",
        agent_factory=lambda s: _StubAgent(["check result"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop("monitor the thing", interval_seconds=120.0)
    assert isinstance(result, str)
    assert "check loop" in result.lower()
    assert "L0" in result


@pytest.mark.asyncio
async def test_start_check_loop_registers_with_correct_session_id() -> None:
    """The registered loop's session_id matches the one passed to the factory."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        session_id="session-42",
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop("session-aware check", interval_seconds=120.0)
    loop_id = _extract_loop_id(result)

    info = registry.get(loop_id)
    assert info is not None
    assert info.session_id == "session-42"
    assert info.prompt == "session-aware check"


@pytest.mark.asyncio
async def test_start_check_loop_passes_max_iterations() -> None:
    """The max_iterations kwarg is forwarded to the registry."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop(
        "bounded check", interval_seconds=120.0, max_iterations=3
    )
    loop_id = _extract_loop_id(result)

    info = registry.get(loop_id)
    assert info is not None
    assert info.max_iterations == 3


@pytest.mark.asyncio
async def test_start_check_loop_forwards_reason_to_registry() -> None:
    """The ``reason`` kwarg is forwarded through to the registry on register."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop(
        "poll endpoint", interval_seconds=120.0, reason="Monitor API health"
    )
    loop_id = _extract_loop_id(result)

    info = registry.get(loop_id)
    assert info is not None
    assert info.reason == "Monitor API health"

    # Clean up.
    registry.stop(loop_id, reason="test teardown")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_start_check_loop_reason_omitted_is_none() -> None:
    """When ``reason`` is omitted, the loop's ``reason`` is ``None``."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop("no reason loop", interval_seconds=120.0)
    loop_id = _extract_loop_id(result)

    info = registry.get(loop_id)
    assert info is not None
    assert info.reason is None

    # Clean up.
    registry.stop(loop_id, reason="test teardown")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_start_check_loop_runs_first_iteration() -> None:
    """The stub agent runs to completion → first tick is recorded."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        agent_factory=lambda s: _StubAgent(["hello from loop"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop("test loop", interval_seconds=120.0)
    loop_id = _extract_loop_id(result)

    # Let the background worker finish its first iteration.
    await asyncio.sleep(0.1)

    info = registry.get(loop_id)
    assert info is not None
    assert info.iterations == 1
    assert info.last_result == "hello from loop"

    # Clean up: stop the loop so we don't leak tasks.
    registry.stop(loop_id, reason="test teardown")
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# start_check_loop — rejection paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_check_loop_interval_too_short_returns_friendly_message() -> None:
    """When the interval is below 60s, a friendly message is returned (no raise)."""
    registry = _loop_registry()
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        agent_factory=lambda s: _StubAgent(["nope"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop("too fast", interval_seconds=10.0)
    assert isinstance(result, str)
    assert "60 seconds" in result
    # No loop registered.
    assert registry.count_running() == 0


@pytest.mark.asyncio
async def test_start_check_loop_at_capacity_returns_friendly_message() -> None:
    """When the cap is reached, a friendly message is returned (no raise)."""
    registry = _loop_registry()
    settings = _stub_settings(max_check_loops=1)

    # Fill the capacity.
    registry.register(
        "c1",
        "blocker",
        interval_seconds=60,
        max_iterations=None,
        coro=asyncio.create_task(asyncio.sleep(0)),
    )
    assert registry.count_running() == 1

    tools = build_check_loop_tools(
        settings,
        registry,
        agent_factory=lambda s: _StubAgent(["nope"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop("should reject", interval_seconds=120.0)
    assert isinstance(result, str)
    assert "couldn't start" in result
    assert "too many" in result
    # Still only 1 running loop.
    assert registry.count_running() == 1


# ---------------------------------------------------------------------------
# start_check_loop — no duplicate loop_started frame
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_check_loop_does_not_publish_loop_started_frame() -> None:
    """The tool does NOT itself publish a loop_started frame.

    The registry's event_sink is the sole authoritative publisher — exactly
    one loop_started frame must reach the sink per start.
    """
    bus = EventBus()
    q = bus.subscribe("c1")
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        session_id="c1",
        agent_factory=lambda s: _StubAgent(["check done"]),
    )
    start_check_loop = tools[0]

    await start_check_loop("frame check", interval_seconds=120.0)

    # Collect frames from the bus.
    await asyncio.sleep(0.1)
    frames: list[dict[str, Any]] = []
    while not q.empty():
        frames.append(q.get_nowait())

    started_frames = [f for f in frames if f["type"] == SSE_LOOP_STARTED_TYPE]
    assert len(started_frames) == 1, (
        f"Expected exactly 1 loop_started frame from the registry's event_sink, "
        f"got {len(started_frames)}"
    )

    # The loop_started frame came from the registry, not the tool.
    frame = started_frames[0]
    assert frame["session_id"] == "c1"

    # Clean up.
    loop_id = frame["loop_id"]
    registry.stop(loop_id, reason="test teardown")
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# stop_check_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_check_loop_stops_running_loop() -> None:
    """Calling stop_check_loop on an owned running loop stops it."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        session_id="c1",
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    start_check_loop = tools[0]
    stop_check_loop = tools[1]

    result = await start_check_loop("loop to stop", interval_seconds=120.0)
    loop_id = _extract_loop_id(result)

    # Stop it.
    stop_result = await stop_check_loop(loop_id)
    assert "Stopped" in stop_result
    assert loop_id in stop_result

    # Registry confirms STOPPED.
    info = registry.get(loop_id)
    assert info is not None
    assert info.status.value == "stopped"


@pytest.mark.asyncio
async def test_stop_check_loop_different_client_not_stopped() -> None:
    """A loop owned by a different client_id is NOT stopped and returns not-found."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    # Start a loop as client "owner".
    tools_owner = build_check_loop_tools(
        settings,
        registry,
        session_id="owner",
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    result = await tools_owner[0]("loop for owner", interval_seconds=120.0)
    loop_id = _extract_loop_id(result)

    # Try to stop as client "intruder".
    tools_intruder = build_check_loop_tools(
        settings,
        registry,
        session_id="intruder",
        agent_factory=lambda s: _StubAgent(["nope"]),
    )
    stop_result = await tools_intruder[1](loop_id)
    assert "don't see" in stop_result.lower() or "not found" in stop_result.lower()

    # The original loop is still RUNNING (not stopped).
    info = registry.get(loop_id)
    assert info is not None
    assert info.status.value == "running"

    # Clean up.
    registry.stop(loop_id, reason="test teardown")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_stop_check_loop_unknown_id_returns_message() -> None:
    """Stopping a nonexistent loop id returns a not-found message without raising."""
    registry = _loop_registry()
    settings = _stub_settings()

    tools = build_check_loop_tools(settings, registry, session_id="c1")
    _, stop_check_loop, _ = tools

    result = await stop_check_loop("nonexistent-99")
    assert isinstance(result, str)
    assert "don't see" in result.lower() or "not found" in result.lower()


@pytest.mark.asyncio
async def test_stop_check_loop_idempotent() -> None:
    """Stopping an already-stopped loop returns a polite message (idempotent)."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        session_id="c1",
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    start_check_loop = tools[0]
    stop_check_loop = tools[1]

    result = await start_check_loop("loop to double-stop", interval_seconds=120.0)
    loop_id = _extract_loop_id(result)

    # First stop.
    await stop_check_loop(loop_id)
    info = registry.get(loop_id)
    assert info is not None
    assert info.status.value == "stopped"

    # Second stop — idempotent (no raise).
    result2 = await stop_check_loop(loop_id)
    assert "Stopped" in result2 or "don't see" in result2.lower()


# ---------------------------------------------------------------------------
# list_check_loops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_check_loops_empty() -> None:
    """list_check_loops returns a message when there are no loops."""
    registry = _loop_registry()
    settings = _stub_settings()

    tools = build_check_loop_tools(settings, registry, session_id="c1")
    _, _, list_check_loops = tools

    result = await list_check_loops()
    assert isinstance(result, str)
    assert "no check loops" in result.lower() or "none" in result.lower()


@pytest.mark.asyncio
async def test_list_check_loops_includes_owned_loop_ids() -> None:
    """list_check_loops returns a summary containing ids of owned loops."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        session_id="c1",
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    start_check_loop = tools[0]
    _, _, list_check_loops = tools

    await start_check_loop("alpha check", interval_seconds=120.0)
    await start_check_loop("beta check", interval_seconds=300.0)

    # Let first iterations land.
    await asyncio.sleep(0.15)

    result = await list_check_loops()
    assert "L0" in result
    assert "L1" in result
    assert "alpha" in result
    assert "beta" in result
    assert "running" in result

    # Clean up.
    registry.stop("L0", reason="test teardown")
    registry.stop("L1", reason="test teardown")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_list_check_loops_excludes_other_clients() -> None:
    """list_check_loops does not reveal loops belonging to other clients."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    # Start a loop as "alice".
    tools_alice = build_check_loop_tools(
        settings,
        registry,
        session_id="alice",
        agent_factory=lambda s: _StubAgent(["alice's check"]),
    )
    r = await tools_alice[0]("alice loop", interval_seconds=120.0)
    alice_loop_id = _extract_loop_id(r)

    # List as "bob".
    tools_bob = build_check_loop_tools(settings, registry, session_id="bob")
    _, _, list_check_loops = tools_bob

    result = await list_check_loops()
    # Bob sees his own loops (none) but not Alice's.
    assert alice_loop_id not in result
    assert "no check loops" in result.lower() or "none" in result.lower()

    # Clean up.
    registry.stop(alice_loop_id, reason="test teardown")
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# create_agent_from_settings — check_loop_registry gating
# ---------------------------------------------------------------------------


def test_no_change_result_empty() -> None:
    """``_no_change_result`` returns True for empty/whitespace strings."""
    from robotsix_chat.chat.delegation import _no_change_result

    assert _no_change_result("") is True
    assert _no_change_result("   ") is True
    assert _no_change_result("\n\t") is True


def test_no_change_result_sentinel() -> None:
    """``_no_change_result`` returns True for strings starting with NO_CHANGE."""
    from robotsix_chat.chat.delegation import _no_change_result

    assert _no_change_result("NO_CHANGE") is True
    assert _no_change_result("NO_CHANGE: all tickets unchanged") is True
    assert _no_change_result("no_change — nothing new") is True


def test_no_change_result_normal_result() -> None:
    """``_no_change_result`` returns False for normal results."""
    from robotsix_chat.chat.delegation import _no_change_result

    assert _no_change_result("ticket X is now blocked") is False
    assert _no_change_result("CHANGE: gate reached on ticket Y") is False


@pytest.mark.asyncio
async def test_start_check_loop_include_previous_result_forwarded() -> None:
    """The include_previous_result kwarg is forwarded to spawn_check_loop."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)

    tools = build_check_loop_tools(
        settings,
        registry,
        session_id="c1",
        agent_factory=lambda s: _StubAgent(["first"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop(
        "compare check",
        interval_seconds=0.01,
        max_iterations=2,
        include_previous_result=True,
    )
    loop_id = _extract_loop_id(result)

    # Let both iterations run.
    for _ in range(50):
        await asyncio.sleep(0.05)
        info = registry.get(loop_id)
        if info and info.status.value == "stopped":
            break

    info = registry.get(loop_id)
    assert info is not None
    assert info.iterations == 2
    assert info.status.value == "stopped"

    registry.stop(loop_id, reason="test teardown")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_start_check_loop_suppresses_no_change_ticks() -> None:
    """When the sub-agent returns NO_CHANGE, the tick is suppressed (no SSE)."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    q_sse = bus.subscribe("c1")

    tools = build_check_loop_tools(
        settings,
        registry,
        session_id="c1",
        agent_factory=lambda s: _StubAgent(["NO_CHANGE"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop(
        "board check",
        interval_seconds=0.01,
        max_iterations=1,
    )
    loop_id = _extract_loop_id(result)

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = registry.get(loop_id)
        if info and info.status.value == "stopped":
            break

    info = registry.get(loop_id)
    assert info is not None
    assert info.iterations == 1
    assert info.last_result == "NO_CHANGE"

    # No loop_tick SSE frame.
    frames: list[dict[str, Any]] = []
    while not q_sse.empty():
        frames.append(q_sse.get_nowait())
    tick_frames = [f for f in frames if f["type"] == SSE_LOOP_TICK_TYPE]
    assert len(tick_frames) == 0, (
        f"Expected zero tick frames for NO_CHANGE result, got {tick_frames}"
    )

    # loop_started and loop_stopped still emitted.
    started = [f for f in frames if f["type"] == SSE_LOOP_STARTED_TYPE]
    assert len(started) == 1
    stopped = [f for f in frames if f["type"] == SSE_LOOP_STOPPED_TYPE]
    assert len(stopped) == 1


@pytest.mark.asyncio
async def test_start_check_loop_does_not_suppress_change_ticks() -> None:
    """When the sub-agent returns a real change, the tick IS published."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings(min_check_loop_interval_seconds=0.001)
    q_sse = bus.subscribe("c1")

    tools = build_check_loop_tools(
        settings,
        registry,
        session_id="c1",
        agent_factory=lambda s: _StubAgent(["CHANGE: ticket X blocked"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop(
        "board check",
        interval_seconds=0.01,
        max_iterations=1,
    )
    loop_id = _extract_loop_id(result)

    for _ in range(50):
        await asyncio.sleep(0.05)
        info = registry.get(loop_id)
        if info and info.status.value == "stopped":
            break

    frames: list[dict[str, Any]] = []
    while not q_sse.empty():
        frames.append(q_sse.get_nowait())
    tick_frames = [f for f in frames if f["type"] == SSE_LOOP_TICK_TYPE]
    assert len(tick_frames) == 1
    assert tick_frames[0]["result"] == "CHANGE: ticket X blocked"


# ---------------------------------------------------------------------------
# create_agent_from_settings — check_loop_registry gating
# ---------------------------------------------------------------------------


def test_foreground_agent_gets_start_check_loop_tool() -> None:
    """A foreground agent built with check_loop_registry gets start_check_loop."""
    registry = _loop_registry()
    settings = Settings()

    agent = create_agent_from_settings(
        settings=settings,
        check_loop_registry=registry,
    )
    assert agent._request_tools_factory is not None, (
        "Foreground agent must have a request_tools_factory"
    )
    per_req = agent._request_tools_factory("test-client")
    tool_names = [t.__name__ for t in per_req]
    assert "start_check_loop" in tool_names, (
        f"Expected start_check_loop in per-request tools, got {tool_names}"
    )


def test_sub_agent_has_no_start_check_loop_tool() -> None:
    """A sub-agent built without check_loop_registry gets no start_check_loop tool."""
    agent = create_agent_from_settings(settings=Settings())
    tools = agent._tools
    request_tools_factory = agent._request_tools_factory

    # Static tools: no start_check_loop.
    if tools is not None:
        names = [getattr(t, "__name__", str(t)) for t in tools]
        assert "start_check_loop" not in names, (
            "Sub-agent must not receive start_check_loop statically"
        )

    # Per-request factory: not set for sub-agents (unless delegation tools
    # are also provided, but they aren't here).
    if request_tools_factory is not None:
        per_req = request_tools_factory("test-client")
        names = [t.__name__ for t in per_req]
        assert "start_check_loop" not in names, (
            "Sub-agent must not receive start_check_loop in per-request tools"
        )


def test_foreground_agent_gets_both_delegation_and_loop_tools() -> None:
    """When both registries are provided, all tools appear in the factory."""
    loop_registry = _loop_registry()
    task_registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    agent = create_agent_from_settings(
        settings=settings,
        task_registry=task_registry,
        delivery_channel=channel,
        check_loop_registry=loop_registry,
    )
    assert agent._request_tools_factory is not None
    per_req = agent._request_tools_factory("test-client")
    tool_names = [t.__name__ for t in per_req]
    assert "delegate_task" in tool_names
    assert "start_check_loop" in tool_names
    assert "stop_check_loop" in tool_names
    assert "list_check_loops" in tool_names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_task_id(result: str) -> str:
    """Pull a hex-like task id from a result string.

    Task ids are 32-char hex strings (uuid4.hex) embedded in the message.
    """
    for word in result.split():
        # Strip trailing punctuation.
        word = word.rstrip(".,!?;:'\"")
        if len(word) == 32 and all(c in "0123456789abcdef" for c in word):
            return word
    # Fallback: find the longest hex-like token.
    candidates = [
        w.rstrip(".,!?;:'\"") for w in result.split() if len(w.rstrip(".,!?;:'\"")) >= 8
    ]
    return candidates[-1] if candidates else ""


def _extract_loop_id(result: str) -> str:
    """Pull a loop id from a result string.

    Loop ids from ``_loop_registry()`` look like ``L0``, ``L1``, …
    """
    for word in result.split():
        word = word.rstrip(".,!?;:'\"")
        if len(word) >= 2 and word[0] == "L" and word[1:].isdigit():
            return word
    # Fallback: find any hex-like token (for non-stub registries).
    return _extract_task_id(result)


# ---------------------------------------------------------------------------
# start_check_loop — verify_via_board parameter wiring
# ---------------------------------------------------------------------------


def test_start_check_loop_docstring_mentions_verify_via_board() -> None:
    """The updated docstring documents the ``verify_via_board`` parameter."""
    registry = _loop_registry()
    settings = _stub_settings()

    tools = build_check_loop_tools(settings, registry)
    doc = tools[0].__doc__
    assert doc is not None
    assert "verify_via_board" in doc
    assert "consult_mill" in doc


@pytest.mark.asyncio
async def test_start_check_loop_forwards_verify_via_board_true() -> None:
    """When verify_via_board=True is passed, the loop is registered with it."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()
    settings.mill = type("MillStub", (), {"enabled": True})()

    tools = build_check_loop_tools(
        settings,
        registry,
        session_id="c-vvb",
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop(
        "gated status check",
        interval_seconds=120.0,
        verify_via_board=True,
    )
    loop_id = _extract_loop_id(result)

    info = registry.get(loop_id)
    assert info is not None
    assert info.verify_via_board is True

    registry.stop(loop_id, reason="test teardown")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_start_check_loop_verify_via_board_defaults_false() -> None:
    """When verify_via_board is omitted, the loop's flag is False."""
    bus = EventBus()
    registry = _loop_registry(sink=bus)
    settings = _stub_settings()

    tools = build_check_loop_tools(
        settings,
        registry,
        session_id="c-vvb2",
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    start_check_loop = tools[0]

    result = await start_check_loop(
        "non-gated check",
        interval_seconds=120.0,
    )
    loop_id = _extract_loop_id(result)

    info = registry.get(loop_id)
    assert info is not None
    assert info.verify_via_board is False

    registry.stop(loop_id, reason="test teardown")
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# create_agent_from_settings — tool_wrapper parameter
# ---------------------------------------------------------------------------


def test_create_agent_from_settings_applies_tool_wrapper() -> None:
    """When tool_wrapper is provided, it is applied to the tools list."""
    from robotsix_chat.chat.server import create_agent_from_settings

    wrapper_called: list[int] = [0]

    def _spy_wrapper(tools: list[Any]) -> list[Any]:
        wrapper_called[0] += 1
        return tools  # identity, just count calls

    _agent = create_agent_from_settings(
        settings=Settings(),
        tool_wrapper=_spy_wrapper,
    )
    assert wrapper_called[0] == 1
    # The tools list may be None (LlmioChatAgent normalises [] → None).
    # That's fine — what matters is that the wrapper was called.


def test_create_agent_from_settings_no_tool_wrapper_is_noop() -> None:
    """When tool_wrapper is omitted (default None), tools are unchanged."""
    from robotsix_chat.chat.server import create_agent_from_settings

    agent = create_agent_from_settings(settings=Settings())
    # Without tool_wrapper, the agent is constructed normally.
    # The tools may be None when all tool-gates are disabled.
    assert isinstance(agent._tools, (list, type(None)))


# ---------------------------------------------------------------------------
# Tick-triggered foreground agent run tests
# ---------------------------------------------------------------------------


class _SpyRunSerializer:
    """A :class:`RunSerializer` stub that records lock acquisitions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.acquired: list[str] = []

    def for_owner(self, owner_id: str) -> asyncio.Lock:
        self.acquired.append(owner_id)
        return self._lock


class _RecordingAgent:
    """An agent whose ``stream`` records the input and yields fixed chunks."""

    def __init__(self, chunks: list[str] | None = None) -> None:
        self.chunks = chunks or ["tick reply"]
        self.calls: list[dict[str, object]] = []

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "message": message,
                "history": history,
                "session_id": session_id,
                "client_id": client_id,
            }
        )
        for chunk in self.chunks:
            yield chunk


class TestTickTriggeredAgentRun:
    """Tests for the tick-triggered foreground agent run.

    Covers :class:`ConversationDeliveryChannel`.
    """

    @staticmethod
    def _store(**kwargs: Any) -> ConversationStore:
        return ConversationStore(
            idle_reset_seconds=3600.0,
            max_history_turns=10,
            max_conversations=5,
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_non_suppressed_tick_schedules_agent_run(self) -> None:
        """A loop_tick frame schedules an agent run and emits loop_reply when wired."""
        store = self._store()
        store.create_session("c-tick-run")
        bus = EventBus()
        q = bus.subscribe("c-tick-run")
        serializer = _SpyRunSerializer()
        agent = _RecordingAgent(["tick reply"])

        channel = ConversationDeliveryChannel(
            store,
            event_bus=bus,
            run_serializer=serializer,
            agent_factory=lambda s: agent,
            settings=Settings(),
        )

        await channel.publish(
            "c-tick-run",
            {
                "type": "loop_tick",
                "loop_id": "L42",
                "iteration": 3,
                "result": "price is $12.34",
                "next_run": 5000.0,
            },
        )

        # The background task should have run by now (it's fast).
        # Give the event loop a tick to flush the task.
        await asyncio.sleep(0)

        # Agent was invoked exactly once.
        assert len(agent.calls) == 1
        call = agent.calls[0]
        assert "price is $12.34" in str(call["message"])
        assert "L42" in str(call["message"])
        assert "tick 3" in str(call["message"])
        assert call["client_id"] == "c-tick-run"

        # A turn was recorded into the exact session that owns the loop.
        history = store.history("c-tick-run")
        assert len(history) == 1
        user_msg, assistant_msg = history[0]
        assert "price is $12.34" in user_msg
        assert assistant_msg == "tick reply"

        # A loop_reply frame was emitted.
        frame = q.get_nowait()
        assert frame["type"] == "loop_reply"
        assert frame["loop_id"] == "L42"
        assert frame["iteration"] == 3
        assert frame["reply"] == "tick reply"

        # The serializer was consulted for this owner.
        assert "c-tick-run" in serializer.acquired

    @pytest.mark.asyncio
    async def test_loop_tick_fallback_without_new_deps(self) -> None:
        """Without new deps, loop_tick falls back to legacy passive recording."""
        store = self._store()
        store.create_session("c-legacy")
        channel = ConversationDeliveryChannel(store)

        await channel.publish(
            "c-legacy",
            {
                "type": "loop_tick",
                "loop_id": "L1",
                "iteration": 1,
                "result": "ok",
            },
        )

        history = store.history("c-legacy")
        assert len(history) == 1
        user_msg, assistant_msg = history[0]
        assert "Check loop" in user_msg
        assert "ok" in assistant_msg

    @pytest.mark.asyncio
    async def test_serialization_prevents_overlap(self) -> None:
        """Two concurrent runs for the same owner do not overlap."""
        store = self._store()
        store.create_session("c-serial")
        bus = EventBus()

        # Use a real RunSerializer (process-local).
        from robotsix_chat.chat.server import RunSerializer

        serializer = RunSerializer()

        step = asyncio.Event()

        class _GatedAgent:
            """Agent that blocks until *step* is set, then yields."""

            def __init__(self) -> None:
                self.started = 0
                self.finished = 0

            async def stream(
                self,
                message: str,
                *,
                history: list[tuple[str, str]] | None = None,
                session_id: str | None = None,
                client_id: str | None = None,
                images: list[tuple[str, bytes]] | None = None,
            ) -> AsyncIterator[str]:
                self.started += 1
                await step.wait()
                self.finished += 1
                yield message

        gated = _GatedAgent()
        channel = ConversationDeliveryChannel(
            store,
            event_bus=bus,
            run_serializer=serializer,
            agent_factory=lambda s: gated,
            settings=Settings(),
        )

        # Publish two ticks for the SAME owner concurrently.
        await channel.publish(
            "c-serial",
            {"type": "loop_tick", "loop_id": "L1", "iteration": 1, "result": "a"},
        )
        await channel.publish(
            "c-serial",
            {"type": "loop_tick", "loop_id": "L2", "iteration": 1, "result": "b"},
        )

        # Give the event loop a chance to start the first task.
        await asyncio.sleep(0.05)
        # Only one agent should have started — the second is waiting on the lock.
        assert gated.started == 1
        assert gated.finished == 0

        # Release the first agent.
        step.set()
        await asyncio.sleep(0.05)
        # Both should now be finished.
        assert gated.started == 2
        assert gated.finished == 2

    @pytest.mark.asyncio
    async def test_suppressed_tick_no_run(self) -> None:
        """When the loop worker suppresses a tick, publish is never called."""
        store = self._store()
        store.create_session("c-suppressed")
        bus = EventBus()
        q = bus.subscribe("c-suppressed")

        agent = _RecordingAgent()
        channel = ConversationDeliveryChannel(
            store,
            event_bus=bus,
            run_serializer=_SpyRunSerializer(),
            agent_factory=lambda s: agent,
            settings=Settings(),
        )

        # Simulate: the loop worker suppressed the tick and did NOT call
        # channel.publish.  Verify nothing arrived.
        assert q.empty()
        assert len(agent.calls) == 0

        # Also verify that a loop_failed frame (different type) does NOT
        # trigger the agent run — only loop_tick does.
        await channel.publish(
            "c-suppressed",
            {"type": "loop_failed", "loop_id": "L1", "error": "boom"},
        )
        assert len(agent.calls) == 0  # agent not invoked for loop_failed

    def test_recursion_boundary_no_check_loop_tools(self) -> None:
        """Tick-run agent factory produces an agent without check-loop tools."""
        from robotsix_chat.chat.server import create_agent_from_settings

        # Build a no-loop-tools agent the same way run_server_from_config does.
        agent = create_agent_from_settings(
            settings=Settings(),
            task_registry=TaskRegistry(),
            delivery_channel=_FakeDeliveryChannel(),
            conversation_store=ConversationStore(),
            # NO check_loop_registry — this is the key difference
        )

        tools = getattr(agent, "_tools", None) or []
        tool_names = {getattr(t, "__name__", "") for t in tools}
        assert "start_check_loop" not in tool_names
        assert "stop_check_loop" not in tool_names
        assert "list_check_loops" not in tool_names

        # But delegation tools (delegate_task) MAY be present.
        # (They are optional depending on wiring; we just assert loop tools
        # are absent.)


# ---------------------------------------------------------------------------
# _is_board_action — unit tests
# ---------------------------------------------------------------------------


def test_is_board_action_positive_file_ticket() -> None:
    """_is_board_action returns True for 'File a ticket to fix the login bug'."""
    assert _is_board_action("File a ticket to fix the login bug") is True


def test_is_board_action_positive_triage_board() -> None:
    """_is_board_action returns True for 'triage the open board items'."""
    assert _is_board_action("triage the open board items") is True


def test_is_board_action_positive_consult_mill() -> None:
    """_is_board_action returns True for 'consult_mill about ticket status'."""
    assert _is_board_action("consult_mill about ticket status") is True


def test_is_board_action_positive_epics() -> None:
    """_is_board_action returns True for a description mentioning epics."""
    assert _is_board_action("list all epics on the board") is True


def test_is_board_action_positive_mill() -> None:
    """_is_board_action returns True for a description mentioning mill."""
    assert _is_board_action("check the mill for new tickets") is True


def test_is_board_action_positive_backlog() -> None:
    """_is_board_action returns True for a description mentioning backlog."""
    assert _is_board_action("review the backlog items") is True


def test_is_board_action_positive_create_ticket() -> None:
    """_is_board_action returns True for 'create a ticket for the bug'."""
    assert _is_board_action("create a ticket for the bug") is True


def test_is_board_action_positive_open_issue() -> None:
    """_is_board_action returns True for 'open an issue to track the work'."""
    assert _is_board_action("open an issue to track the work") is True


def test_is_board_action_positive_log_bug() -> None:
    """_is_board_action returns True for 'log a bug about the crash'."""
    assert _is_board_action("log a bug about the crash") is True


def test_is_board_action_positive_report_bug() -> None:
    """_is_board_action returns True for 'report a bug for the login failure'."""
    assert _is_board_action("report a bug for the login failure") is True


def test_is_board_action_positive_file_a_ticket() -> None:
    """_is_board_action returns True for 'file a ticket for the feature request'."""
    assert _is_board_action("file a ticket for the feature request") is True


# -- negative (must NOT match) -------------------------------------------


def test_is_board_action_negative_summarize_pdf() -> None:
    """_is_board_action returns False for 'Summarize this 50-page PDF'."""
    assert _is_board_action("Summarize this 50-page PDF") is False


def test_is_board_action_negative_quantum_research() -> None:
    """_is_board_action returns False for a multi-step web research description."""
    assert (
        _is_board_action("Run a multi-step web research on quantum computing") is False
    )


def test_is_board_action_negative_long_generation() -> None:
    """_is_board_action returns False for ordinary delegation descriptions."""
    assert _is_board_action("Generate a 500-word summary of the meeting notes") is False


def test_is_board_action_negative_code_review() -> None:
    """_is_board_action returns False for code review delegation."""
    assert (
        _is_board_action("Review the entire codebase for security vulnerabilities")
        is False
    )


def test_is_board_action_negative_data_analysis() -> None:
    """_is_board_action returns False for data analysis delegation."""
    assert _is_board_action("Analyze this 10MB CSV and produce a report") is False


def test_is_board_action_negative_false_positive_ticket_word_in_context() -> None:
    """_is_board_action is careful with 'ticket' in non-board contexts.

    The word 'ticket' alone triggers a match (by design; regex matching
    is intentionally broad).  However, phrases that coincidentally
    contain the word should still be checked for false positives.
    """
    # This one DOES trigger because 'ticket' is in the regex alternation.
    # That's acceptable — better to err on the side of blocking.
    assert _is_board_action("Help me buy a plane ticket") is True


# ---------------------------------------------------------------------------
# delegate_task — board-action guardrail (tool-level test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_task_board_action_returns_guardrail_message() -> None:
    """Calling delegate_task with a board-action description returns the guardrail."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    delegate_task = tools[0]

    result = await delegate_task("File a ticket to fix the login bug")

    # Returns the guardrail string (NOT a task id).
    assert isinstance(result, str)
    assert "can't delegate" in result
    assert "consult_mill" in result

    # No task_started frame was published.
    started_frames = [f for _, f in channel.frames if f["type"] == "task_started"]
    assert len(started_frames) == 0

    # No task was registered (registry count should be 0).
    assert registry.count_running() == 0


@pytest.mark.asyncio
async def test_delegate_task_board_action_does_not_spawn_subagent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A board-action description must never call spawn_subagent_task."""
    from robotsix_chat.chat import delegation as del_mod

    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    spawn_calls: list[dict[str, Any]] = []

    def _fake_spawn(**kwargs: Any) -> str:
        spawn_calls.append(kwargs)
        return "should-not-reach"

    monkeypatch.setattr(del_mod, "spawn_subagent_task", _fake_spawn)

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    delegate_task = tools[0]

    result = await delegate_task("File a ticket to fix the login bug")

    assert "consult_mill" in result
    assert len(spawn_calls) == 0, (
        "spawn_subagent_task must not be called for board-action descriptions"
    )


@pytest.mark.asyncio
async def test_delegate_task_board_action_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A board-action description emits a logger.warning."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    delegate_task = tools[0]

    with caplog.at_level("WARNING", logger="robotsix_chat.chat.delegation"):
        result = await delegate_task("triage the open board items")

    assert "consult_mill" in result
    assert "delegate_task blocked" in caplog.text


@pytest.mark.asyncio
async def test_delegate_task_non_board_still_spawns() -> None:
    """Calling delegate_task with a non-board description still spawns a sub-agent.

    This guards against false-positives in the board-action detector breaking
    normal delegation.
    """
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        agent_factory=lambda s: _StubAgent(["done"]),
    )
    delegate_task = tools[0]

    result = await delegate_task("Summarize this 50-page PDF")
    assert isinstance(result, str)
    # Should contain a task id (normal path).
    task_id = _extract_task_id(result)
    assert task_id

    # Task is registered.
    info = registry.get(task_id)
    assert info is not None
    assert info.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED)

    # A task_started frame was published.
    started_frames = [f for _, f in channel.frames if f["type"] == "task_started"]
    assert len(started_frames) == 1

"""Tests for :mod:`robotsix_chat.chat.delegation` — the delegate_task tool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from robotsix_chat.chat.delegation import (
    NullDeliveryChannel,
    build_delegation_tools,
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
async def test_task_registered_with_correct_client_id() -> None:
    """The registered task's client_id matches the one passed to the factory."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    tools = build_delegation_tools(
        settings,
        registry,
        channel,
        client_id="browser-42",
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    delegate_task = tools[0]

    result = await delegate_task("client-aware work")
    task_id = _extract_task_id(result)

    info = registry.get(task_id)
    assert info is not None
    assert info.client_id == "browser-42"
    assert info.prompt == "client-aware work"


@pytest.mark.asyncio
async def test_task_client_id_defaults_to_empty_string() -> None:
    """When ``client_id`` is not passed to the factory, the task uses ``""``."""
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
    assert info.client_id == ""


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
        client_id="c1",
        agent_factory=lambda s: _StubAgent(["ok"]),
    )
    delegate_task_fn = tools[0]

    result = await delegate_task_fn("too many tasks")

    # Returns the friendly message — no task id substring.
    assert isinstance(result, str)
    assert "couldn't start" in result
    # Verify no 32-char hex task id in the response.
    import re

    assert not re.search(r"\b[0-9a-f]{32}\b", result)

    # No task_started frame was published to the channel.
    started_frames = [f for _, f in channel.frames if f["type"] == "task_started"]
    assert len(started_frames) == 0

    # The registry count is still 1 (no new task was registered).
    assert registry.count_running() == 1


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

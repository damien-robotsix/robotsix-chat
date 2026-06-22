"""Tests for :mod:`robotsix_chat.chat.delegation` — the delegate_task tool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from robotsix_chat.chat.delegation import (
    NullDeliveryChannel,
    build_delegation_tools,
    current_client_id,
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
    """The registered task's client_id matches ``current_client_id``."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    current_client_id.set("browser-42")
    try:
        tools = build_delegation_tools(
            settings,
            registry,
            channel,
            agent_factory=lambda s: _StubAgent(["ok"]),
        )
        delegate_task = tools[0]

        result = await delegate_task("client-aware work")
        task_id = _extract_task_id(result)

        info = registry.get(task_id)
        assert info is not None
        assert info.client_id == "browser-42"
        assert info.prompt == "client-aware work"
    finally:
        current_client_id.set(None)


@pytest.mark.asyncio
async def test_task_client_id_falls_back_to_empty_string() -> None:
    """When ``current_client_id`` is unset, the task uses ``""``."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    # Ensure the context var is at its default.
    current_client_id.set(None)

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
    ``delegate_task`` tool.
    """
    agent = create_agent_from_settings(settings=Settings())
    tools = agent._tools

    # With no other tools enabled (mill/calendar/refdocs disabled by default),
    # there should be no tools at all.
    if tools is not None:
        names = [getattr(t, "__name__", str(t)) for t in tools]
        assert "delegate_task" not in names, (
            "Sub-agent must not receive the delegate_task tool"
        )


def test_foreground_agent_gets_delegate_tool() -> None:
    """A foreground agent built with registry and channel DOES get the tool."""
    registry = TaskRegistry()
    channel = _FakeDeliveryChannel()
    settings = Settings()

    agent = create_agent_from_settings(
        settings=settings,
        task_registry=registry,
        delivery_channel=channel,
    )
    tools = agent._tools
    assert tools is not None
    names = [getattr(t, "__name__", str(t)) for t in tools]
    assert "delegate_task" in names, (
        "Foreground agent must receive the delegate_task tool"
    )


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

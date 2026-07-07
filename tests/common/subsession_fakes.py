"""Shared fakes and builders for subsession tests.

Provides a scripted :class:`FakeAgent`, a recording :class:`RecordingSink`
event sink, a deterministic :class:`FakeClock`, lightweight settings
stand-ins (real ``Settings`` validators forbid tiny periodic intervals,
which tests need), and a :func:`build_env` helper that wires a full
``SubsessionEnv`` around an in-memory registry and conversation store.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.server.routes import RunSerializer
from robotsix_chat.subsessions import (
    CloseState,
    ParentDelivery,
    SubsessionContext,
    SubsessionEnv,
    SubsessionRegistry,
)


class FakeAgent:
    """A scripted :class:`ChatAgent` — yields queued replies and records calls."""

    def __init__(
        self,
        replies: list[str] | None = None,
        *,
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
        default_reply: str = "done",
    ) -> None:
        """Queue *replies*; optionally block on *gate* or raise *error*."""
        self.replies = list(replies or [])
        self.error = error
        self.gate = gate
        self.default_reply = default_reply
        self.calls: list[dict[str, Any]] = []

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
    ) -> AsyncIterator[str]:
        """Record the call, optionally wait on the gate, yield one reply."""
        self.calls.append(
            {
                "message": message,
                "history": history,
                "session_id": session_id,
                "client_id": client_id,
                "images": images,
            }
        )
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        yield self.replies.pop(0) if self.replies else self.default_reply


class CapturingAgentFactory:
    """Agent factory that records its arguments and returns scripted agents."""

    def __init__(self, *agents: Any) -> None:
        """Serve *agents* in order (the last one repeats for extra calls)."""
        self._agents = list(agents) or [FakeAgent()]
        self.captured: list[dict[str, Any]] = []

    def __call__(
        self,
        settings: Any,
        model_level: int,
        ctx: SubsessionContext,
        close_state: CloseState,
    ) -> Any:
        """Record the call and hand out the next scripted agent."""
        agent = self._agents.pop(0) if len(self._agents) > 1 else self._agents[0]
        self.captured.append(
            {
                "settings": settings,
                "model_level": model_level,
                "ctx": ctx,
                "close_state": close_state,
                "agent": agent,
            }
        )
        return agent


class RecordingSink:
    """Fake ``EventSink`` capturing ``(session_id, frame)`` tuples."""

    def __init__(self) -> None:
        """Start with no captured frames."""
        self.frames: list[tuple[str, dict[str, object]]] = []

    def publish(self, session_id: str, frame: dict[str, object]) -> None:
        """Record the published frame."""
        self.frames.append((session_id, frame))

    def of_type(self, frame_type: str) -> list[tuple[str, dict[str, object]]]:
        """Return the captured frames whose ``type`` equals *frame_type*."""
        return [(s, f) for s, f in self.frames if f.get("type") == frame_type]


class FakeClock:
    """A controllable wall clock for registry timestamps."""

    def __init__(self, start: float = 1_000.0) -> None:
        """Start the clock at *start* seconds."""
        self.now = start

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by *seconds*."""
        self.now += seconds


def make_settings(
    *,
    max_concurrent: int = 8,
    max_depth: int = 3,
    default_model_level: int = 2,
    min_interval_seconds: float = 0.01,
    auto_stop_no_change_runs: int = 3,
    llmio_api_key: str = "",
) -> SimpleNamespace:
    """Build a settings stand-in with test-friendly (tiny) intervals.

    Real ``Settings`` validators require ``min_interval_seconds >= 1.0``;
    the worker only reads the attributes mirrored here, so a
    ``SimpleNamespace`` keeps periodic tests fast.
    """
    from pydantic import SecretStr

    return SimpleNamespace(
        subsessions=SimpleNamespace(
            max_concurrent=max_concurrent,
            max_depth=max_depth,
            default_model_level=default_model_level,
            min_interval_seconds=min_interval_seconds,
            auto_stop_no_change_runs=auto_stop_no_change_runs,
        ),
        llmio_api_key=SecretStr(llmio_api_key),
    )


def build_env(
    *,
    agent_factory: Any | None = None,
    agent: Any | None = None,
    settings: Any | None = None,
    event_sink: RecordingSink | None = None,
    store: ConversationStore | None = None,
    registry: SubsessionRegistry | None = None,
) -> SubsessionEnv:
    """Wire a full ``SubsessionEnv`` around in-memory dependencies.

    Pass either a ready *agent_factory* or a single *agent* (wrapped in a
    :class:`CapturingAgentFactory`).  The registry defaults to a fresh
    ``store_path=None`` instance sharing *event_sink*.
    """
    if agent_factory is None:
        agent_factory = CapturingAgentFactory(agent or FakeAgent())
    settings = settings or make_settings()
    store = store or ConversationStore()
    registry = registry or SubsessionRegistry(event_sink=event_sink, store_path=None)
    delivery = ParentDelivery(
        conversation_store=store,
        registry=registry,
        run_serializer=RunSerializer(),
    )
    return SubsessionEnv(
        settings=settings,
        registry=registry,
        delivery=delivery,
        conversation_store=store,
        agent_factory=agent_factory,
        event_sink=event_sink,
    )


async def wait_until(
    predicate: Any, *, timeout: float = 2.0, interval: float = 0.005
) -> None:
    """Poll *predicate* until it returns truthy or *timeout* elapses."""

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout)

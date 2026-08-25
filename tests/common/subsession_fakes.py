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
        trace_metadata: dict[str, str] | None = None,
        trace_name: str | None = None,
        model_level: int | None = None,
    ) -> AsyncIterator[str]:
        """Record the call, optionally wait on the gate, yield one reply."""
        self.calls.append(
            {
                "message": message,
                "history": history,
                "session_id": session_id,
                "client_id": client_id,
                "images": images,
                "trace_metadata": trace_metadata,
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
    max_concurrent_per_session: int = 0,
    stale_reclaim_seconds: float = 0.0,
    max_depth: int = 3,
    default_model_level: int = 3,
    monitor_max_model_level: int = 2,
    min_interval_seconds: float = 0.01,
    auto_stop_no_change_runs: int = 3,
    max_idle_runs: int = 0,
    max_no_change_pauses: int = 3,
    human_approval_timeout_runs: int = 3,
    human_approval_timeout_seconds: float = 300.0,
    pre_authorized_ticket_patterns: list[str] | None = None,
    auto_drive_promote_ready_drafts: bool = False,
    run_timeout_seconds: float = 600.0,
    mill_recovery_initial_backoff_seconds: float = 0.01,
    mill_recovery_max_backoff_seconds: float = 3600.0,
    mill_recovery_max_retries: int = 10,
    periodic_max_interval_seconds: float = 3600.0,
    periodic_max_total_runs: int = 100,
    user_chat_max_retries: int = 3,
    monitor_error_max_retries: int = 2,
    consecutive_error_fail_threshold: int = 3,
    transient_error_max_retries: int = 3,
    transient_error_backoff_base: float = 0.01,
    transient_error_backoff_cap: float = 30.0,
    paused_monitor_auto_resume_seconds: float = 1800.0,
    max_runs_escalation_threshold: int = 3,
    max_runs_progress_extension: int = 20,
    max_runs_progress_window: int = 5,
    monitor_slot_budget: int = 0,
    monitor_slot_queue_max: int = 32,
    event_driven_timeout_seconds: float = 60.0,
    turn_budget: Any | None = None,
    llmio_api_key: str = "test-key",
) -> SimpleNamespace:
    """Build a settings stand-in with test-friendly (tiny) intervals.

    Real ``Settings`` validators require ``min_interval_seconds >= 1.0``;
    the worker only reads the attributes mirrored here, so a
    ``SimpleNamespace`` keeps periodic tests fast.
    """
    from pydantic import SecretStr

    if turn_budget is None:
        turn_budget = SimpleNamespace(
            task=SimpleNamespace(soft_warn_turns=25, hard_stop_turns=40),
            periodic=SimpleNamespace(soft_warn_turns=0, hard_stop_turns=0),
            user_chat=SimpleNamespace(soft_warn_turns=25, hard_stop_turns=40),
            on_close=SimpleNamespace(soft_warn_turns=25, hard_stop_turns=40),
        )

    return SimpleNamespace(
        subsessions=SimpleNamespace(
            max_concurrent=max_concurrent,
            max_concurrent_per_session=max_concurrent_per_session,
            stale_reclaim_seconds=stale_reclaim_seconds,
            max_depth=max_depth,
            default_model_level=default_model_level,
            monitor_max_model_level=monitor_max_model_level,
            min_interval_seconds=min_interval_seconds,
            auto_stop_no_change_runs=auto_stop_no_change_runs,
            max_idle_runs=max_idle_runs,
            max_no_change_pauses=max_no_change_pauses,
            human_approval_timeout_runs=human_approval_timeout_runs,
            human_approval_timeout_seconds=human_approval_timeout_seconds,
            pre_authorized_ticket_patterns=(
                pre_authorized_ticket_patterns
                if pre_authorized_ticket_patterns is not None
                else []
            ),
            auto_drive_promote_ready_drafts=auto_drive_promote_ready_drafts,
            run_timeout_seconds=run_timeout_seconds,
            mill_recovery_initial_backoff_seconds=mill_recovery_initial_backoff_seconds,
            mill_recovery_max_backoff_seconds=mill_recovery_max_backoff_seconds,
            mill_recovery_max_retries=mill_recovery_max_retries,
            user_chat_max_retries=user_chat_max_retries,
            monitor_error_max_retries=monitor_error_max_retries,
            consecutive_error_fail_threshold=consecutive_error_fail_threshold,
            transient_error_max_retries=transient_error_max_retries,
            transient_error_backoff_base=transient_error_backoff_base,
            transient_error_backoff_cap=transient_error_backoff_cap,
            periodic_max_interval_seconds=periodic_max_interval_seconds,
            periodic_max_total_runs=periodic_max_total_runs,
            event_driven_timeout_seconds=event_driven_timeout_seconds,
            paused_monitor_poll_interval_seconds=60.0,
            paused_monitor_long_poll_interval_seconds=15.0,
            paused_monitor_auto_resume_seconds=paused_monitor_auto_resume_seconds,
            max_runs_escalation_threshold=max_runs_escalation_threshold,
            max_runs_progress_extension=max_runs_progress_extension,
            max_runs_progress_window=max_runs_progress_window,
            monitor_slot_budget=monitor_slot_budget,
            monitor_slot_queue_max=monitor_slot_queue_max,
            turn_budget=turn_budget,
        ),
        central_deploy=SimpleNamespace(url="https://central-deploy.example.com"),
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
        batch_window_seconds=0,
    )
    env = SubsessionEnv(
        settings=settings,
        registry=registry,
        delivery=delivery,
        conversation_store=store,
        agent_factory=agent_factory,
        event_sink=event_sink,
    )
    # Wire the per-conversation slot-budget manager (no-op when disabled).
    from robotsix_chat.subsessions.worker import attach_slot_budget

    attach_slot_budget(env)
    return env


async def wait_until(
    predicate: Any, *, timeout: float = 2.0, interval: float = 0.005
) -> None:
    """Poll *predicate* until it returns truthy or *timeout* elapses."""

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout)

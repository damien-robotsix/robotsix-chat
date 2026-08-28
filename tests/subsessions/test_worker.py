"""Tests for the subsession worker: spawn validation and the turn loop."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE, SSE_SUBSESSION_RESULT_TYPE
from robotsix_chat.subsessions import (
    SubsessionCapacityError,
    SubsessionDepthError,
    SubsessionInfo,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionLevelError,
    SubsessionNoChangeThresholdError,
    SubsessionPeriodicSpawnError,
    SubsessionRegistry,
    SubsessionStatus,
    resume_subsessions,
    spawn_subsession,
)
from robotsix_chat.subsessions import worker as worker_mod
from robotsix_chat.subsessions.worker import (
    CloseState,
    SubsessionContext,
    SubsessionEnv,
    _draft_decision_comment_posted,
    _event_wait_loop,
    _format_worker_error,
    _is_duplicate_reply,
    _is_no_change,
    _is_queued,
    _run_wait_for_event_turn,
    _truncate,
)
from robotsix_chat.subsessions.worker_periodic import _build_periodic_input
from tests.common.subsession_fakes import (
    CapturingAgentFactory,
    FakeAgent,
    FakeClock,
    RecordingSink,
    build_env,
    make_settings,
    wait_until,
)


@pytest.fixture(autouse=True)
def _instant_transient_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse transient-retry backoff to zero for every test in this module.

    The retry loop is ``robotsix_http.acall_with_retry``, whose delay is
    ``min(backoff_base ** attempt, backoff_cap)``. Pinning the cap to 0
    makes every retry immediate, so the transient-error paths stay fast
    without a per-test knob — the two ``transient_error_backoff_*``
    settings these tests used to pass were removed along with the
    hand-rolled loop.
    """
    monkeypatch.setattr(worker_mod, "_TRANSIENT_BACKOFF_CAP", 0.0)


OWNER = "sess-main"


def _spawn(
    env: SubsessionEnv,
    *,
    kind: SubsessionKind = SubsessionKind.TASK,
    parent_id: str | None = None,
    depth: int = 1,
    title: str = "job",
    prompt: str = "do the thing",
    model_level: int = 3,
    **kwargs: object,
) -> str:
    """Spawn a subsession with sensible defaults for tests."""
    return spawn_subsession(
        env=env,
        kind=kind,
        owner_session_id=OWNER,
        parent_id=parent_id,
        depth=depth,
        title=title,
        prompt=prompt,
        model_level=model_level,
        **kwargs,  # type: ignore[arg-type]
    )


async def _await_worker(env: SubsessionEnv, sub_id: str, timeout: float = 2.0) -> None:
    """Wait for *sub_id*'s worker task to finish."""
    task = env.registry._running.get(sub_id)
    if task is not None:
        await asyncio.wait_for(task, timeout)


# ---------------------------------------------------------------------------
# task kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_single_turn_completes_and_delivers() -> None:
    """A one-shot task runs once, closes as completed, and reports back."""
    agent = FakeAgent(["result 42"])
    env = build_env(agent=agent)

    sub_id = _spawn(env, prompt="compute the answer")
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "completed"
    assert info.summary == "result 42"
    assert [(e.role, e.text) for e in info.transcript] == [("assistant", "result 42")]

    # Exactly one agent turn with the initial prompt as input.
    assert len(agent.calls) == 1
    assert agent.calls[0]["message"] == "compute the answer"
    assert agent.calls[0]["session_id"] == sub_id
    assert agent.calls[0]["client_id"] == sub_id

    # The summary landed in the owning main session's conversation store.
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    label, reply = history[0]
    assert label.startswith(f"[Subsession {sub_id[:8]} (task)")
    assert "completed" in label
    assert reply == "result 42"


@pytest.mark.asyncio
async def test_agent_factory_runs_off_the_event_loop_thread() -> None:
    """agent_factory must never be invoked on the event loop's own thread.

    Regression test for a production incident: create_agent_from_settings
    calls fetch_roster_sync, which does asyncio.run(...) internally — legal
    only when the calling thread has no running event loop. _subsession_worker
    itself runs as a task on the server's already-running loop, so calling
    agent_factory directly there reproduced exactly that crash ("asyncio.run()
    cannot be called from a running event loop") for every subsession spawn.
    The worker must dispatch the call to a separate thread.
    """
    event_loop_thread = threading.current_thread()

    def factory(
        settings: Any,
        model_level: int,
        ctx: SubsessionContext,
        close_state: CloseState,
    ) -> FakeAgent:
        assert threading.current_thread() is not event_loop_thread
        return FakeAgent(["ok"])

    env = build_env(agent_factory=factory)

    sub_id = _spawn(env, prompt="hello")
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.summary == "ok"


_AMBIENT = contextvars.ContextVar("test_worker_ambient", default="unset")


@pytest.mark.asyncio
async def test_worker_does_not_inherit_the_spawning_turn_context() -> None:
    """The worker task runs in a fresh context, not the spawning turn's.

    spawn_subsession is called from inside the parent agent's turn; if the
    worker inherited that context, the turn's active OTEL span (stored in a
    contextvar) would parent every subsession span and the subsession's runs
    would nest inside the owner session's Langfuse trace instead of forming
    their own trace under the subsession's session id.
    """

    class ContextProbeAgent(FakeAgent):
        def __init__(self) -> None:
            super().__init__(["ok"])
            self.seen: list[str] = []

        async def stream(self, message: str, **kwargs: Any) -> AsyncIterator[str]:
            self.seen.append(_AMBIENT.get())
            async for chunk in super().stream(message, **kwargs):
                yield chunk

    agent = ContextProbeAgent()
    env = build_env(agent=agent)

    token = _AMBIENT.set("parent-turn")
    try:
        sub_id = _spawn(env, prompt="probe context")
    finally:
        _AMBIENT.reset(token)
    await _await_worker(env, sub_id)

    assert agent.seen == ["unset"]


@pytest.mark.asyncio
async def test_task_steering_message_triggers_second_turn() -> None:
    """A message queued mid-turn produces a follow-up turn before closing."""
    gate = asyncio.Event()
    agent = FakeAgent(["first reply", "second reply"], gate=gate)
    env = build_env(agent=agent)

    sub_id = _spawn(env, prompt="start the job")
    await wait_until(lambda: len(agent.calls) == 1)

    # The first turn is still in flight — queue a steering message.
    assert env.registry.enqueue_message(sub_id, "parent", "also cover Y") is True
    gate.set()
    await _await_worker(env, sub_id)

    assert len(agent.calls) == 2
    assert agent.calls[1]["message"] == "also cover Y"
    assert agent.calls[1]["history"] == [("start the job", "first reply")]

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.summary == "second reply"
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    assert history[0][1] == "second reply"


@pytest.mark.asyncio
async def test_task_turn_budget_hard_stop_force_closes() -> None:
    """A task hitting hard_stop_turns is force-closed with a partial summary."""
    turn_budget = SimpleNamespace(
        task=SimpleNamespace(soft_warn_turns=0, hard_stop_turns=1),
        periodic=SimpleNamespace(soft_warn_turns=0, hard_stop_turns=0),
        user_chat=SimpleNamespace(soft_warn_turns=0, hard_stop_turns=0),
        on_close=SimpleNamespace(soft_warn_turns=0, hard_stop_turns=0),
    )
    agent = FakeAgent(["partial result"])
    env = build_env(
        agent=agent,
        settings=make_settings(turn_budget=turn_budget),
    )

    sub_id = _spawn(env, prompt="do the thing")
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "turn_budget_exceeded"
    assert "partial result" in (info.summary or "")


@pytest.mark.asyncio
async def test_task_turn_budget_hard_stop_respects_threshold() -> None:
    """hard_stop_turns=2 force-closes after exactly 2 turns (not 3)."""
    turn_budget = SimpleNamespace(
        task=SimpleNamespace(soft_warn_turns=0, hard_stop_turns=2),
        periodic=SimpleNamespace(soft_warn_turns=0, hard_stop_turns=0),
        user_chat=SimpleNamespace(soft_warn_turns=0, hard_stop_turns=0),
        on_close=SimpleNamespace(soft_warn_turns=0, hard_stop_turns=0),
    )
    gate = asyncio.Event()
    agent = FakeAgent(["first", "second", "third"], gate=gate)
    env = build_env(
        agent=agent,
        settings=make_settings(turn_budget=turn_budget),
    )

    sub_id = _spawn(env, prompt="start the job")
    await wait_until(lambda: len(agent.calls) == 1)
    assert env.registry.enqueue_message(sub_id, "parent", "keep going") is True
    gate.set()
    await _await_worker(env, sub_id)

    # Two agent turns ran; the third was never reached because the
    # hard-stop fired at the threshold.
    assert len(agent.calls) == 2
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "turn_budget_exceeded"


# ---------------------------------------------------------------------------
# user_chat kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_chat_waits_between_turns_and_closes_via_close_state() -> None:
    """A user_chat waits for messages, grows history, and self-closes."""
    agent = FakeAgent(["hi there", "sure thing", "goodbye"])
    factory = CapturingAgentFactory(agent)
    env = build_env(agent_factory=factory)

    sub_id = _spawn(env, kind=SubsessionKind.USER_CHAT, prompt="ask about deploys")
    await wait_until(
        lambda: env.registry.get(sub_id).status is SubsessionStatus.WAITING  # type: ignore[union-attr]
    )
    assert len(agent.calls) == 1

    # A user message wakes the worker for a second turn with grown history.
    env.registry.enqueue_message(sub_id, "user", "tell me more")
    await wait_until(lambda: len(agent.calls) == 2)
    assert agent.calls[1]["message"] == "tell me more"
    # The first turn input includes the user_chat system note prepended
    # by the worker, so the history entry is (note + prompt, reply).
    from robotsix_chat.subsessions.worker import _USER_CHAT_FIRST_TURN_NOTE

    assert agent.calls[1]["history"] == [
        (_USER_CHAT_FIRST_TURN_NOTE + "\n\n" + "ask about deploys", "hi there")
    ]
    await wait_until(
        lambda: env.registry.get(sub_id).status is SubsessionStatus.WAITING  # type: ignore[union-attr]
    )

    # complete_subsession flips the worker-shared CloseState.
    close_state = factory.captured[0]["close_state"]
    close_state.requested = True
    close_state.summary = "user satisfied"
    env.registry.enqueue_message(sub_id, "user", "thanks, bye")
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "completed"
    assert info.summary == "user satisfied"

    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    label, reply = history[0]
    assert "(user_chat)" in label
    assert "completed" in label
    # The outcome now includes the transcript for user_chat kinds (the
    # summary + a formatted transcript section).
    assert reply.startswith("user satisfied")
    assert "Conversation transcript:" in reply
    assert "[assistant] hi there" in reply


@pytest.mark.asyncio
async def test_user_chat_retry_redelivers_drained_operator_answer() -> None:
    """A failed turn re-delivers its drained operator answer on retry.

    Regression test for the answer-loss bug: a user_chat turn drains the
    operator's inbox message and then raises. The retry path must feed that
    drained message back into the retry turn instead of discarding it (which
    made the subsession re-ask its original question).
    """

    class FailSecondTurnAgent(FakeAgent):
        """Succeed on the first turn, raise on the second."""

        def __init__(self) -> None:
            super().__init__(["what is the answer?"])
            self._turn = 0

        async def stream(self, message: str, **kwargs: Any) -> AsyncIterator[str]:
            self._turn += 1
            if self._turn == 2:
                raise RuntimeError("boom")
            async for chunk in super().stream(message, **kwargs):
                yield chunk

    first_agent = FailSecondTurnAgent()
    retry_agent = FakeAgent(["got your answer"])
    factory = CapturingAgentFactory(first_agent, retry_agent)
    env = build_env(agent_factory=factory)

    sub_id = _spawn(env, kind=SubsessionKind.USER_CHAT, prompt="ask the question")
    await wait_until(
        lambda: env.registry.get(sub_id).status is SubsessionStatus.WAITING  # type: ignore[union-attr]
    )
    assert len(first_agent.calls) == 1

    # The operator answers; the worker drains it for the second turn, which
    # then raises and re-enters the worker via the retry path.
    env.registry.enqueue_message(sub_id, "user", "the operator answer")

    # The retry worker is re-created with a fresh agent; wait for its turn.
    await wait_until(lambda: len(factory.captured) >= 2)
    await wait_until(lambda: len(retry_agent.calls) >= 1)

    assert retry_agent.calls[0]["message"] == "the operator answer"
    assert "ask the question" not in retry_agent.calls[0]["message"]

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.retry_count == 1

    # Close the retry worker so the task reaches a terminal state.
    close_state = factory.captured[1]["close_state"]
    close_state.requested = True
    close_state.summary = "closed after answer"
    env.registry.enqueue_message(sub_id, "user", "thanks, done")
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "completed"


# ---------------------------------------------------------------------------
# periodic kind
# ---------------------------------------------------------------------------


def test_periodic_interval_below_minimum_is_rejected() -> None:
    """A periodic interval below the configured minimum raises."""
    env = build_env(settings=make_settings(min_interval_seconds=1.0))

    with pytest.raises(SubsessionIntervalError):
        _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.5)
    with pytest.raises(SubsessionIntervalError):
        _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=None)

    assert env.registry.list_for_owner(OWNER) == []


def test_periodic_no_change_threshold_below_one_is_rejected() -> None:
    """A per-spawn auto_stop_no_change_runs below 1 raises."""
    env = build_env()

    with pytest.raises(SubsessionNoChangeThresholdError):
        _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.5,
            auto_stop_no_change_runs=0,
        )

    assert env.registry.list_for_owner(OWNER) == []


def test_periodic_no_change_threshold_non_int_is_rejected() -> None:
    """A non-int auto_stop_no_change_runs is rejected (bool is an int in Python)."""
    env = build_env()

    for bad_value in (2.5, "50", True):
        with pytest.raises(SubsessionNoChangeThresholdError):
            _spawn(
                env,
                kind=SubsessionKind.PERIODIC,
                interval_seconds=60.0,
                auto_stop_no_change_runs=bad_value,
            )

    assert env.registry.list_for_owner(OWNER) == []


def test_build_periodic_input_includes_loop_guard_instructions() -> None:
    """The periodic turn input includes CI workflow loop guard instructions."""
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-x",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch ticket 9d6a",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "abc123"},
    )

    result = _build_periodic_input(info, previous_result=None, steering=[])

    # The output must include the loop guard instructions.
    assert "LOOP GUARD" in result
    assert "CI workflow verification" in result
    assert "GitHub Actions API" in result
    assert "publish/deploy workflow" in result
    # Programmatic gate language.
    assert "PROGRAMMATIC GATE" in result
    assert "will REJECT any summary" in result
    # Deploy-complete config/status check for opt-in feature flags.
    assert "config/status check" in result
    assert "continuation.enabled" in result


def test_build_periodic_input_includes_redundant_fix_ticket_detection() -> None:
    """The periodic turn input includes redundant fix ticket detection."""
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-y",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch fix ticket 3120",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "3120"},
    )

    result = _build_periodic_input(info, previous_result=None, steering=[])

    # The output must include the redundant fix ticket detection instructions.
    assert "REDUNDANT FIX TICKET DETECTION" in result
    assert "alternative path" in result
    assert "baseline ticket" in result
    assert "complete_subsession" in result
    assert "redundant" in result


def test_build_periodic_input_pre_authorized_via_dedup_key() -> None:
    """PRE-AUTHORIZED instruction is injected when dedup_key matches a pattern.

    Even when ticket_id is not yet in the checkpoint (first run).
    """
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-x",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch ticket TICKET-1",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        dedup_key="TICKET-1",
        # No checkpoint yet — first run.
    )

    result = _build_periodic_input(
        info,
        previous_result=None,
        steering=[],
        pre_authorized_patterns=["TICKET-*"],
    )

    assert "PRE-AUTHORIZED TICKET" in result
    assert "human_issue_approval gate does NOT apply" in result


def test_build_periodic_input_pre_authorized_via_checkpoint_ticket_id() -> None:
    """PRE-AUTHORIZED instruction is injected when checkpoint ticket_id matches."""
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-x",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch ticket TICKET-2",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "TICKET-2"},
    )

    result = _build_periodic_input(
        info,
        previous_result=None,
        steering=[],
        pre_authorized_patterns=["TICKET-*"],
    )

    assert "PRE-AUTHORIZED TICKET" in result
    assert "human_issue_approval gate does NOT apply" in result


def test_build_periodic_input_pre_authorized_no_match() -> None:
    """PRE-AUTHORIZED instruction is omitted when no patterns match the ticket."""
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-x",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch ticket OTHER-1",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        dedup_key="OTHER-1",
        checkpoint={"ticket_id": "OTHER-1"},
    )

    result = _build_periodic_input(
        info,
        previous_result=None,
        steering=[],
        pre_authorized_patterns=["TICKET-*"],
    )

    assert "PRE-AUTHORIZED TICKET" not in result


@pytest.mark.asyncio
async def test_periodic_run_delivers_result_frame_only() -> None:
    """Each non-suppressed run publishes a result frame to the event sink.

    Intermediate runs are NOT delivered to the parent conversation store —
    only the terminal summary (via complete_subsession or auto-close) arrives.
    """
    sink = RecordingSink()
    agent = FakeAgent(["report 1", "report 2"])
    settings = make_settings(max_runs_progress_extension=0)
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=2,
        title="watch",
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "max_runs"
    assert info.runs == 2
    assert info.last_result == "report 2"
    assert info.summary == "Reached the 2-run limit. Last: report 2"

    result_frames = sink.of_type(SSE_SUBSESSION_RESULT_TYPE)
    assert [(s, f["run"], f["text"]) for s, f in result_frames] == [
        (OWNER, 1, "report 1"),
        (OWNER, 2, "report 2"),
    ]

    history = env.conversation_store.history(OWNER)
    # Only the terminal summary is delivered — intermediate runs stay silent.
    assert len(history) == 1
    assert "max_runs" in history[0][0]


@pytest.mark.asyncio
async def test_periodic_max_runs_escalation_threshold_reached() -> None:
    """Monitor closes with 'max_runs_escalated' when threshold is reached."""
    sink = RecordingSink()
    agent = FakeAgent(["report 1"])
    settings = make_settings(
        max_runs_escalation_threshold=2, max_runs_progress_extension=0
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="watch",
        checkpoint={
            "ticket_id": "ticket-1",
            "max_runs_exhausted_count": 1,
        },
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "max_runs_escalated"
    assert info.runs == 1
    assert "consecutive time" in (info.summary or "")


@pytest.mark.asyncio
async def test_periodic_max_runs_below_escalation_threshold() -> None:
    """Below threshold, closes with 'max_runs' and persists incremented count."""
    sink = RecordingSink()
    agent = FakeAgent(["report 1"])
    settings = make_settings(
        max_runs_escalation_threshold=3, max_runs_progress_extension=0
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="watch",
        checkpoint={
            "ticket_id": "ticket-1",
            "max_runs_exhausted_count": 1,
        },
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "max_runs"
    assert info.runs == 1
    # The checkpoint should have been updated with the incremented count.
    assert info.checkpoint is not None
    assert info.checkpoint.get("max_runs_exhausted_count") == 2


@pytest.mark.asyncio
async def test_periodic_max_runs_escalation_disabled_when_threshold_zero() -> None:
    """A threshold of 0 disables escalation entirely."""
    sink = RecordingSink()
    agent = FakeAgent(["report 1"])
    settings = make_settings(
        max_runs_escalation_threshold=0, max_runs_progress_extension=0
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="watch",
        checkpoint={
            "ticket_id": "ticket-1",
            "max_runs_exhausted_count": 5,
        },
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "max_runs"


@pytest.mark.asyncio
async def test_periodic_max_runs_escalation_no_checkpoint() -> None:
    """No checkpoint: count starts at 1, below threshold."""
    sink = RecordingSink()
    agent = FakeAgent(["report 1"])
    settings = make_settings(
        max_runs_escalation_threshold=2, max_runs_progress_extension=0
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="watch",
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    # No checkpoint → count starts at 0 → incremented to 1 < threshold.
    assert info.close_reason == "max_runs"
    assert info.checkpoint is not None
    assert info.checkpoint.get("max_runs_exhausted_count") == 1


@pytest.mark.asyncio
async def test_periodic_max_runs_extends_on_progress_then_auto_stops() -> None:
    """A monitor making progress extends its budget instead of closing at cap.

    When a non-suppressed reply (progress) is observed within the window,
    the max_runs cap is raised; the monitor then continues and only stops
    once it accumulates enough consecutive NO_CHANGE runs.
    """
    sink = RecordingSink()
    agent = FakeAgent(["report 1", "report 2", "NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    settings = make_settings(
        max_runs_progress_extension=20,
        max_runs_progress_window=5,
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=2,
        title="watch",
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_auto_stop"
    assert info.runs == 5
    assert info.max_runs == 22


@pytest.mark.asyncio
async def test_periodic_max_runs_no_progress_does_not_extend() -> None:
    """Without recent progress the monitor still closes at the hard cap."""
    sink = RecordingSink()
    agent = FakeAgent(["NO_CHANGE"])
    settings = make_settings(
        max_runs_progress_extension=20,
        max_runs_progress_window=5,
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="watch",
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "max_runs"
    assert info.max_runs == 1


@pytest.mark.asyncio
async def test_periodic_no_change_reply_is_suppressed() -> None:
    """A NO_CHANGE run produces no delivery and no result frame."""
    sink = RecordingSink()
    agent = FakeAgent(["NO_CHANGE"])
    env = build_env(agent=agent, event_sink=sink)

    sub_id = _spawn(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02, max_runs=1
    )
    await _await_worker(env, sub_id)

    assert sink.of_type(SSE_SUBSESSION_RESULT_TYPE) == []
    history = env.conversation_store.history(OWNER)
    # Only the terminal summary is delivered — no per-run turn.
    assert len(history) == 1
    assert "run 1" not in history[0][0]
    assert "max_runs" in history[0][0]


@pytest.mark.asyncio
async def test_periodic_auto_stops_after_consecutive_no_change_runs() -> None:
    """N consecutive NO_CHANGE runs close the subsession automatically."""
    agent = FakeAgent(["NO_CHANGE", "no_change again"])
    env = build_env(agent=agent, settings=make_settings(auto_stop_no_change_runs=2))

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_auto_stop"
    assert "Auto-stopped after 2 consecutive no-change runs" in (info.summary or "")
    assert len(agent.calls) == 2


@pytest.mark.asyncio
async def test_periodic_auto_stop_uses_per_spawn_no_change_threshold() -> None:
    """A per-spawn auto_stop_no_change_runs override wins over the global cap."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(agent=agent, settings=make_settings(auto_stop_no_change_runs=2))

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        auto_stop_no_change_runs=3,
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_auto_stop"
    assert "Auto-stopped after 3 consecutive no-change runs" in (info.summary or "")
    # The global threshold (2) must not have fired first.
    assert len(agent.calls) == 3


@pytest.mark.asyncio
async def test_periodic_ignores_bool_no_change_threshold_in_checkpoint() -> None:
    """A bool ``auto_stop_no_change_runs`` in the checkpoint is not a valid override.

    Spawn validation rejects bools, but an agent ``set_checkpoint`` call
    could still write one — the periodic turn loop must ignore it and fall
    back to the global threshold rather than treating ``True`` as ``1``.
    """
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE"])
    env = build_env(agent=agent, settings=make_settings(auto_stop_no_change_runs=2))

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    # Seed a bool override directly into the checkpoint before the worker's
    # first turn runs (spawn_subsession is synchronous, so the task has not
    # been scheduled yet).
    assert env.registry.update_checkpoint(sub_id, {"auto_stop_no_change_runs": True})
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_auto_stop"
    # The global threshold (2) applied — True was ignored, not treated as 1.
    assert "Auto-stopped after 2 consecutive no-change runs" in (info.summary or "")
    assert len(agent.calls) == 2


@pytest.mark.asyncio
async def test_periodic_max_idle_runs_pauses_after_consecutive_no_change() -> None:
    """max_idle_runs pauses the subsession and emits a notification."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    sink = RecordingSink()
    env = build_env(
        agent=agent,
        settings=make_settings(max_idle_runs=3, auto_stop_no_change_runs=10),
        event_sink=sink,
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    # The worker enters a PAUSED wait loop and never finishes — wait
    # briefly for it to reach the paused state, then cancel it.
    await asyncio.sleep(0.15)
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    # Let cancellation propagate.
    await asyncio.sleep(0.05)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.PAUSED
    assert info.close_reason == "paused"
    assert info.summary == (
        "Auto-paused after 3 consecutive no-change runs "
        "(no-change pause 1/3; the monitor will auto-close after 3 "
        "such pauses if the ticket never changes). The monitor will "
        "resume when the ticket's state changes, or you can resume it "
        "now by sending a message to this subsession via "
        "message_subsession."
    )
    assert len(agent.calls) == 3

    # Assert an SSE notification was published for the auto-pause.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    assert len(notifications) == 1
    _sid, frame = notifications[0]
    assert frame["title"] == f"Monitor auto-paused: {info.title}"
    assert f" — {info.summary}" in str(frame["body"])
    assert frame["urgency"] == "low"


@pytest.mark.asyncio
async def test_periodic_auto_closes_after_repeated_no_change_pauses() -> None:
    """Repeated no-change pauses auto-close the monitor instead of pausing again."""
    # 6 NO_CHANGE replies: 3 to auto-pause the first time, then 3 more
    # after the auto-resume timeout to reach the second pause — which
    # hits the pause limit and closes the monitor.
    agent = FakeAgent(["NO_CHANGE"] * 6)
    sink = RecordingSink()
    env = build_env(
        agent=agent,
        settings=make_settings(
            max_idle_runs=3,
            auto_stop_no_change_runs=10,
            max_no_change_pauses=2,
            paused_monitor_auto_resume_seconds=0.05,
        ),
        event_sink=sink,
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_pause_limit"
    assert "2 consecutive pauses" in (info.summary or "")
    assert "reassess" in (info.summary or "").lower()
    assert len(agent.calls) == 6

    # Auto-pause, auto-resume (timeout), then auto-close notifications.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    assert len(notifications) == 3
    _sid, frame = notifications[-1]
    assert frame["title"] == f"Monitor auto-closed: {info.title}"
    assert frame["urgency"] == "low"


@pytest.mark.asyncio
async def test_paused_periodic_resumes_on_parent_message() -> None:
    """A parent message via enqueue_message wakes a paused periodic worker."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(max_idle_runs=3, auto_stop_no_change_runs=10),
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)

    # Wait for auto-pause — the worker consumes the 3 NO_CHANGE replies
    # and enters the paused wait loop.
    await wait_until(lambda: len(agent.calls) >= 3)
    await asyncio.sleep(0.15)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.PAUSED

    # Send a parent message — the paused worker should wake and resume.
    env.registry.enqueue_message(sub_id, "parent", "please resume monitoring")

    # The worker resumes and runs at least one more turn.
    await wait_until(lambda: len(agent.calls) >= 4)

    # Clean up — cancel the worker so it doesn't loop forever.
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    await asyncio.sleep(0.05)

    # Verify the subsession was resumed (status no longer PAUSED).
    info = env.registry.get(sub_id)
    assert info is not None
    assert len(agent.calls) >= 4


@pytest.mark.asyncio
async def test_paused_periodic_resume_resets_no_change_counter() -> None:
    """Resume resets consecutive_no_change; monitor not immediately re-paused."""
    # 5 NO_CHANGE replies: 3 to trigger auto-pause, then 2 more after
    # resume to prove the counter was reset (otherwise the 4th would
    # re-pause and the 5th would never happen).
    agent = FakeAgent(["NO_CHANGE"] * 5)
    env = build_env(
        agent=agent,
        settings=make_settings(max_idle_runs=3, auto_stop_no_change_runs=10),
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)

    # Wait for the worker to auto-pause after 3 consecutive NO_CHANGE runs.
    await wait_until(lambda: len(agent.calls) >= 3)
    await asyncio.sleep(0.15)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.PAUSED

    # Send a parent message to resume.
    env.registry.enqueue_message(sub_id, "parent", "resume please")

    # The worker resumes.  The 4th call is the resumed turn; the 5th
    # proves the counter was reset — without the fix the 4th NO_CHANGE
    # would re-pause the monitor immediately and the 5th would never run.
    await wait_until(lambda: len(agent.calls) >= 5, timeout=3.0)

    # Clean up.
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    await asyncio.sleep(0.05)

    info = env.registry.get(sub_id)
    assert info is not None
    assert len(agent.calls) >= 5


@pytest.mark.asyncio
async def test_paused_periodic_auto_resumes_on_timeout() -> None:
    """A paused periodic monitor auto-resumes after the configured timeout."""
    agent = FakeAgent(
        ["NO_CHANGE", "NO_CHANGE", "NO_CHANGE", "CHANGE_DETECTED", "NO_CHANGE"]
    )
    env = build_env(
        agent=agent,
        settings=make_settings(
            max_idle_runs=3,
            auto_stop_no_change_runs=10,
            paused_monitor_auto_resume_seconds=0.05,
        ),
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)

    # The worker will: run 3 NO_CHANGE turns → auto-pause → auto-resume
    # after 0.05s → run at least one more turn (the 4th call).
    # We verify that the monitor resumed and ran >= 4 turns.
    await wait_until(lambda: len(agent.calls) >= 4)
    await asyncio.sleep(0.1)

    info = env.registry.get(sub_id)
    assert info is not None
    # After auto-resume, the monitor is back in its normal periodic cycle
    # (SLEEPING between runs, or RUNNING during a turn).
    assert info.status in (SubsessionStatus.SLEEPING, SubsessionStatus.RUNNING)
    assert len(agent.calls) >= 4

    # Clean up — cancel the worker so it doesn't loop forever.
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_periodic_max_idle_runs_zero_disables_pause() -> None:
    """max_idle_runs=0 disables pausing; falls through to auto_stop."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(max_idle_runs=0, auto_stop_no_change_runs=2),
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    # Falls through to auto_stop, not paused.
    assert info.close_reason == "no_change_auto_stop"
    assert len(agent.calls) == 2


@pytest.mark.asyncio
async def test_periodic_turn_budget_resets_each_run() -> None:
    """The turn budget is per-run-burst, not a lifetime ceiling, for monitors.

    With hard_stop_turns=2 a lifetime counter would force-close the monitor
    after two cumulative turns.  A per-run reset lets a long-lived monitor
    keep cycling through many single-turn runs without ``turn_budget_exceeded``.
    """
    turn_budget = SimpleNamespace(
        task=SimpleNamespace(soft_warn_turns=25, hard_stop_turns=40),
        periodic=SimpleNamespace(soft_warn_turns=0, hard_stop_turns=2),
        user_chat=SimpleNamespace(soft_warn_turns=25, hard_stop_turns=40),
        on_close=SimpleNamespace(soft_warn_turns=25, hard_stop_turns=40),
    )
    agent = FakeAgent(["NO_CHANGE"] * 10)
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=100,
            max_idle_runs=0,
            turn_budget=turn_budget,
        ),
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)

    # The monitor should run many single-turn cycles without being
    # force-closed for exceeding a lifetime turn ceiling.
    await wait_until(lambda: len(agent.calls) >= 4)
    await asyncio.sleep(0.1)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status in (SubsessionStatus.SLEEPING, SubsessionStatus.RUNNING)
    assert info.close_reason != "turn_budget_exceeded"
    assert len(agent.calls) >= 4

    # Clean up — cancel the worker so it doesn't loop forever.
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_periodic_human_approval_switches_to_event_driven() -> None:
    """human_issue_approval checkpoint triggers event-driven wait instead of close.

    The monitor must stay alive and wait for the ticket to leave
    human_issue_approval, not auto-close with human_approval_timeout.
    """
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=5,
            human_approval_timeout_runs=3,
            paused_monitor_auto_resume_seconds=0.05,
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={
            "last_known_state": "human_issue_approval",
        },
    )
    # Wait until the agent has been called 3 times (human_approval_timeout_runs)
    # — the monitor will then switch to event-driven waiting.
    await wait_until(lambda: len(agent.calls) >= 3)

    info = env.registry.get(sub_id)
    assert info is not None
    # The monitor should still be alive — it switched to event-driven
    # waiting after the human_approval_timeout_runs threshold was reached.
    assert info.status is not SubsessionStatus.CLOSED
    # The agent was called exactly human_approval_timeout_runs times
    # before switching to event-driven wait.
    assert len(agent.calls) == 3

    # Clean up the worker task.
    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_periodic_human_approval_timeout_ignored_without_checkpoint() -> None:
    """Without human_issue_approval checkpoint, generic auto_stop applies."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=3,
            human_approval_timeout_runs=2,
        ),
    )

    # Checkpoint has no last_known_state — human-approval timeout should
    # not trigger.
    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={"other_field": "value"},
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    # Falls through to the generic auto-stop, not human_approval_timeout.
    assert info.close_reason == "no_change_auto_stop"
    assert len(agent.calls) == 3


@pytest.mark.asyncio
async def test_periodic_human_approval_timeout_uses_own_threshold() -> None:
    """human_approval_timeout_runs switches to event-driven wait independently."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=10,
            human_approval_timeout_runs=2,
            paused_monitor_auto_resume_seconds=0.05,
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={
            "last_known_state": "human_issue_approval",
        },
    )
    # Wait for the 2 human_approval_timeout_runs calls.
    await wait_until(lambda: len(agent.calls) >= 2)

    info = env.registry.get(sub_id)
    assert info is not None
    # Monitor should still be alive — switched to event-driven wait.
    assert info.status is not SubsessionStatus.CLOSED

    # Clean up the worker task.
    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_periodic_pre_authorized_escalates_immediately() -> None:
    """Pre-authorized ticket in human_issue_approval escalates on first NO_CHANGE."""
    agent = FakeAgent(["NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=10,
            human_approval_timeout_runs=5,
            pre_authorized_ticket_patterns=["TICKET-*"],
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={
            "last_known_state": "human_issue_approval",
            "ticket_id": "TICKET-1",
        },
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "pre_authorized_approval"
    assert "pre-authorized" in (info.summary or "").lower()
    assert len(agent.calls) == 1  # Escalated on first run, not 5.


@pytest.mark.asyncio
async def test_periodic_pre_authorized_no_match_switches_to_event_driven() -> None:
    """Non-matching ticket switches to event-driven wait instead of closing."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=10,
            human_approval_timeout_runs=3,
            pre_authorized_ticket_patterns=["OTHER-*"],
            paused_monitor_auto_resume_seconds=0.05,
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={
            "last_known_state": "human_issue_approval",
            "ticket_id": "TICKET-1",
        },
    )
    await wait_until(lambda: len(agent.calls) >= 3)

    info = env.registry.get(sub_id)
    assert info is not None
    # Monitor stays alive — switched to event-driven wait.
    assert info.status is not SubsessionStatus.CLOSED
    assert len(agent.calls) == 3

    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_periodic_pre_authorized_empty_patterns_waits() -> None:
    """Empty patterns list falls through to event-driven wait."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=10,
            human_approval_timeout_runs=2,
            pre_authorized_ticket_patterns=[],
            paused_monitor_auto_resume_seconds=0.05,
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={
            "last_known_state": "human_issue_approval",
            "ticket_id": "TICKET-1",
        },
    )
    await wait_until(lambda: len(agent.calls) >= 2)

    info = env.registry.get(sub_id)
    assert info is not None
    # Monitor stays alive — switched to event-driven wait.
    assert info.status is not SubsessionStatus.CLOSED

    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_periodic_human_approval_wall_clock_switches_to_event_driven() -> None:
    """Wall-clock timeout switches to event-driven wait even without NO_CHANGE.

    The wall-clock backstop catches the case where the agent follows the
    prompt (producing non-NO_CHANGE output each run) but the ticket is
    still stuck at human_issue_approval.  The monitor stays alive.
    """
    clock = FakeClock(start=1000.0)
    agent = FakeAgent(["still waiting for approval", "still waiting for approval"] * 5)
    registry = SubsessionRegistry(
        event_sink=RecordingSink(),
        store_path=None,
        clock=clock,
    )
    env = build_env(
        agent=agent,
        registry=registry,
        settings=make_settings(
            auto_stop_no_change_runs=999,
            human_approval_timeout_runs=999,
            human_approval_timeout_seconds=300.0,
            paused_monitor_auto_resume_seconds=0.05,
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={
            "last_known_state": "human_issue_approval",
            "ticket_id": "TICKET-1",
        },
    )

    # Let the first run complete — it sets human_approval_since in the
    # checkpoint and then the worker sleeps for the inter-run interval.
    await wait_until(lambda: env.registry.get(sub_id).runs >= 1)  # type: ignore[union-attr]

    # Advance the clock past the wall-clock timeout so the *next* run
    # sees the ticket has been stuck too long.
    clock.advance(301.0)

    # Wait for the second run (the wall-clock check triggers on this run).
    await wait_until(lambda: env.registry.get(sub_id).runs >= 2)  # type: ignore[union-attr]

    info = env.registry.get(sub_id)
    assert info is not None
    # Monitor stays alive — switched to event-driven wait by wall-clock.
    assert info.status is not SubsessionStatus.CLOSED

    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_periodic_steering_message_wakes_the_sleep_early() -> None:
    """A queued message interrupts the inter-run sleep and feeds the run."""
    agent = FakeAgent(["baseline", "focused report"])
    settings = make_settings(max_runs_progress_extension=0)
    env = build_env(agent=agent, settings=settings)

    # A long interval — the test only finishes quickly if the wake works.
    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=30.0,
        max_runs=2,
        include_previous_result=True,
        prompt="watch the build",
    )
    await wait_until(lambda: len(agent.calls) == 1)
    env.registry.enqueue_message(sub_id, "parent", "focus on flaky tests")
    await _await_worker(env, sub_id, timeout=3.0)

    assert len(agent.calls) == 2
    second_input = agent.calls[1]["message"]
    assert "watch the build" in second_input
    assert "Previous run result:\nbaseline" in second_input
    assert "New instructions received since the last run:" in second_input
    assert "focus on flaky tests" in second_input

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.close_reason == "max_runs"


# ---------------------------------------------------------------------------
# failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_failure_marks_failed_and_delivers_summary() -> None:
    """An agent exception fails the subsession and reports to the parent."""
    agent = FakeAgent(error=RuntimeError("kaboom"))
    env = build_env(agent=agent)

    sub_id = _spawn(env, title="fragile")
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED
    # _format_worker_error now includes the exception type for unclassified
    # errors so opaque SDK strings are never passed through verbatim.
    assert "[RuntimeError] kaboom" in (info.error or "")
    assert info.summary is not None
    assert "kaboom" in info.summary
    # The task subsession should have exhausted its retry budget.
    assert info.retry_count == 3

    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    label, reply = history[0]
    assert "failed" in label
    assert reply == info.summary


# ---------------------------------------------------------------------------
# _format_worker_error / _truncate unit tests
# ---------------------------------------------------------------------------


def test_format_degenerate_success_error() -> None:
    """A degenerate-success exception gets a clear explanation."""
    exc = RuntimeError("Claude Code returned an error result: success")
    result = _format_worker_error(exc)
    assert "degenerate success frame" in result
    assert "known Claude SDK bug" in result
    assert "Original SDK message:" in result


def test_format_usage_exhausted_error() -> None:
    """A usage-exhausted exception gets a clear message about credits."""
    exc = RuntimeError("You are out of usage credits for this tier")
    result = _format_worker_error(exc)
    assert "usage credits" in result.lower()
    assert "exhausted" in result.lower()


def test_format_process_error_with_exit_code_and_stderr() -> None:
    """A ProcessError-like exception includes exit code and stderr."""
    exc = RuntimeError("command failed")
    exc.exit_code = 1  # type: ignore[attr-defined]
    exc.stderr = "permission denied\nfatal error"  # type: ignore[attr-defined]
    result = _format_worker_error(exc)
    assert "exited with code 1" in result
    assert "permission denied" in result
    assert "command failed" in result


def test_format_process_error_no_stderr() -> None:
    """ProcessError without stderr still includes the exit code."""
    exc = RuntimeError("something broke")
    exc.exit_code = 2  # type: ignore[attr-defined]
    result = _format_worker_error(exc)
    assert "exited with code 2" in result
    assert "stderr:" not in result


def test_format_process_error_empty_stderr() -> None:
    """ProcessError with empty stderr omits the stderr line."""
    exc = RuntimeError("fail")
    exc.exit_code = 3  # type: ignore[attr-defined]
    exc.stderr = "  "  # type: ignore[attr-defined]
    result = _format_worker_error(exc)
    assert "exited with code 3" in result
    assert "stderr:" not in result


def test_format_unknown_error_preserves_message() -> None:
    """An unrecognized error includes the exception type for diagnostics."""
    exc = RuntimeError("kaboom")
    result = _format_worker_error(exc)
    assert result == "[RuntimeError] kaboom"


def test_format_unknown_error_type_name_in_message() -> None:
    """When the type name is already in the message it is not duplicated."""
    exc = RuntimeError("RuntimeError: kaboom")
    result = _format_worker_error(exc)
    assert result == "RuntimeError: kaboom"


def test_truncate_short() -> None:
    """Short text is not truncated."""
    assert _truncate("hello", 10) == "hello"


def test_truncate_long() -> None:
    """Long text is truncated with trailing '...'."""
    assert _truncate("hello world", 5) == "hello..."


def test_truncate_exact() -> None:
    """Text at the exact limit is not truncated."""
    assert _truncate("hello", 5) == "hello"


# ---------------------------------------------------------------------------
# nested subsessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nested_child_delivers_summary_to_parent_inbox() -> None:
    """A child's terminal summary lands in its (active) parent's inbox."""
    agent = FakeAgent(["child result"])
    env = build_env(agent=agent)
    # Parent registered directly (no worker) — stays active.
    parent = env.registry.create(
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="parent",
        prompt="chat",
        model_level=3,
    )

    child_id = _spawn(env, parent_id=parent.id, depth=2, title="child")
    await _await_worker(env, child_id)

    messages = env.registry.drain_inbox(parent.id)
    assert len(messages) == 1
    assert messages[0].role == "parent"
    assert f"[Subsession {child_id[:8]} (task) 'child' completed]" in messages[0].text
    assert "child result" in messages[0].text
    # NOT delivered to the conversation store — the parent inbox got it.
    assert env.conversation_store.history(OWNER) == []


@pytest.mark.asyncio
async def test_nested_child_falls_back_to_store_when_parent_terminal() -> None:
    """When the parent is already terminal the summary goes to the store."""
    agent = FakeAgent(["orphan result"])
    env = build_env(agent=agent)
    parent = env.registry.create(
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="parent",
        prompt="chat",
        model_level=3,
    )
    env.registry.mark_closed(parent.id, summary="gone", reason="completed")

    child_id = _spawn(env, parent_id=parent.id, depth=2, title="orphan")
    await _await_worker(env, child_id)

    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    assert history[0][1] == "orphan result"
    assert f"[Subsession {child_id[:8]}" in history[0][0]


# ---------------------------------------------------------------------------
# external cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_cancel_mid_turn_no_double_delivery() -> None:
    """``cancel_and_close`` during a turn cancels cleanly with one outcome."""
    gate = asyncio.Event()  # never set — the turn blocks forever
    agent = FakeAgent(["never seen"], gate=gate)
    env = build_env(agent=agent)

    sub_id = _spawn(env)
    await wait_until(lambda: len(agent.calls) == 1)
    worker = env.registry._running[sub_id]

    closed = env.registry.cancel_and_close(
        sub_id, reason="closed by parent", closed_by="parent"
    )
    assert closed is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "closed by parent"
    # The worker's CancelledError path delivers nothing — the registry is
    # already terminal and the caller decides about summary delivery.
    assert env.conversation_store.history(OWNER) == []
    # Idempotent: the external close won exactly once.
    assert (
        env.registry.cancel_and_close(sub_id, reason="again", closed_by="user") is None
    )


# ---------------------------------------------------------------------------
# spawn validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_capacity_error_when_cap_reached() -> None:
    """Spawning beyond ``max_concurrent`` raises ``SubsessionCapacityError``."""
    gate = asyncio.Event()
    agent = FakeAgent(["ok"], gate=gate)
    env = build_env(agent=agent, settings=make_settings(max_concurrent=1))

    first = _spawn(env)
    with pytest.raises(SubsessionCapacityError):
        _spawn(env)

    # Cleanup: cancel the blocked worker.
    worker = env.registry._running[first]
    env.registry.cancel_and_close(first, reason="teardown", closed_by="system")
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_spawn_per_session_capacity_error() -> None:
    """Spawning beyond per-session cap raises SubsessionCapacityError."""
    gate = asyncio.Event()
    agent = FakeAgent(["ok"], gate=gate)
    env = build_env(
        agent=agent,
        settings=make_settings(max_concurrent=100, max_concurrent_per_session=1),
    )

    first = _spawn(env)
    with pytest.raises(SubsessionCapacityError, match="per-session"):
        _spawn(env)

    # Cleanup.
    env.registry.cancel_and_close(first, reason="teardown", closed_by="system")
    worker = env.registry._running[first]
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_spawn_stale_reclaim_frees_slot() -> None:
    """A stale SLEEPING subsession from another owner is reclaimed to free a slot."""
    from robotsix_chat.subsessions.models import SubsessionStatus

    gate = asyncio.Event()
    agent = FakeAgent(["ok"], gate=gate)

    # Use a controlled clock so last_activity_at can be aged past the
    # reclaim threshold.
    clock = FakeClock(1000.0)
    registry = SubsessionRegistry(store_path=None, clock=clock)
    env = build_env(
        agent=agent,
        settings=make_settings(max_concurrent=1, stale_reclaim_seconds=10.0),
        registry=registry,
    )

    # Fill the pool with a subsession from owner "A".
    first = spawn_subsession(
        env=env,
        kind=SubsessionKind.TASK,
        owner_session_id="sess-a",
        parent_id=None,
        depth=1,
        title="job",
        prompt="do the thing",
        model_level=3,
    )
    info_a = env.registry.get(first)
    assert info_a is not None

    # Manually set it to SLEEPING and age it past the reclaim threshold.
    info_a.status = SubsessionStatus.SLEEPING
    clock.advance(20.0)
    info_a.last_activity_at = clock.now - 20.0
    assert env.registry.count_active() >= 1  # SLEEPING counts

    # The global pool thinks it's full — but the stale subsession from
    # owner "A" should be reclaimed when owner "B" tries to spawn.
    second = spawn_subsession(
        env=env,
        kind=SubsessionKind.TASK,
        owner_session_id="sess-b",
        parent_id=None,
        depth=1,
        title="job",
        prompt="do the thing",
        model_level=3,
    )
    assert second != first
    assert not env.registry.get(first).is_active  # reclaimed

    # Cleanup.
    env.registry.cancel_and_close(second, reason="teardown", closed_by="system")
    worker = env.registry._running[second]
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_spawn_stale_reclaim_respects_owner_boundary() -> None:
    """A stale subsession from the same owner is NOT reclaimed."""
    from robotsix_chat.subsessions.models import SubsessionStatus

    gate = asyncio.Event()
    agent = FakeAgent(["ok"], gate=gate)
    clock = FakeClock(1000.0)
    registry = SubsessionRegistry(store_path=None, clock=clock)
    env = build_env(
        agent=agent,
        settings=make_settings(max_concurrent=1, stale_reclaim_seconds=10.0),
        registry=registry,
    )

    first = spawn_subsession(
        env=env,
        kind=SubsessionKind.TASK,
        owner_session_id="sess-main",
        parent_id=None,
        depth=1,
        title="job",
        prompt="do the thing",
        model_level=3,
    )
    info = env.registry.get(first)
    assert info is not None
    info.status = SubsessionStatus.SLEEPING
    clock.advance(20.0)
    info.last_activity_at = clock.now - 20.0

    # Same owner cannot reclaim its own stale subsession.
    with pytest.raises(SubsessionCapacityError):
        spawn_subsession(
            env=env,
            kind=SubsessionKind.TASK,
            owner_session_id="sess-main",
            parent_id=None,
            depth=1,
            title="job",
            prompt="do the thing",
            model_level=3,
        )

    # Cleanup.
    env.registry.cancel_and_close(first, reason="teardown", closed_by="system")
    worker = env.registry._running[first]
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)


# ---------------------------------------------------------------------------
# slot-budget admission (per-conversation monitor slots)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slot_budget_reuses_paused_monitor_slot() -> None:
    """At budget, a new monitor reclaims the least-recently-active paused one."""
    from robotsix_chat.subsessions.slot_budget import SlotBudget

    gate = asyncio.Event()
    agent = FakeAgent(["ok"], gate=gate)
    env = build_env(
        agent=agent,
        settings=make_settings(monitor_slot_budget=2, monitor_slot_queue_max=4),
    )
    assert isinstance(env.slot_budget, SlotBudget)

    # Occupy both slots with periodic monitors; pause the older one.
    first = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="old", interval_seconds=60.0
    )
    second = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="young", interval_seconds=60.0
    )
    env.registry.mark_paused(first, summary="no change")
    assert env.registry.count_occupied_for_owner(OWNER) == 2

    # At budget: the new request reclaims the paused monitor's slot.
    third = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="new", interval_seconds=60.0
    )
    assert third != first
    assert env.registry.get(first) is not None
    assert not env.registry.get(first).is_active
    assert env.registry.get(first).close_reason == "slot_reclaimed"
    # Occupied count unchanged: the reclaimed slot was repurposed.
    assert env.registry.count_occupied_for_owner(OWNER) == 2
    # The live monitor was never evicted.
    assert env.registry.get(second).is_active

    # Cleanup.
    for sub_id in (second, third):
        env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")
        worker = env.registry._running[sub_id]
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_slot_budget_queues_when_all_slots_active() -> None:
    """At budget with every slot active, the request is queued — no eviction."""
    gate = asyncio.Event()
    agent = FakeAgent(["ok"], gate=gate)
    env = build_env(
        agent=agent,
        settings=make_settings(monitor_slot_budget=1, monitor_slot_queue_max=4),
    )

    first = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="active", interval_seconds=60.0
    )
    queued_id = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="pending", interval_seconds=60.0
    )
    assert queued_id == "__slot_budget_queued__"
    assert env.slot_budget.pending_count(OWNER) == 1
    # The active monitor was NOT evicted to admit the new request.
    assert env.registry.get(first).is_active
    assert env.registry.count_occupied_for_owner(OWNER) == 1

    # Cleanup.
    env.registry.cancel_and_close(first, reason="teardown", closed_by="system")
    worker = env.registry._running[first]
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_slot_budget_drains_queue_when_slot_frees() -> None:
    """When a monitor terminates, the oldest pending request is spawned."""
    gate = asyncio.Event()
    agent = FakeAgent(["ok"], gate=gate)
    env = build_env(
        agent=agent,
        settings=make_settings(monitor_slot_budget=1, monitor_slot_queue_max=4),
    )

    first = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="active", interval_seconds=60.0
    )
    queued_id = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="pending", interval_seconds=60.0
    )
    assert queued_id == "__slot_budget_queued__"
    assert env.slot_budget.pending_count(OWNER) == 1

    # Free the slot: the pending request spawns into it.
    env.registry.cancel_and_close(first, reason="completed", closed_by="system")
    worker = env.registry._running[first]
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)

    assert env.slot_budget.pending_count(OWNER) == 0
    assert env.registry.count_occupied_for_owner(OWNER) == 1
    spawned = [
        info
        for info in env.registry.list_for_owner(OWNER)
        if info.is_active and info.title == "pending"
    ]
    assert len(spawned) == 1

    # Cleanup.
    env.registry.cancel_and_close(spawned[0].id, reason="teardown", closed_by="system")
    spawned_worker = env.registry._running[spawned[0].id]
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(spawned_worker, 2.0)


@pytest.mark.asyncio
async def test_slot_budget_rejects_when_queue_full() -> None:
    """A request beyond the queue cap is rejected with a clear error."""
    gate = asyncio.Event()
    agent = FakeAgent(["ok"], gate=gate)
    env = build_env(
        agent=agent,
        settings=make_settings(monitor_slot_budget=1, monitor_slot_queue_max=1),
    )

    first = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="active", interval_seconds=60.0
    )
    queued_id = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="pending", interval_seconds=60.0
    )
    assert queued_id == "__slot_budget_queued__"
    with pytest.raises(SubsessionCapacityError, match="queue.*cap"):
        _spawn(env, kind=SubsessionKind.PERIODIC, title="third", interval_seconds=60.0)
    # The queue did not grow past its cap.
    assert env.slot_budget.pending_count(OWNER) == 1

    # Cleanup.
    env.registry.cancel_and_close(first, reason="teardown", closed_by="system")
    worker = env.registry._running[first]
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_slot_budget_disabled_admits_unbounded() -> None:
    """With budget 0 (disabled), monitor spawns proceed as before."""
    gate = asyncio.Event()
    agent = FakeAgent(["ok"], gate=gate)
    env = build_env(
        agent=agent,
        settings=make_settings(monitor_slot_budget=0),
    )
    assert env.slot_budget is None

    first = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="one", interval_seconds=60.0
    )
    second = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="two", interval_seconds=60.0
    )
    assert first != "__slot_budget_queued__"
    assert second != "__slot_budget_queued__"
    assert env.registry.count_occupied_for_owner(OWNER) == 2

    # Cleanup.
    for sub_id in (first, second):
        env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")
        worker = env.registry._running[sub_id]
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_slot_budget_ignores_one_shot_subsessions() -> None:
    """task/user_chat subsessions are not monitors and never queue."""
    gate = asyncio.Event()
    agent = FakeAgent(["ok"], gate=gate)
    env = build_env(
        agent=agent,
        settings=make_settings(monitor_slot_budget=1, monitor_slot_queue_max=4),
    )

    first = _spawn(env, kind=SubsessionKind.TASK, title="one")
    second = _spawn(env, kind=SubsessionKind.TASK, title="two")
    assert second != "__slot_budget_queued__"
    assert env.slot_budget.pending_count(OWNER) == 0

    # Cleanup.
    for sub_id in (first, second):
        env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")
        worker = env.registry._running[sub_id]
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_slot_budget_discards_pending_on_session_close() -> None:
    """Closing a whole conversation drops its pending queue (no respawn)."""
    gate = asyncio.Event()
    agent = FakeAgent(["ok"], gate=gate)
    env = build_env(
        agent=agent,
        settings=make_settings(monitor_slot_budget=1, monitor_slot_queue_max=4),
    )

    first = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="active", interval_seconds=60.0
    )
    queued_id = _spawn(
        env, kind=SubsessionKind.PERIODIC, title="pending", interval_seconds=60.0
    )
    assert queued_id == "__slot_budget_queued__"
    assert env.slot_budget.pending_count(OWNER) == 1

    # Tear down the conversation: the pending queue must be discarded,
    # not drained into the dying session.
    from robotsix_chat.subsessions.registry import OWNER_CLOSED_REASON

    closed = env.registry.close_all_for_owner(OWNER, reason=OWNER_CLOSED_REASON)
    assert closed == 1
    assert env.slot_budget.pending_count(OWNER) == 0
    # No new worker was spawned for the queued request.
    assert env.registry.count_occupied_for_owner(OWNER) == 0

    # Cleanup.
    worker = env.registry._running[first]
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)


def test_spawn_depth_error_beyond_max_depth() -> None:
    """Spawning deeper than ``max_depth`` raises ``SubsessionDepthError``."""
    env = build_env(settings=make_settings(max_depth=2))

    with pytest.raises(SubsessionDepthError):
        _spawn(env, depth=3)

    assert env.registry.list_for_owner(OWNER) == []


def test_spawn_level_errors() -> None:
    """Invalid levels and keyless key-bearing levels raise level errors."""
    env = build_env(settings=make_settings(llmio_api_key=""))

    with pytest.raises(SubsessionLevelError):
        _spawn(env, model_level=5)
    with pytest.raises(SubsessionLevelError):
        _spawn(env, model_level=1)  # level 1 needs an API key

    assert env.registry.list_for_owner(OWNER) == []


@pytest.mark.asyncio
async def test_spawn_monitor_model_level_clamped() -> None:
    """Periodic/wait_for_event monitors are clamped to monitor_max_model_level."""
    env = build_env(
        settings=make_settings(
            monitor_max_model_level=2,
            default_model_level=3,
            llmio_api_key="test-key",
        )
    )

    # Periodic at model_level=4 gets clamped to 2.
    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        model_level=4,
        interval_seconds=10.0,
    )
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.model_level == 2

    # wait_for_event at model_level=3 gets clamped to 2.
    sub_id2 = _spawn(
        env,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        model_level=3,
        event_timeout_seconds=60.0,
    )
    info2 = env.registry.get(sub_id2)
    assert info2 is not None
    assert info2.model_level == 2

    # Task at model_level=4 is NOT clamped (task is uncapped).
    sub_id3 = _spawn(
        env,
        kind=SubsessionKind.TASK,
        model_level=4,
    )
    info3 = env.registry.get(sub_id3)
    assert info3 is not None
    assert info3.model_level == 4

    # Periodic at model_level=2 (within cap) is not clamped.
    sub_id4 = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        model_level=2,
        interval_seconds=10.0,
    )
    info4 = env.registry.get(sub_id4)
    assert info4 is not None
    assert info4.model_level == 2


@pytest.mark.asyncio
async def test_periodic_parent_cannot_spawn_periodic_or_on_close_child() -> None:
    """A periodic subsession cannot spawn periodic or on_close children."""
    env = build_env()
    # Register a periodic parent.
    parent = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="parent periodic",
        prompt="monitor",
        model_level=3,
        interval_seconds=10.0,
    )

    # Periodic children are blocked.
    with pytest.raises(SubsessionPeriodicSpawnError, match="periodic"):
        _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            parent_id=parent.id,
            depth=2,
            interval_seconds=5.0,
        )

    # On_close children are also blocked.
    with pytest.raises(SubsessionPeriodicSpawnError, match="on_close"):
        _spawn(
            env,
            kind=SubsessionKind.ON_CLOSE,
            parent_id=parent.id,
            depth=2,
        )

    # Task children from a periodic parent are now allowed (sibling spawn
    # is handled at the tool layer; the worker allows direct task spawns).
    task_id = _spawn(
        env,
        kind=SubsessionKind.TASK,
        parent_id=parent.id,
        depth=2,
    )
    assert task_id
    env.registry.cancel_and_close(task_id, reason="teardown", closed_by="system")

    # User_chat children from a periodic parent are still allowed.
    chat_id = _spawn(
        env,
        kind=SubsessionKind.USER_CHAT,
        parent_id=parent.id,
        depth=2,
    )
    assert chat_id
    env.registry.cancel_and_close(chat_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_user_chat_parent_cannot_spawn_user_chat_child() -> None:
    """A user_chat subsession cannot spawn another user_chat subsession."""
    env = build_env()
    # Register a user_chat parent.
    parent = env.registry.create(
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="parent user_chat",
        prompt="chat",
        model_level=3,
    )

    from robotsix_chat.subsessions import SubsessionUserChatSpawnError

    with pytest.raises(SubsessionUserChatSpawnError, match="user_chat"):
        _spawn(
            env,
            kind=SubsessionKind.USER_CHAT,
            parent_id=parent.id,
            depth=2,
        )

    # Non-user_chat children (e.g. task) are still allowed.
    task_id = _spawn(
        env,
        kind=SubsessionKind.TASK,
        parent_id=parent.id,
        depth=2,
    )
    assert task_id
    # Clean up the spawned worker.
    env.registry.cancel_and_close(task_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_non_user_chat_parent_can_spawn_user_chat_child() -> None:
    """A task or periodic parent can still spawn user_chat children."""
    env = build_env()
    parent = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="parent periodic",
        prompt="monitor",
        model_level=3,
        interval_seconds=10.0,
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.USER_CHAT,
        parent_id=parent.id,
        depth=2,
    )
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.kind is SubsessionKind.USER_CHAT
    # Clean up the spawned worker.
    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_non_periodic_parent_can_spawn_periodic_child() -> None:
    """A task or user_chat parent can still spawn periodic children."""
    env = build_env()
    parent = env.registry.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="parent task",
        prompt="work",
        model_level=3,
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        parent_id=parent.id,
        depth=2,
        interval_seconds=10.0,
    )
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.kind is SubsessionKind.PERIODIC
    # Clean up the spawned worker.
    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


# ---------------------------------------------------------------------------
# periodic tick with no condition → no sibling spawn (AC 5, ticket 20260801T151631Z)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_no_condition_tick_performs_no_sibling_spawn() -> None:
    """A periodic tick that detects no condition does not initiate any sibling spawn.

    The worker's periodic loop never calls spawn_subsession on a tick —
    spawning is purely agent/LLM-driven via the spawn_subsession tool.
    This test proves that a NO_CHANGE tick leaves the registry with no
    new children.
    """
    agent = FakeAgent(["NO_CHANGE"])
    env = build_env(agent=agent)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="monitor",
    )
    await _await_worker(env, sub_id)

    # The periodic parent completed its one tick.
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.runs == 1

    # No sibling subsession was spawned — the agent's NO_CHANGE reply
    # does not trigger any spawn_subsession path.
    children = env.registry.list_descendants(sub_id)
    assert children == [], f"expected no children, got {len(children)}"

    # Only one agent call — no extra turns or tool invocations.
    assert len(agent.calls) == 1


# ---------------------------------------------------------------------------
# idempotent spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_with_duplicate_sub_id_is_idempotent() -> None:
    """Spawning with the same sub_id twice does not create a second worker."""
    agent = FakeAgent(["result"])
    env = build_env(agent=agent)

    first_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="only once",
        prompt="do it",
        model_level=3,
        sub_id="fixed-id-001",
    )
    # Second spawn with the same explicit id returns the existing id
    # without launching another worker.
    second_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="impostor",
        prompt="evil twin",
        model_level=3,
        sub_id="fixed-id-001",
    )

    assert first_id == second_id == "fixed-id-001"
    await _await_worker(env, first_id)

    # Only one agent call — the duplicate spawn did not launch a second worker.
    assert len(agent.calls) == 1
    info = env.registry.get(first_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED


# ---------------------------------------------------------------------------
# component_request availability check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_closes_when_component_request_unavailable() -> None:
    """A periodic subsession closes immediately when central_deploy.url is empty.

    Without component_request the monitor cannot fetch ticket state,
    so the subsession is closed before the first turn to prevent
    futile retries and child-task churn.
    """
    agent = FakeAgent(["should not be called"])
    # Settings with no central_deploy URL
    settings = make_settings()
    settings.central_deploy = SimpleNamespace(url="")
    env = build_env(agent=agent, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        title="monitor",
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "missing_tool"
    assert "component_request" in (info.summary or "")
    # The agent must never have been called.
    assert len(agent.calls) == 0


# ---------------------------------------------------------------------------
# run guard (periodic duplicate-execution prevention)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_guard_records_executed_runs() -> None:
    """Records completed runs in ``completed_runs``.

    After a periodic run completes, the run number is persisted and
    ``claim_run`` returns ``False`` for the same run.
    """
    agent = FakeAgent(["report 1", "report 2"])
    env = build_env(agent=agent)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=2,
        title="guarded",
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # Both run 1 and run 2 should be recorded as completed.
    assert 1 in info.completed_runs
    assert 2 in info.completed_runs
    # claim_run returns False for already-executed runs.
    assert env.registry.claim_run(sub_id, 1) is False
    assert env.registry.claim_run(sub_id, 2) is False


@pytest.mark.asyncio
async def test_run_guard_survives_duplicate_worker_race() -> None:
    """Concurrent spawn attempts cannot produce duplicate run-1 execution."""
    agent = FakeAgent(["run-1-result", "run-1-dup", "run-2-result"])
    settings = make_settings(max_runs_progress_extension=0)
    env = build_env(agent=agent, settings=settings)

    # Simulate a race: create the subsession manually, then call
    # spawn_subsession with the same sub_id while the first worker
    # is mid-flight.
    sub_id = "race-id-001"
    first_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="racer",
        prompt="monitor",
        model_level=3,
        interval_seconds=0.02,
        max_runs=1,
        sub_id=sub_id,
    )
    # Wait for the first worker to start and claim run 1.
    await wait_until(lambda: len(agent.calls) >= 1)

    # The second spawn_subsession returns the existing id (no new worker).
    second_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="racer-impostor",
        prompt="evil twin",
        model_level=3,
        interval_seconds=0.02,
        max_runs=1,
        sub_id=sub_id,
    )
    assert first_id == second_id == sub_id

    await _await_worker(env, sub_id)

    # Exactly one run-1 execution (not two).
    assert len(agent.calls) == 1
    info = env.registry.get(sub_id)
    assert info is not None
    assert 1 in info.completed_runs


@pytest.mark.asyncio
async def test_run_guard_fast_forwards_without_sleeping() -> None:
    """A stale run counter fast-forwards past completed runs instantly.

    Regression: when the counter lags ``completed_runs`` (a pre-fix
    persisted store resumed at runs=0), each collision used to sleep a
    full interval before trying the next number.  The 60 s interval
    here makes any such sleep overshoot the test's wait budget.
    """
    agent = FakeAgent(["run 4 result"], gate=asyncio.Event())
    env = build_env(agent=agent)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        max_runs=10,
        title="stale-counter",
        completed_runs={1, 2, 3},
    )

    await wait_until(lambda: len(agent.calls) >= 1)
    info = env.registry.get(sub_id)
    assert info is not None
    # The worker skipped 1..3 without sleeping and claimed run 4.
    assert info.runs == 3
    assert 4 in info.completed_runs

    worker = env.registry._running.get(sub_id)
    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")
    if worker is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


# ---------------------------------------------------------------------------
# reaper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_cancels_orphaned_timer() -> None:
    """A timer whose subsession is not in any conversation tree is reaped."""
    agent = FakeAgent(["tick"])
    env = build_env(agent=agent)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.05,
        title="orphan-me",
    )
    await wait_until(lambda: len(agent.calls) == 1)

    # Simulate tree-record loss: remove the subsession from _by_owner
    # but leave the worker running.
    info = env.registry.get(sub_id)
    assert info is not None
    # Remove from the owner's tree.
    owner_set = env.registry._by_owner.get(OWNER)
    if owner_set is not None:
        owner_set.discard(sub_id)

    # The worker is still alive — verify it has a running task.
    task = env.registry._running.get(sub_id)
    assert task is not None
    assert not task.done()

    # Reap should find and cancel the orphan.
    reaped = env.registry.reap_orphans()
    assert reaped >= 1

    # The timer should now be cancelled.
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 2.0)
    assert task.cancelled() or task.done()

    # The subsession must be terminal so it no longer counts as active.
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED
    assert info.error == "orphaned_timer_reaped"


# ---------------------------------------------------------------------------
# complete_subsession failure when parent link is gone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_subsession_fails_when_subsession_inactive() -> None:
    """Calling complete_subsession on an already-closed subsession returns error."""
    agent = FakeAgent(["ok"])
    factory = CapturingAgentFactory(agent)
    env = build_env(agent_factory=factory)

    sub_id = _spawn(env, kind=SubsessionKind.TASK, title="ephemeral")
    await _await_worker(env, sub_id)

    # The subsession is now CLOSED. Reconstruct the complete_subsession
    # tool to verify it returns an error.
    close_state = factory.captured[0]["close_state"]
    # Simulate the agent calling complete_subsession after close.
    # The tool checks registry.is_active and returns an error.
    from robotsix_chat.subsessions.tools import build_subsession_tools

    ctx = SubsessionContext(
        owner_session_id=OWNER,
        subsession_id=sub_id,
        depth=1,
    )
    tools = build_subsession_tools(env, ctx=ctx, close_state=close_state)
    complete_tool = [t for t in tools if t.__name__ == "complete_subsession"][0]

    result = await complete_tool("trying to complete after close")
    assert "Error" in result or "no longer active" in result
    # The close state should NOT have been flipped.
    assert not close_state.requested


def test_rebuild_turn_history_parses_valid_pairs() -> None:
    """``_rebuild_turn_history`` converts persisted list-of-lists to tuples."""
    from robotsix_chat.subsessions.resume import _rebuild_turn_history

    entry = {"turn_history": [["in 1", "out 1"], ["in 2", "out 2"]]}

    assert _rebuild_turn_history(entry) == [("in 1", "out 1"), ("in 2", "out 2")]


def test_rebuild_turn_history_ignores_malformed_entries() -> None:
    """Malformed items (wrong shape/type) are dropped, not raised on."""
    from robotsix_chat.subsessions.resume import _rebuild_turn_history

    entry = {
        "turn_history": [
            ["ok in", "ok out"],
            ["only one"],
            [1, 2],
            "not a list",
            None,
        ]
    }

    assert _rebuild_turn_history(entry) == [("ok in", "ok out")]


def test_rebuild_turn_history_missing_field_returns_empty() -> None:
    """A persisted entry without ``turn_history`` (older format) is fine."""
    from robotsix_chat.subsessions.resume import _rebuild_turn_history

    assert _rebuild_turn_history({}) == []


# ---------------------------------------------------------------------------
# dedup key spawn guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_dedup_guard_returns_existing_id_for_active_key() -> None:
    """When a subsession with the same dedup_key is active, spawn returns its id."""
    env = build_env()
    first_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="first side-chat",
        prompt="ask user about X",
        model_level=3,
        dedup_key="asyncio.run-crash",
    )

    # Second spawn with the same dedup_key — must return the first id,
    # not create a new subsession.
    second_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="duplicate side-chat",
        prompt="ask user about X again",
        model_level=3,
        dedup_key="asyncio.run-crash",
    )

    assert first_id == second_id
    # Only one subsession exists in the registry.
    assert len(env.registry.list_for_owner(OWNER)) == 1

    # Clean up the spawned worker.
    env.registry.cancel_and_close(first_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_spawn_dedup_guard_works_for_all_kinds() -> None:
    """A dedup_key on any subsession kind prevents duplicate spawns."""
    env = build_env()
    first_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="first task",
        prompt="do work",
        model_level=3,
        dedup_key="some-key",
    )

    second_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="second task",
        prompt="do more work",
        model_level=3,
        dedup_key="some-key",
    )

    assert first_id == second_id
    assert len(env.registry.list_for_owner(OWNER)) == 1

    # Clean up spawned worker.
    env.registry.cancel_and_close(first_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_spawn_dedup_guard_periodic_monitor_dedup() -> None:
    """A periodic monitor with a ticket-id dedup_key prevents duplicate monitors."""
    env = build_env()
    first_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="monitor ticket 5f1c",
        prompt="track ticket 5f1c state",
        model_level=3,
        interval_seconds=1800,
        max_runs=60,
        dedup_key="5f1c",
    )

    # Second periodic monitor for the same ticket — must return the first id.
    second_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="monitor ticket 5f1c (duplicate)",
        prompt="track ticket 5f1c state again",
        model_level=3,
        interval_seconds=1800,
        max_runs=60,
        dedup_key="5f1c",
    )

    assert first_id == second_id
    assert len(env.registry.list_for_owner(OWNER)) == 1

    # Clean up spawned worker.
    env.registry.cancel_and_close(first_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_spawn_dedup_guard_no_key_creates_fresh() -> None:
    """When no dedup_key is provided, each spawn creates a fresh subsession."""
    env = build_env()
    first_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="chat 1",
        prompt="ask something",
        model_level=3,
    )

    second_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="chat 2",
        prompt="ask something else",
        model_level=3,
    )

    assert first_id != second_id
    assert len(env.registry.list_for_owner(OWNER)) == 2

    # Clean up spawned workers.
    env.registry.cancel_and_close(first_id, reason="teardown", closed_by="system")
    env.registry.cancel_and_close(second_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_spawn_dedup_guard_different_keys_dont_collide() -> None:
    """Different dedup_key values are tracked independently."""
    env = build_env()
    first_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="crash chat",
        prompt="about crash",
        model_level=3,
        dedup_key="crash-error",
    )

    second_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="timeout chat",
        prompt="about timeout",
        model_level=3,
        dedup_key="timeout-error",
    )

    assert first_id != second_id
    assert len(env.registry.list_for_owner(OWNER)) == 2

    # Clean up spawned workers.
    env.registry.cancel_and_close(first_id, reason="teardown", closed_by="system")
    env.registry.cancel_and_close(second_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_spawn_wait_for_event_write_barrier_repairs_checkpoint() -> None:
    """A wait_for_event spawn persists ticket_id into the checkpoint synchronously.

    Even when the caller supplies a checkpoint without ``ticket_id`` (or no
    checkpoint at all), the dedup_key must be merged into the checkpoint
    before ``registry.create`` persists it, so the monitor's first turn never
    observes a checkpoint missing its ticket id.
    """
    env = build_env(agent=FakeAgent(["NO_CHANGE"]))
    sub_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket abc",
        prompt="monitor the ticket",
        model_level=3,
        dedup_key="ticket-abc-123",
        checkpoint={"last_known_state": "open"},
    )

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.checkpoint == {
        "last_known_state": "open",
        "ticket_id": "ticket-abc-123",
    }
    assert info.dedup_key == "ticket-abc-123"

    # Clean up the spawned worker.
    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_spawn_wait_for_event_write_barrier_without_checkpoint() -> None:
    """A wait_for_event spawn without a checkpoint still writes ticket_id."""
    env = build_env(agent=FakeAgent(["NO_CHANGE"]))
    sub_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket xyz",
        prompt="monitor the ticket",
        model_level=3,
        dedup_key="ticket-xyz-789",
    )

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.checkpoint == {"ticket_id": "ticket-xyz-789"}
    assert info.dedup_key == "ticket-xyz-789"

    # Clean up the spawned worker.
    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


# -- _is_no_change / _is_duplicate_reply unit tests ------------------------


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("NO_CHANGE", True),
        ("NO_CHANGE ", True),
        ("no_change", True),
        ("NO_CHANGE.", True),  # startswith catches trailing punctuation
        ("No change", True),  # space variant also caught
        ("No changes", True),
        ("Nothing changed", True),
        ("Nothing has changed", True),
        ("No updates", True),
        ("Unchanged", True),
        ("No new", True),
        ("Everything is the same", True),
        ("All quiet", True),
        ("Status unchanged", True),
        ("No significant change", True),
        ("No meaningful change", True),
        ("  no changes  ", True),
        ("Ticket #123 moved to done", False),
        ("Something actually happened", False),
    ],
)
def test_is_no_change(reply: str, expected: bool) -> None:
    """``_is_no_change`` recognises the sentinel and common paraphrases."""
    assert _is_no_change(reply) == expected


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("QUEUED", True),
        ("QUEUED ", True),
        ("queued", True),
        ("QUEUED.", True),  # startswith catches trailing punctuation
        ("Queued for implementation", True),
        ("Waiting for implementation", True),
        ("In queue", True),
        ("Implementation queued", True),
        ("Awaiting implementation", True),
        ("Pending implementation", True),
        ("  queued  ", True),
        ("NO_CHANGE", False),
        ("Ticket #123 is queued and waiting", False),  # must start with sentinel
        ("Something actually happened", False),
    ],
)
def test_is_queued(reply: str, expected: bool) -> None:
    """``_is_queued`` recognises the queued sentinel and common paraphrases."""
    assert _is_queued(reply) == expected


def test_is_duplicate_reply_none_previous() -> None:
    """A reply is never a duplicate when there's no previous result."""
    assert _is_duplicate_reply("anything", None) is False


def test_is_duplicate_reply_exact_match() -> None:
    """Exact string match is a duplicate."""
    assert _is_duplicate_reply("hello", "hello") is True


def test_is_duplicate_reply_case_insensitive() -> None:
    """Case differences are ignored."""
    assert _is_duplicate_reply("Hello World", "hello world") is True


def test_is_duplicate_reply_whitespace_insensitive() -> None:
    """Leading/trailing whitespace differences are ignored."""
    assert _is_duplicate_reply("  hello  ", "hello") is True


def test_is_duplicate_reply_different() -> None:
    """Different content is not a duplicate."""
    assert _is_duplicate_reply("hello", "goodbye") is False


# -- integration: duplicate non-NO_CHANGE replies are suppressed ------------


@pytest.mark.asyncio
async def test_periodic_duplicate_replies_are_suppressed() -> None:
    """Verbose replies that repeat verbatim are suppressed from the event sink.

    Intermediate runs are never delivered to the parent conversation store
    regardless of suppression — only the terminal summary arrives.
    """
    agent = FakeAgent(["Status: all clear", "Status: all clear"])
    env = build_env(agent=agent)

    sub_id = _spawn(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02, max_runs=2
    )
    await _await_worker(env, sub_id)

    # Only the terminal summary is delivered — both intermediate runs stay silent.
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    assert "max_runs" in history[0][0]


@pytest.mark.asyncio
async def test_periodic_no_change_phrases_are_suppressed() -> None:
    """Common LLM paraphrases of 'no change' are suppressed."""
    agent = FakeAgent(["No changes", "Nothing changed"])
    sink = RecordingSink()
    env = build_env(agent=agent, event_sink=sink)

    sub_id = _spawn(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02, max_runs=2
    )
    await _await_worker(env, sub_id)

    # Neither run should produce a result frame.
    assert sink.of_type(SSE_SUBSESSION_RESULT_TYPE) == []

    # Only the terminal summary is delivered — no per-run turn.
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    assert "max_runs" in history[0][0]


@pytest.mark.asyncio
async def test_periodic_no_change_phrases_count_toward_auto_stop() -> None:
    """No-change phrases increment the consecutive counter for auto-stop."""
    agent = FakeAgent(["No changes", "Nothing changed", "NO_CHANGE"])
    env = build_env(agent=agent, settings=make_settings(auto_stop_no_change_runs=2))

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_auto_stop"
    # Stopped after 2 consecutive no-change runs, not all 3.
    assert len(agent.calls) == 2


# -- transient error retry (periodic subsessions) ---------------------------


class _FakeTransientError(Exception):
    """A synthetic transient error for testing retry logic."""


@pytest.mark.asyncio
async def test_periodic_transient_error_retried_then_succeeds() -> None:
    """Periodic turn retries on transient error, then succeeds on retry."""
    agent = FakeAgent()
    env = build_env(
        agent=agent,
        settings=make_settings(
            transient_error_max_retries=2,
            max_runs_progress_extension=0,
        ),
    )

    # First call raises, second succeeds.
    call_count = 0

    async def _flaky_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _FakeTransientError("upstream hiccup")
        yield "all good"

    with (
        patch.object(agent, "stream", _flaky_stream),
        patch(
            "robotsix_chat.subsessions.worker.is_openrouter_transient",
            return_value=True,
        ),
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
            max_runs=1,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # The subsession should NOT be failed — the retry succeeded.
    assert info.status is not SubsessionStatus.FAILED
    # Two calls: first failed, second succeeded.
    assert call_count == 2


@pytest.mark.asyncio
async def test_periodic_transient_error_exhausted_skips_cycle() -> None:
    """Periodic cycle is skipped (not failed) when transient retries are exhausted."""
    agent = FakeAgent(error=_FakeTransientError("upstream hiccup"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            transient_error_max_retries=1,
            # Use a high threshold so transient-error cycles don't
            # trigger the consecutive-error failure during the test.
            consecutive_error_fail_threshold=1000,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=True,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        # The worker loops forever skipping cycles, so _await_worker
        # will time out.  That's expected — the subsession should still
        # be alive, not failed.
        with pytest.raises(asyncio.TimeoutError):
            await _await_worker(env, sub_id, timeout=0.5)

    info = env.registry.get(sub_id)
    assert info is not None
    # Should NOT be FAILED — cycles were skipped gracefully.
    assert info.status is not SubsessionStatus.FAILED
    # The subsession should be alive (the worker task loops forever
    # skipping cycles).
    assert info.is_active


@pytest.mark.asyncio
async def test_task_transient_error_not_retried() -> None:
    """TASK subsessions do NOT retry transient errors — they fail immediately."""
    agent = FakeAgent(error=_FakeTransientError("upstream hiccup"))
    env = build_env(agent=agent)

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=True,
    ):
        sub_id = _spawn(env, prompt="compute", kind=SubsessionKind.TASK)
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # TASK subsession should be FAILED — no retry for non-periodic.
    assert info.status is SubsessionStatus.FAILED


@pytest.mark.asyncio
async def test_periodic_non_transient_error_not_retried_when_disabled() -> None:
    """Periodic subsessions use consecutive-error threshold, not worker retries."""
    agent = FakeAgent(error=ValueError("not transient"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            monitor_error_max_retries=0,
            consecutive_error_fail_threshold=1,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED
    # With threshold=1, one errored run fails the subsession.
    assert info.consecutive_errored_runs >= 1
    assert "not transient" in (info.error or "")


@pytest.mark.asyncio
async def test_periodic_non_transient_error_retries_then_fails() -> None:
    """Periodic monitors track consecutive errored runs and fail at threshold."""
    agent = FakeAgent(error=ValueError("tool retry limit"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            monitor_error_max_retries=2,
            consecutive_error_fail_threshold=3,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # After 3 consecutive errored runs the monitor is permanently failed.
    assert info.status is SubsessionStatus.FAILED
    assert info.consecutive_errored_runs >= 3
    assert "tool retry limit" in (info.error or "")
    # The error summary mentions consecutive errored runs.
    assert "consecutive errored runs" in (info.error or "")


@pytest.mark.asyncio
async def test_periodic_non_transient_error_retry_succeeds() -> None:
    """Periodic monitor that fails then succeeds continues running."""
    call_count = 0

    class FailOnceAgent:
        """Fails on the first call, succeeds on subsequent calls."""

        def __init__(self) -> None:
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
            nonlocal call_count
            call_count += 1
            self.calls.append({"message": message, "session_id": session_id})
            if call_count == 1:
                raise ValueError("transient glitch")
            yield "queue drained successfully"

    agent = FailOnceAgent()
    env = build_env(
        agent=agent,
        settings=make_settings(
            monitor_error_max_retries=2,
            consecutive_error_fail_threshold=3,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
            max_runs=2,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # The monitor should have completed (not FAILED).
    assert info.status is SubsessionStatus.CLOSED
    # The consecutive error counter was reset after the successful run.
    assert info.consecutive_errored_runs == 0
    assert info.runs >= 2


@pytest.mark.asyncio
async def test_wait_for_event_non_transient_error_retries() -> None:
    """WAIT_FOR_EVENT monitors track consecutive errored runs and fail at threshold."""
    agent = FakeAgent(error=ValueError("tool error"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            monitor_error_max_retries=1,
            consecutive_error_fail_threshold=1,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.WAIT_FOR_EVENT,
            dedup_key="ticket-123",
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED
    assert info.consecutive_errored_runs >= 1
    assert "consecutive errored runs" in (info.error or "")


@pytest.mark.asyncio
async def test_periodic_transient_error_transcript_recorded() -> None:
    """When transient retries are exhausted, a transcript entry is recorded."""
    agent = FakeAgent(error=_FakeTransientError("upstream hiccup"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            transient_error_max_retries=0,
            consecutive_error_fail_threshold=1000,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=True,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        with pytest.raises(asyncio.TimeoutError):
            await _await_worker(env, sub_id, timeout=0.5)

    info = env.registry.get(sub_id)
    assert info is not None
    # Check transcript has a system message about the errored run.
    system_entries = [e for e in info.transcript if e.role == "system"]
    assert any("Run errored" in (e.text or "") for e in system_entries)


@pytest.mark.asyncio
async def test_periodic_unexpected_model_behavior_retried_then_succeeds() -> None:
    """Periodic turn retries on UnexpectedModelBehavior, then succeeds."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    agent = FakeAgent()
    env = build_env(
        agent=agent,
        settings=make_settings(
            transient_error_max_retries=2,
            max_runs_progress_extension=0,
        ),
    )

    call_count = 0

    async def _flaky_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise UnexpectedModelBehavior("Streamed response ended without content")
        yield "recovered"

    with patch.object(agent, "stream", _flaky_stream):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
            max_runs=1,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # The subsession should NOT be failed — the retry succeeded.
    assert info.status is not SubsessionStatus.FAILED
    assert call_count == 2


@pytest.mark.asyncio
async def test_periodic_unexpected_model_behavior_exhausted_skips_cycle() -> None:
    """Periodic cycle is skipped when UnexpectedModelBehavior retries exhaust."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    agent = FakeAgent(error=UnexpectedModelBehavior("bad response shape"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            transient_error_max_retries=1,
            consecutive_error_fail_threshold=1000,
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
    )
    with pytest.raises(asyncio.TimeoutError):
        await _await_worker(env, sub_id, timeout=0.5)

    info = env.registry.get(sub_id)
    assert info is not None
    # Should NOT be FAILED — cycles were skipped gracefully.
    assert info.status is not SubsessionStatus.FAILED
    assert info.is_active


@pytest.mark.asyncio
async def test_task_unexpected_model_behavior_not_retried() -> None:
    """TASK subsessions do NOT retry UnexpectedModelBehavior — they fail immediately."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    agent = FakeAgent(error=UnexpectedModelBehavior("bad response"))
    env = build_env(agent=agent)

    sub_id = _spawn(env, prompt="compute", kind=SubsessionKind.TASK)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED


def test_is_unexpected_model_behavior() -> None:
    """Unit test for the `_is_unexpected_model_behavior` helper."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    from robotsix_chat.subsessions.worker import _is_unexpected_model_behavior

    # Direct match.
    assert _is_unexpected_model_behavior(UnexpectedModelBehavior("stream ended"))

    # Wrapped in another exception (cause chain).
    wrapper = RuntimeError("agent failed")
    wrapper.__cause__ = UnexpectedModelBehavior("empty response")
    assert _is_unexpected_model_behavior(wrapper)

    # Unrelated exception — should NOT match.
    assert not _is_unexpected_model_behavior(ValueError("not this"))

    # None in cause chain — should NOT match and should not loop.
    assert not _is_unexpected_model_behavior(RuntimeError("plain"))


# ---------------------------------------------------------------------------
# model-tier fallback (HTTP 404)
# ---------------------------------------------------------------------------


class _FakeModelTier404Error(Exception):
    """An exception that looks like a model-tier 404 to `_is_model_tier_not_found`."""

    def __init__(self, message: str = "model not found") -> None:
        super().__init__(message)
        self.status_code = 404


@pytest.mark.asyncio
async def test_periodic_model_tier_404_falls_back_to_lower_level() -> None:
    """A periodic subsession at level 2 that hits a 404 falls back to level 1."""
    error_agent = FakeAgent(error=_FakeModelTier404Error())
    fallback_agent = FakeAgent(["monitor result ok"])
    factory = CapturingAgentFactory(error_agent, fallback_agent)
    env = build_env(
        agent_factory=factory,
        settings=make_settings(llmio_api_key="test-key"),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        model_level=2,
        interval_seconds=0.02,
    )
    # The fallback worker skips the already-executed run 1 and goes to
    # sleep, so _await_worker will time out — that's expected.
    with pytest.raises(asyncio.TimeoutError):
        await _await_worker(env, sub_id, timeout=0.5)

    # Verify the factory was called twice: first at level 2, then at level 1.
    assert len(factory.captured) >= 2
    assert factory.captured[0]["model_level"] == 2
    assert factory.captured[1]["model_level"] == 1

    # The subsession should still be alive (not failed).
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status not in (SubsessionStatus.FAILED, SubsessionStatus.CLOSED)
    assert info.model_level == 1

    # The checkpoint should record the fallback.
    cp = info.checkpoint or {}
    assert cp.get("_tier_fallback_count") == 1
    assert cp.get("_fallback_model_level") == 1


@pytest.mark.asyncio
async def test_periodic_model_tier_404_fallback_exhausted_fails() -> None:
    """When all tier fallbacks are exhausted, the subsession fails."""
    error_agent = FakeAgent(error=_FakeModelTier404Error())
    factory = CapturingAgentFactory(error_agent)  # only one agent — all calls fail
    env = build_env(
        agent_factory=factory,
        settings=make_settings(llmio_api_key="test-key"),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        model_level=2,
        interval_seconds=0.02,
    )
    # The worker eventually fails after exhausting fallbacks.
    await _await_worker(env, sub_id, timeout=3.0)

    info = env.registry.get(sub_id)
    assert info is not None
    # After exhausting fallbacks (2→1→fail), the subsession should fail.
    assert info.status == SubsessionStatus.FAILED
    assert "model tier" in (info.error or "").lower()


@pytest.mark.asyncio
async def test_task_subsession_no_model_tier_fallback() -> None:
    """Task subsessions do NOT fall back on model-tier 404.

    They use the existing user_chat/task retry path instead.
    """
    env = build_env(
        agent=FakeAgent(error=_FakeModelTier404Error()),
        settings=make_settings(llmio_api_key="test-key"),
    )

    sub_id = _spawn(env, kind=SubsessionKind.TASK, model_level=2)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # Task subsessions should fail (they don't have model-tier fallback).
    assert info.status == SubsessionStatus.FAILED


@pytest.mark.asyncio
async def test_wait_for_event_model_tier_404_falls_back() -> None:
    """A wait_for_event subsession also gets model-tier fallback.

    The monitor is clamped from 3→2 by monitor_max_model_level,
    then the 404 fallback reduces it further from 2→1.
    """
    error_agent = FakeAgent(error=_FakeModelTier404Error())
    fallback_agent = FakeAgent(["NO_CHANGE"])
    factory = CapturingAgentFactory(error_agent, fallback_agent)
    env = build_env(
        agent_factory=factory,
        settings=make_settings(llmio_api_key="test-key"),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        model_level=3,
        dedup_key="ticket-123",
    )
    # The fallback worker skips the already-executed run 1 and enters
    # event-wait, so _await_worker will time out — that's expected.
    with pytest.raises(asyncio.TimeoutError):
        await _await_worker(env, sub_id, timeout=0.5)

    info = env.registry.get(sub_id)
    assert info is not None
    # Clamped 3→2 by monitor cap, then 404-fell-back 2→1.
    assert info.model_level == 1
    assert info.status not in (SubsessionStatus.FAILED, SubsessionStatus.CLOSED)


@pytest.mark.asyncio
async def test_periodic_model_tier_404_at_floor_no_fallback() -> None:
    """When already at the fallback floor (level 1), a 404 fails immediately."""
    env = build_env(
        agent=FakeAgent(error=_FakeModelTier404Error()),
        settings=make_settings(llmio_api_key="test-key"),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        model_level=1,
        interval_seconds=0.02,
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status == SubsessionStatus.FAILED


# consecutive error handling — acceptance criteria tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_failure_leaves_subsession_alive_periodic() -> None:
    """Tool failure exhausting retries in run k leaves subsession alive.

    Acceptance criteria: A simulated tool failure exhausting retries in
    run k leaves the subsession alive and run k+1 executes normally.
    """
    call_count = 0

    class FailThenSucceedAgent:
        """Fails on the first call, succeeds on subsequent calls."""

        def __init__(self) -> None:
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
            nonlocal call_count
            call_count += 1
            self.calls.append({"message": message, "session_id": session_id})
            if call_count == 1:
                raise ValueError("component_request exceeded max retries count of 2")
            yield "run completed normally"

    agent = FailThenSucceedAgent()
    env = build_env(
        agent=agent,
        settings=make_settings(
            consecutive_error_fail_threshold=3,
            transient_error_max_retries=0,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
            max_runs=2,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # The subsession completed normally after the error + recovery.
    assert info.status is SubsessionStatus.CLOSED
    assert info.runs >= 2
    assert info.consecutive_errored_runs == 0  # reset after success


@pytest.mark.asyncio
async def test_tool_failure_exhausting_retries_leaves_subsession_alive_wfe() -> None:
    """Same as periodic but for wait_for_event kind."""
    call_count = 0

    class FailThenSucceedAgent:
        def __init__(self) -> None:
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
            nonlocal call_count
            call_count += 1
            self.calls.append({"message": message, "session_id": session_id})
            if call_count == 1:
                raise ValueError("component_request exceeded max retries count of 2")
            yield "event processed"

    agent = FailThenSucceedAgent()
    env = build_env(
        agent=agent,
        settings=make_settings(
            consecutive_error_fail_threshold=3,
            transient_error_max_retries=0,
            # Short event-driven timeout so the test doesn't wait 60s.
            event_driven_timeout_seconds=0.1,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.WAIT_FOR_EVENT,
            dedup_key="ticket-456",
        )
        # First run fails, second succeeds.  Wait for both to complete.
        # The event wait loop has a safety-net timeout, so we need to wait
        # long enough for it to fire.
        await asyncio.sleep(2.0)

    info = env.registry.get(sub_id)
    assert info is not None
    # The subsession should still be alive (not FAILED).
    assert info.status is not SubsessionStatus.FAILED
    assert info.consecutive_errored_runs == 0  # reset after success
    assert info.runs >= 2


@pytest.mark.asyncio
async def test_three_consecutive_errored_runs_fails_subsession() -> None:
    """3 consecutive errored runs fail the subsession with last error."""
    agent = FakeAgent(error=ValueError("tool retry limit exceeded"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            consecutive_error_fail_threshold=3,
            transient_error_max_retries=0,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED
    assert info.consecutive_errored_runs >= 3
    assert "consecutive errored runs" in (info.error or "")
    assert "tool retry limit exceeded" in (info.error or "")


@pytest.mark.asyncio
async def test_single_errored_run_produces_at_most_one_notification() -> None:
    """A single errored run produces at most one parent notification.

    Acceptance criteria: A single errored run produces at most one
    parent notification (avoid spamming a notice per errored run).
    """
    event_sink = RecordingSink()
    agent = FakeAgent(error=ValueError("transient tool failure"))
    env = build_env(
        agent=agent,
        event_sink=event_sink,
        settings=make_settings(
            consecutive_error_fail_threshold=5,
            transient_error_max_retries=0,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        # Wait for several errored runs (more than 1).
        await asyncio.sleep(0.3)

    info = env.registry.get(sub_id)
    assert info is not None
    # At least 2 errored runs happened.
    assert info.consecutive_errored_runs >= 2

    # Only one parent notification (urgency=low, not the result frame).
    notification_frames = [
        (s, f)
        for s, f in event_sink.frames
        if f.get("type") == SSE_NOTIFICATION_TYPE and f.get("urgency") == "low"
    ]
    assert len(notification_frames) == 1
    assert "errored run" in str(notification_frames[0][1].get("body", ""))


@pytest.mark.asyncio
async def test_no_behavior_change_for_task_kind() -> None:
    """Task subsessions still fail immediately on error."""
    agent = FakeAgent(error=ValueError("task tool failure"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            consecutive_error_fail_threshold=3,
            transient_error_max_retries=0,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(env, prompt="do something", kind=SubsessionKind.TASK)
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # TASK subsession should be failed immediately (no consecutive error tracking).
    assert info.status is SubsessionStatus.FAILED
    assert info.consecutive_errored_runs == 0  # not tracked for task kind


@pytest.mark.asyncio
async def test_threshold_zero_fails_on_first_errored_run() -> None:
    """consecutive_error_fail_threshold=0 fails the subsession on the first errored run.

    The docstring says "Set to 0 to fail on the first errored run (legacy
    behaviour)".  This test guards that edge case.
    """
    agent = FakeAgent(error=ValueError("tool failure"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            consecutive_error_fail_threshold=0,
            transient_error_max_retries=0,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # With threshold=0, the very first errored run should fail the subsession.
    assert info.status is SubsessionStatus.FAILED
    assert info.consecutive_errored_runs >= 1
    assert "consecutive errored runs" in (info.error or "")


# wait_for_event checkpoint repair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_event_turn_repairs_checkpoint_ticket_id() -> None:
    """After a set_checkpoint call wipes ticket_id, the turn handler restores it.

    The agent may call set_checkpoint with only ``last_known_state``,
    replacing the spawn-time checkpoint and losing the ticket_id.
    ``_run_wait_for_event_turn`` must recover it from the dedup_key
    and persist it so the subsession survives a restart.
    """
    agent = FakeAgent(["NO_CHANGE"])
    env = build_env(agent=agent)

    sub_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket abc",
        prompt="monitor the ticket",
        model_level=3,
        dedup_key="abc-123",
        checkpoint={"ticket_id": "abc-123", "last_known_state": "open"},
    )
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.dedup_key == "abc-123"

    # Simulate a legacy checkpoint that lost its ticket_id before the
    # registry-level preservation guard existed.  New ``update_checkpoint``
    # writes no longer drop the key, but a persisted entry from before the
    # fix can still load without it — the turn handler must repair it.
    info = env.registry.get(sub_id)
    assert info is not None
    info.checkpoint = {"last_known_state": "in_progress"}

    # Run the post-turn handler — it should repair the checkpoint.
    result = await _run_wait_for_event_turn(env, info, sub_id, "NO_CHANGE", None, 0)
    assert result is not None  # not closed

    # After repair, ticket_id must be back in the checkpoint.
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.checkpoint is not None
    assert info.checkpoint.get("ticket_id") == "abc-123"
    assert info.checkpoint.get("last_known_state") == "in_progress"


@pytest.mark.asyncio
async def test_wait_for_event_turn_repair_noop_when_ticket_id_present() -> None:
    """When ticket_id is already in checkpoint, the turn handler leaves it."""
    agent = FakeAgent(["NO_CHANGE"])
    env = build_env(agent=agent)

    sub_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket xyz",
        prompt="monitor the ticket",
        model_level=3,
        dedup_key="xyz-456",
        checkpoint={"ticket_id": "xyz-456", "last_known_state": "open"},
    )
    info = env.registry.get(sub_id)
    assert info is not None

    result = await _run_wait_for_event_turn(env, info, sub_id, "NO_CHANGE", None, 0)
    assert result is not None

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.checkpoint is not None
    assert info.checkpoint.get("ticket_id") == "xyz-456"


@pytest.mark.asyncio
async def test_wait_for_event_checkpoint_survives_resume(
    tmp_path: Path,
) -> None:
    """A wait_for_event subsession with ticket_id in checkpoint resumes cleanly.

    Simulates a restart: the checkpoint is persisted, a fresh registry
    loads it, and resume_subsessions re-spawns the monitor.  The resumed
    subsession must carry the ticket_id forward.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    wfe = registry1.create(
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket def",
        prompt="monitor the ticket",
        model_level=3,
        checkpoint={"ticket_id": "def-789", "last_known_state": "open"},
        dedup_key="def-789",
        event_timeout_seconds=3600.0,
    )
    registry1.set_status(wfe.id, SubsessionStatus.SLEEPING, runs=1)

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["ok"]),
        registry=registry2,
    )
    resume_subsessions(env)

    resumed = registry2.get(wfe.id)
    assert resumed is not None
    assert resumed.status is SubsessionStatus.RUNNING
    assert resumed.checkpoint is not None
    assert resumed.checkpoint.get("ticket_id") == "def-789"
    assert resumed.checkpoint.get("last_known_state") == "open"
    assert resumed.dedup_key == "def-789"

    # Clean up the worker.
    worker = registry2._running.get(wfe.id)
    registry2.cancel_and_close(wfe.id, reason="teardown", closed_by="system")
    if worker is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


# ---------------------------------------------------------------------------
# on_close kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_close_parent_already_closed_runs_immediately() -> None:
    """When the parent session is already closed, ON_CLOSE runs right away."""
    agent = FakeAgent(["cleanup complete"])
    store = ConversationStore()
    store.is_session_closed = lambda session_id: True  # type: ignore[method-assign]
    env = build_env(agent=agent, store=store)

    sub_id = _spawn(env, kind=SubsessionKind.ON_CLOSE, prompt="do the cleanup")
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "completed"
    assert info.summary == "cleanup complete"
    assert len(agent.calls) == 1
    assert agent.calls[0]["message"] == "do the cleanup"


@pytest.mark.asyncio
async def test_on_close_waits_until_parent_closes() -> None:
    """ON_CLOSE polls until the parent closes, then runs the one-shot task."""
    agent = FakeAgent(["cleanup done"])
    parent_closed = [False]  # mutable closure

    store = ConversationStore()
    store.is_session_closed = lambda session_id: parent_closed[0]  # type: ignore[method-assign]
    env = build_env(agent=agent, store=store)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        sub_id = _spawn(env, kind=SubsessionKind.ON_CLOSE, prompt="cleanup")

        # The worker spins in the close-waiting loop (sleep mocked to
        # return immediately).  Give it a few iterations.
        await asyncio.sleep(0.05)
        assert len(agent.calls) == 0, "agent must not be called while parent is open"

        # Simulate parent closing.
        parent_closed[0] = True

        await _await_worker(env, sub_id)

    assert len(agent.calls) == 1
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "completed"
    assert info.summary == "cleanup done"


@pytest.mark.asyncio
async def test_on_close_external_close_during_wait_exits_cleanly() -> None:
    """Subsession externally closed while waiting — worker exits without running."""
    agent = FakeAgent(["should not run"])

    store = ConversationStore()
    store.is_session_closed = lambda session_id: False  # type: ignore[method-assign]
    env = build_env(agent=agent, store=store)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        sub_id = _spawn(env, kind=SubsessionKind.ON_CLOSE, prompt="cleanup")

        # Let the worker enter the close-waiting loop.
        await asyncio.sleep(0.05)

        # Externally close the subsession while it's waiting.
        env.registry.cancel_and_close(sub_id, reason="cancelled", closed_by="user")

        # The worker task is cancelled by cancel_and_close;
        # suppress CancelledError when awaiting it.
        with contextlib.suppress(asyncio.CancelledError):
            await _await_worker(env, sub_id)

    # The agent must never have been called.
    assert len(agent.calls) == 0

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED


@pytest.mark.asyncio
async def test_on_close_child_of_periodic_is_rejected() -> None:
    """Spawning an ON_CLOSE child under a PERIODIC parent raises an error."""
    env = build_env()
    # Register a periodic parent without spawning a worker (same pattern as
    # test_periodic_parent_cannot_spawn_periodic_or_on_close_child).
    parent = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="parent periodic",
        prompt="monitor",
        model_level=3,
        interval_seconds=10.0,
    )

    with pytest.raises(SubsessionPeriodicSpawnError, match="on_close"):
        _spawn(
            env,
            kind=SubsessionKind.ON_CLOSE,
            parent_id=parent.id,
            depth=2,
            title="cleanup",
            prompt="clean up on close",
        )


@pytest.mark.asyncio
async def test_on_close_retries_up_to_user_chat_max_retries() -> None:
    """ON_CLOSE uses ``user_chat_max_retries`` when the agent fails."""
    agent = FakeAgent(error=RuntimeError("kaboom"))
    store = ConversationStore()
    store.is_session_closed = lambda session_id: True  # type: ignore[method-assign]
    env = build_env(agent=agent, store=store)

    sub_id = _spawn(env, kind=SubsessionKind.ON_CLOSE, prompt="cleanup")
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED
    # user_chat_max_retries defaults to 3 in make_settings.
    assert info.retry_count == 3
    assert "[RuntimeError] kaboom" in (info.error or "")


# ---------------------------------------------------------------------------
# _event_wait_loop — dedup_key fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_wait_loop_dedup_fallback_checkpoint_none() -> None:
    """When checkpoint is None, the dedup_key is used as ticket_id and written back."""
    env = build_env()
    sub_id = "sub-wfe-none"

    info = SubsessionInfo(
        id=sub_id,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="event monitor",
        prompt="monitor ticket foo",
        model_level=3,
        status=SubsessionStatus.RUNNING,
        created_at=1000.0,
        last_activity_at=1000.0,
        dedup_key="ticket-abc-123",
        checkpoint=None,
        event_timeout_seconds=5.0,
    )
    env.registry._subs[sub_id] = info

    with (
        patch.object(
            env.registry, "wait_for_inbox", new_callable=AsyncMock
        ) as mock_wait,
        patch.object(
            env.registry, "update_checkpoint", wraps=env.registry.update_checkpoint
        ) as mock_update,
        patch.object(env.registry, "drain_inbox", return_value=[]) as _mock_drain,
    ):
        mock_wait.return_value = True

        result = await _event_wait_loop(
            env, info, sub_id, previous_result=None, consecutive_no_change=0
        )

    # The checkpoint should have been repaired via update_checkpoint.
    mock_update.assert_called_once_with(sub_id, {"ticket_id": "ticket-abc-123"})
    # The in-memory info should now carry the repaired checkpoint.
    assert info.checkpoint == {"ticket_id": "ticket-abc-123"}
    # The function should return a tuple (not None), meaning it didn't close.
    assert result is not None
    pending, prev, cn_change = result
    assert prev is None
    assert cn_change == 0


@pytest.mark.asyncio
async def test_event_wait_loop_dedup_fallback_no_ticket_id() -> None:
    """When checkpoint has no ticket_id key, dedup_key fills the gap."""
    env = build_env()
    sub_id = "sub-wfe-missing"

    info = SubsessionInfo(
        id=sub_id,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="event monitor",
        prompt="monitor ticket bar",
        model_level=3,
        status=SubsessionStatus.RUNNING,
        created_at=1000.0,
        last_activity_at=1000.0,
        dedup_key="ticket-xyz-789",
        checkpoint={"last_known_state": "open"},
        event_timeout_seconds=5.0,
    )
    env.registry._subs[sub_id] = info

    with (
        patch.object(
            env.registry, "wait_for_inbox", new_callable=AsyncMock
        ) as mock_wait,
        patch.object(
            env.registry, "update_checkpoint", wraps=env.registry.update_checkpoint
        ) as mock_update,
        patch.object(env.registry, "drain_inbox", return_value=[]) as _mock_drain,
    ):
        mock_wait.return_value = True

        result = await _event_wait_loop(
            env, info, sub_id, previous_result="last ok", consecutive_no_change=2
        )

    # update_checkpoint should merge ticket_id into the existing checkpoint.
    mock_update.assert_called_once_with(
        sub_id, {"last_known_state": "open", "ticket_id": "ticket-xyz-789"}
    )
    assert info.checkpoint == {
        "last_known_state": "open",
        "ticket_id": "ticket-xyz-789",
    }
    assert result is not None
    pending, prev, cn_change = result
    assert prev == "last ok"
    assert cn_change == 2


@pytest.mark.asyncio
async def test_event_wait_loop_closes_when_no_ticket_id_and_no_dedup_key() -> None:
    """When both checkpoint and dedup_key lack a ticket_id, the subsession is closed."""
    env = build_env()
    sub_id = "sub-wfe-noid"

    info = SubsessionInfo(
        id=sub_id,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="broken monitor",
        prompt="monitor ticket baz",
        model_level=3,
        status=SubsessionStatus.RUNNING,
        created_at=1000.0,
        last_activity_at=1000.0,
        dedup_key=None,
        checkpoint=None,
        event_timeout_seconds=5.0,
    )
    env.registry._subs[sub_id] = info

    result = await _event_wait_loop(
        env, info, sub_id, previous_result=None, consecutive_no_change=0
    )

    # The function returns None when the subsession is closed.
    assert result is None
    # The subsession should be marked closed.
    refreshed = env.registry.get(sub_id)
    assert refreshed is not None
    assert refreshed.status is SubsessionStatus.CLOSED
    assert refreshed.close_reason == "missing_ticket_id"
    assert "no recoverable ticket_id" in (refreshed.summary or "").lower()


# ---------------------------------------------------------------------------
# auto-drive promotable-draft branch
# ---------------------------------------------------------------------------


def test_draft_decision_comment_posted_helper() -> None:
    """The draft-decision guard fires only for a comment-posted, still-draft ticket."""
    from robotsix_chat.subsessions import SubsessionInfo, SubsessionKind

    def _info(checkpoint: dict[str, object] | None) -> SubsessionInfo:
        return SubsessionInfo(
            id="sub-guard",
            kind=SubsessionKind.PERIODIC,
            owner_session_id="sess-1",
            parent_id=None,
            depth=1,
            title="monitor",
            prompt="watch ticket",
            model_level=3,
            status=SubsessionStatus.RUNNING,
            created_at=0.0,
            last_activity_at=0.0,
            checkpoint=checkpoint,
        )

    # No checkpoint — not posted.
    assert not _draft_decision_comment_posted(_info(None))
    # Flag set but ticket left draft — not posted (state changed).
    assert not _draft_decision_comment_posted(
        _info(
            {
                "ticket_id": "T-1",
                "last_known_state": "ready",
                "auto_drive_comment_posted": True,
            }
        )
    )
    # Still draft but flag unset — comment not posted yet.
    assert not _draft_decision_comment_posted(
        _info({"ticket_id": "T-1", "last_known_state": "draft"})
    )
    # Draft + flag set — posted.
    assert _draft_decision_comment_posted(
        _info(
            {
                "ticket_id": "T-1",
                "last_known_state": "draft",
                "auto_drive_comment_posted": True,
            }
        )
    )


def test_build_periodic_input_promotable_draft_default_is_operator_decision() -> None:
    """Default (gate off): the prompt instructs the operator-decision comment.

    The gate defaults to False — the monitor must NEVER auto-promote a
    draft.  It must instead post exactly one [AUTO_DRIVE] operator-decision
    comment and then reply QUEUED.
    """
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-x",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch ticket TICKET-9",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        dedup_key="TICKET-9",
        checkpoint={"ticket_id": "TICKET-9", "last_known_state": "draft"},
    )

    result = _build_periodic_input(info, previous_result=None, steering=[])

    # Operator-decision branch present, auto-promote branch absent.
    assert "DRAFT TICKETS — OPERATOR-DECISION BRANCH" in result
    assert "AUTO-PROMOTE BRANCH" not in result
    # The classification and idempotency guarantees are spelled out.
    assert "PROMOTABLE DRAFT" in result
    assert "## Problem" in result
    assert "## Acceptance criteria" in result
    assert "[AUTO_DRIVE]" in result
    assert "auto_drive_comment_posted" in result
    assert "EXACTLY ONE" in result
    # The monitor must not burn runs after posting — it replies QUEUED.
    assert "QUEUED" in result
    # Non-promotable drafts are left untouched.
    assert "NOT promotable" in result


def test_build_periodic_input_promotable_draft_gate_on_pre_authorized_promotes() -> (
    None
):
    """Gate ON + pre-authorized ticket: the prompt instructs auto-promotion."""
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-x",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch ticket TICKET-9",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        dedup_key="TICKET-9",
        checkpoint={"ticket_id": "TICKET-9", "last_known_state": "draft"},
    )

    result = _build_periodic_input(
        info,
        previous_result=None,
        steering=[],
        pre_authorized_patterns=["TICKET-*"],
        auto_drive_promote_ready_drafts=True,
    )

    # Auto-promote branch present, operator-decision branch absent.
    assert "DRAFT TICKETS — AUTO-PROMOTE BRANCH" in result
    assert "OPERATOR-DECISION BRANCH" not in result
    assert "mark_ticket_ready" in result
    # The monitor still must not burn runs after the transition.
    assert "QUEUED" in result
    # Non-promotable drafts are left untouched.
    assert "NOT promotable" in result


def test_build_periodic_input_promotable_draft_gate_on_not_pre_authorized() -> None:
    """Gate ON but ticket NOT pre-authorized: still the comment branch.

    The auto-promote branch requires BOTH the opt-in gate and a matching
    pre_authorized_ticket_patterns entry — a gate without authorization
    must not promote.
    """
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-x",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch ticket OTHER-3",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        dedup_key="OTHER-3",
        checkpoint={"ticket_id": "OTHER-3", "last_known_state": "draft"},
    )

    result = _build_periodic_input(
        info,
        previous_result=None,
        steering=[],
        pre_authorized_patterns=["TICKET-*"],
        auto_drive_promote_ready_drafts=True,
    )

    assert "DRAFT TICKETS — OPERATOR-DECISION BRANCH" in result
    assert "AUTO-PROMOTE BRANCH" not in result
    assert "[AUTO_DRIVE]" in result


@pytest.mark.asyncio
async def test_periodic_draft_comment_posted_skips_agent_turns() -> None:
    """A comment-posted, unchanged draft consumes NO further runs.

    Once the monitor's checkpoint carries ``auto_drive_comment_posted``
    with ``last_known_state='draft'``, the worker must skip the agent
    turn and wait event-driven — the run budget (e.g. a 60-run cap) is
    never exhausted re-driving the unchanged draft.
    """
    agent = FakeAgent(["NO_CHANGE"] * 3)
    env = build_env(agent=agent, settings=make_settings())

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        title="draft monitor",
    )
    assert env.registry.update_checkpoint(
        sub_id,
        {
            "ticket_id": "TICKET-DRAFT",
            "last_known_state": "draft",
            "auto_drive_comment_posted": True,
        },
    )
    # Let the worker reach its first loop iteration.
    await asyncio.sleep(0.15)

    info = env.registry.get(sub_id)
    assert info is not None
    # No agent turn was run — the comment is already posted.
    assert agent.calls == []
    assert info.runs == 0
    # The worker is waiting (SLEEPING), not closed, not failed.
    assert info.status is SubsessionStatus.SLEEPING
    assert info.close_reason is None

    # Cancel the waiting worker to end the test cleanly.
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    await asyncio.sleep(0.05)

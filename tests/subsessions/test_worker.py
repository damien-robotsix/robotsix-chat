"""Tests for the subsession worker: spawn validation and the turn loop."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from robotsix_chat.subsessions import (
    SubsessionDepthError,
    SubsessionKind,
    SubsessionLevelError,
    SubsessionPeriodicSpawnError,
    SubsessionStatus,
)
from robotsix_chat.subsessions import turn_runner as turn_runner_mod
from robotsix_chat.subsessions.worker import (
    CloseState,
    SubsessionContext,
)
from tests.common.subsession_fakes import (
    CapturingAgentFactory,
    FakeAgent,
    build_env,
    make_settings,
    wait_until,
)
from tests.subsessions.conftest import OWNER, _await_worker, _spawn

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

    # A task turn is operator-facing work, not machine boilerplate, so it
    # keeps full automatic recall.
    assert agent.calls[0]["skip_recall"] is False


@pytest.mark.asyncio
async def test_periodic_monitor_wake_skips_recall() -> None:
    """A periodic monitor wake suppresses automatic long-term recall.

    Monitor turn inputs are fixed, machine-generated instruction
    boilerplate; recalling on them churns the memory component for
    near-random matches on every wake. Monitors are self-contained by
    design, so the worker passes ``skip_recall=True``.
    """
    agent = FakeAgent(["report 1"])
    env = build_env(agent=agent)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="watch",
    )
    await _await_worker(env, sub_id)

    assert len(agent.calls) >= 1
    assert agent.calls[0]["skip_recall"] is True


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


def test_user_chat_first_turn_note_carries_suggestion_contract() -> None:
    """The user_chat first-turn note carries the ```suggestions contract.

    It tells the agent to emit the block for discrete multiple-choice
    decisions, so subsession chips appear.
    """
    from robotsix_chat.subsessions.worker import _USER_CHAT_FIRST_TURN_NOTE

    assert "```suggestions" in _USER_CHAT_FIRST_TURN_NOTE
    assert "option per line" in _USER_CHAT_FIRST_TURN_NOTE.lower()


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


def test_spawn_depth_error_beyond_max_depth() -> None:
    """Spawning deeper than ``max_depth`` raises ``SubsessionDepthError``."""
    env = build_env(settings=make_settings(max_depth=2))

    with pytest.raises(SubsessionDepthError):
        _spawn(env, depth=3)

    assert env.registry.list_for_owner(OWNER) == []


def test_spawn_level_errors() -> None:
    """An out-of-range level raises; a missing key never blocks a spawn.

    Every level is served by the keyless default slot.
    """
    env = build_env(settings=make_settings(openrouter_api_key=""))

    with pytest.raises(SubsessionLevelError):
        _spawn(env, model_level=6)
    with pytest.raises(SubsessionLevelError):
        _spawn(env, model_level=4)

    assert env.registry.list_for_owner(OWNER) == []


@pytest.mark.asyncio
async def test_spawn_monitor_model_level_clamped() -> None:
    """Periodic/wait_for_event monitors are clamped to monitor_max_model_level."""
    env = build_env(
        settings=make_settings(
            monitor_max_model_level=2,
            default_model_level=3,
            openrouter_api_key="test-key",
        )
    )

    # Periodic at model_level=3 gets clamped to 2.
    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        model_level=3,
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

    # Task at model_level=3 is NOT clamped (task is uncapped).
    sub_id3 = _spawn(
        env,
        kind=SubsessionKind.TASK,
        model_level=3,
    )
    info3 = env.registry.get(sub_id3)
    assert info3 is not None
    assert info3.model_level == 3

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
            "robotsix_chat.subsessions.turn_runner.is_openrouter_transient",
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
        "robotsix_chat.subsessions.turn_runner.is_openrouter_transient",
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
        "robotsix_chat.subsessions.turn_runner.is_openrouter_transient",
        return_value=True,
    ):
        sub_id = _spawn(env, prompt="compute", kind=SubsessionKind.TASK)
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # TASK subsession should be FAILED — no retry for non-periodic.
    assert info.status is SubsessionStatus.FAILED


@pytest.mark.asyncio
async def test_task_run_timeout_fails_without_retry(monkeypatch) -> None:
    """A task turn that exceeds its per-run timeout fails fast — no retry.

    Regression for the 2026-09-04 incident: three 10-minute retries of the
    same timed-out Hexarchy batch.  A turn that hit the hard timeout will
    hit it again with the identical input, so it must fail immediately
    rather than burn the retry budget on a doomed turn.
    """
    # Collapse the grace window so the tiny test timeout fires instantly.
    monkeypatch.setattr(turn_runner_mod, "_RUN_TIMEOUT_GRACE", 0.0)
    # The agent blocks forever on the gate, so the turn can only end by
    # hitting the per-subsession run timeout.
    gate = asyncio.Event()
    agent = FakeAgent(gate=gate)
    env = build_env(agent=agent)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.TASK,
        prompt="compute",
        run_timeout_seconds=0.05,
    )
    await _await_worker(env, sub_id, timeout=2.0)

    info = env.registry.get(sub_id)
    assert info is not None
    # Failed, and exactly one agent call — the timeout was never retried.
    assert info.status is SubsessionStatus.FAILED
    assert info.retry_count == 0
    assert len(agent.calls) == 1
    # The per-subsession value (0.05s) drove the timeout, not the global
    # 600 s default — proving the override is read by _run_turn_with_timeout.
    assert info.run_timeout_seconds == 0.05


@pytest.mark.asyncio
async def test_spawn_run_timeout_override_and_cap() -> None:
    """Per-subsession run_timeout_seconds is plumbed and capped at the max."""
    env = build_env(agent=FakeAgent(["done"]))
    env.settings.subsessions.max_run_timeout_seconds = 3600.0

    # Within the cap → honored as-is.
    sub_a = _spawn(env, kind=SubsessionKind.TASK, run_timeout_seconds=1800.0)
    info_a = env.registry.get(sub_a)
    assert info_a is not None
    assert info_a.run_timeout_seconds == 1800.0

    # Above the cap → clamped to max_run_timeout_seconds.
    sub_b = _spawn(env, kind=SubsessionKind.TASK, run_timeout_seconds=7200.0)
    info_b = env.registry.get(sub_b)
    assert info_b is not None
    assert info_b.run_timeout_seconds == 3600.0

    # Absent → None (falls back to the global default at run time).
    sub_c = _spawn(env, kind=SubsessionKind.TASK)
    info_c = env.registry.get(sub_c)
    assert info_c is not None
    assert info_c.run_timeout_seconds is None

    for sid in (sub_a, sub_b, sub_c):
        task = env.registry._running.get(sid)
        if task is not None and not task.done():
            task.cancel()
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Monitor turn concurrency gate (2026-09-02 OOM: unbounded monitor-turn
# stampede — each turn spawns a ~240MB claudeSDK subprocess)
# ---------------------------------------------------------------------------


def _gate_env(concurrency: int) -> SimpleNamespace:
    from tests.common.subsession_fakes import make_settings

    return SimpleNamespace(settings=make_settings(monitor_turn_concurrency=concurrency))


def _gate_info(kind: SubsessionKind) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, runs=0)


@pytest.mark.asyncio
async def test_monitor_turns_serialize_through_the_gate(monkeypatch):
    """With concurrency=1, two monitor turns never overlap."""
    turn_runner_mod._monitor_turn_gate = None
    turn_runner_mod._monitor_turn_gate_size = -1
    running = 0
    max_running = 0

    async def fake_turn(env, agent, turn_input, history, sub_id, info):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.02)
        running -= 1
        return "ok"

    monkeypatch.setattr(turn_runner_mod, "_run_turn_with_timeout", fake_turn)
    env = _gate_env(1)
    results = await asyncio.gather(
        *(
            turn_runner_mod._run_turn_with_transient_retry(
                env, None, "in", [], f"sub{i}", _gate_info(SubsessionKind.PERIODIC)
            )
            for i in range(3)
        )
    )
    assert results == ["ok", "ok", "ok"]
    assert max_running == 1


@pytest.mark.asyncio
async def test_gate_disabled_and_non_monitor_kinds_bypass(monkeypatch):
    """concurrency=0 disables the gate; TASK turns never wait on it."""
    turn_runner_mod._monitor_turn_gate = None
    turn_runner_mod._monitor_turn_gate_size = -1
    running = 0
    max_running = 0

    async def fake_turn(env, agent, turn_input, history, sub_id, info):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.02)
        running -= 1
        return "ok"

    monkeypatch.setattr(turn_runner_mod, "_run_turn_with_timeout", fake_turn)
    await asyncio.gather(
        turn_runner_mod._run_turn_with_transient_retry(
            _gate_env(0), None, "in", [], "s1", _gate_info(SubsessionKind.PERIODIC)
        ),
        turn_runner_mod._run_turn_with_transient_retry(
            _gate_env(1), None, "in", [], "s2", _gate_info(SubsessionKind.TASK)
        ),
        turn_runner_mod._run_turn_with_transient_retry(
            _gate_env(1), None, "in", [], "s3", _gate_info(SubsessionKind.TASK)
        ),
    )
    assert max_running >= 2

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
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE, SSE_SUBSESSION_RESULT_TYPE
from robotsix_chat.subsessions import (
    SubsessionCapacityError,
    SubsessionDepthError,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionLevelError,
    SubsessionPeriodicSpawnError,
    SubsessionRegistry,
    SubsessionStatus,
    resume_subsessions,
    spawn_subsession,
)
from robotsix_chat.subsessions.worker import (
    CloseState,
    SubsessionContext,
    SubsessionEnv,
    _build_periodic_input,
    _format_worker_error,
    _is_duplicate_reply,
    _is_no_change,
    _is_queued,
    _run_wait_for_event_turn,
    _truncate,
)
from robotsix_chat.subsessions.worker_mill import (
    _check_resume_status,
    _get_mill_started_at,
    _handle_mill_unreachable,
    _reset_mill_failure_counter,
)
from tests.common.subsession_fakes import (
    CapturingAgentFactory,
    FakeAgent,
    FakeClock,
    RecordingSink,
    build_env,
    make_settings,
    wait_until,
)

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
    env = build_env(agent=agent, event_sink=sink)

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
        "Auto-paused after 3 consecutive no-change runs. "
        "To resume monitoring, send a message to this subsession "
        "via message_subsession — the monitor will wake and re-check "
        "the ticket state on its next run."
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
async def test_periodic_human_approval_timeout_auto_escalates() -> None:
    """human_issue_approval checkpoint triggers human_approval_timeout close."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=5,
            human_approval_timeout_runs=3,
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
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "human_approval_timeout"
    assert "human_issue_approval" in (info.summary or "")
    assert len(agent.calls) == 3


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
    """human_approval_timeout_runs is independent of auto_stop_no_change_runs."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=10,
            human_approval_timeout_runs=2,
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
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "human_approval_timeout"
    assert len(agent.calls) == 2


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
async def test_periodic_pre_authorized_no_match_uses_timeout() -> None:
    """Non-matching ticket still uses the normal human_approval_timeout."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=10,
            human_approval_timeout_runs=3,
            pre_authorized_ticket_patterns=["OTHER-*"],
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
    assert info.close_reason == "human_approval_timeout"
    assert len(agent.calls) == 3


@pytest.mark.asyncio
async def test_periodic_pre_authorized_empty_patterns_uses_timeout() -> None:
    """Empty patterns list falls through to normal human_approval_timeout."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=10,
            human_approval_timeout_runs=2,
            pre_authorized_ticket_patterns=[],
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
    assert info.close_reason == "human_approval_timeout"
    assert len(agent.calls) == 2


@pytest.mark.asyncio
async def test_periodic_human_approval_timeout_by_wall_clock() -> None:
    """Wall-clock timeout escalates even when the agent never emits NO_CHANGE.

    The run-count gate requires consecutive NO_CHANGE replies, but the
    system prompt tells the agent to call complete_subsession instead
    when stuck — the wall-clock backstop catches the case where the
    agent follows the prompt (producing non-NO_CHANGE output each run)
    but never actually calls complete_subsession to close the ticket.
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

    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "human_approval_timeout"
    # Escalated by wall-clock timeout, not by run count — the agent
    # never said NO_CHANGE, so the run-count gate (capped at 999) was
    # never reached.
    assert "wall-clock timeout" in (info.summary or "")


@pytest.mark.asyncio
async def test_periodic_steering_message_wakes_the_sleep_early() -> None:
    """A queued message interrupts the inter-run sleep and feeds the run."""
    agent = FakeAgent(["baseline", "focused report"])
    env = build_env(agent=agent)

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
    env = build_env(agent=agent)

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


# ============================================================================
# resume status check (_check_resume_status, _handle_mill_unreachable,
# _reset_mill_failure_counter)
# ============================================================================

# _MAX_MILL_FAILURES = 2 in the worker module (private constant).


# -- helpers ----------------------------------------------------------------


def _make_checkpoint_info(env, **checkpoint_kwargs):
    """Register a periodic subsession with a checkpoint and return info."""
    sub_id = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="ticket monitor",
        prompt="monitor TICKET-1",
        model_level=3,
        interval_seconds=60.0,
        checkpoint=checkpoint_kwargs or None,
    ).id
    return env.registry.get(sub_id)


def _env_with_board(board_url="https://mill.example.com"):
    """Build an env with ``board_api_base_url`` configured.

    The resume status check actually makes HTTP calls instead of
    short-circuiting on a missing/empty URL.
    """
    settings = make_settings()
    settings.direct_repo = type("_ns", (), {"board_api_base_url": board_url})()
    return build_env(settings=settings)


def _mock_async_client(response_json=None, side_effect=None):
    """Build a mock ``httpx.AsyncClient`` that returns a controlled response.

    Returns a MagicMock suitable for ``patch("httpx.AsyncClient", ...)``.
    The mock client is an async context manager whose ``__aenter__``
    returns a mock with ``.get`` returning either *response_json* (via a
    mock response) or raising *side_effect*.
    """
    # Use MagicMock (NOT AsyncMock) for the response — raise_for_status()
    # and json() are sync methods on httpx.Response.
    mock_response = MagicMock()
    mock_response.json.return_value = response_json or {}
    mock_response.raise_for_status.return_value = None

    # mock_client holds the async get method.
    mock_client = MagicMock()
    get_mock = AsyncMock()
    if side_effect is not None:
        get_mock.side_effect = side_effect
    else:
        get_mock.return_value = mock_response
    mock_client.get = get_mock

    # mock_instance is the async context manager (returned by AsyncClient()).
    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=mock_instance)


def _make_response(json_body):
    """Build a MagicMock httpx.Response with the given JSON body."""
    resp = MagicMock()
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


def _mock_async_client_dual(*, ticket_json=None, health_json=None):
    """Build a mock AsyncClient dispatching on URL path.

    ``mock_client.get(url)`` inspects the URL path and returns:
    - *ticket_json* for URLs containing ``/tickets/``
    - *health_json* for URLs containing ``/health``
    - An empty dict otherwise.
    """

    async def _dispatch(url, **kwargs):
        url_str = str(url)
        if "/health" in url_str:
            return _make_response(health_json or {})
        if "/tickets/" in url_str:
            return _make_response(ticket_json or {})
        return _make_response({})

    mock_client = MagicMock()
    mock_client.get = _dispatch

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=mock_instance)


# -- no-checkpoint / no-ticket-id / no-board-url paths -----------------------


@pytest.mark.asyncio
async def test_check_resume_status_no_checkpoint_continues():
    """When info.checkpoint is None, return (True, None) — normal resume."""
    env = build_env()
    info = _make_checkpoint_info(env)  # no checkpoint
    info.checkpoint = None

    should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is None


@pytest.mark.asyncio
async def test_check_resume_status_no_ticket_id_continues():
    """Checkpoint without 'ticket_id' key → continue."""
    env = build_env()
    info = _make_checkpoint_info(env, other_field="value")

    should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is None


@pytest.mark.asyncio
async def test_check_resume_status_no_board_url_continues():
    """When board_api_base_url is not configured, skip the check."""
    settings = make_settings()
    settings.direct_repo = type("_ns", (), {"board_api_base_url": ""})()
    env = build_env(settings=settings)
    info = _make_checkpoint_info(env, ticket_id="TICKET-1")

    should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is None


# -- terminal / blocked / open state branches --------------------------------


@pytest.mark.asyncio
async def test_check_resume_status_terminal_closes_and_delivers():
    """A ticket in a terminal state closes the subsession and delivers summary."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(
        response_json={
            "state": "closed",
            "pr_url": "https://github.com/owner/repo/pull/42",
        }
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert context_msg is not None
    assert "terminal" in context_msg
    assert "TICKET-1" in context_msg

    # Delivery is fire-and-forget — let the background task run.
    await asyncio.sleep(0)

    # Registry is now closed.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "ticket_terminal_on_resume"

    # Summary was delivered to the conversation store.
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    label, reply = history[0]
    assert "ticket_terminal" in label
    assert "TICKET-1" in reply


@pytest.mark.asyncio
async def test_check_resume_status_blocked_injects_context():
    """A blocked ticket returns (True, context_message)."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(response_json={"state": "blocked"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg
    assert "TICKET-1" in context_msg


# -- stale worker detection on blocked resume ---------------------------------


@pytest.mark.asyncio
async def test_check_resume_status_blocked_stale_worker_first_attempt():
    """First stale-worker resume: injects strong warning context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        worker_started_at="2024-01-01T00:00:00Z",
    )

    mock = _mock_async_client_dual(
        ticket_json={"state": "blocked"},
        health_json={"status": "alive", "started_at": "2024-01-01T00:00:00Z"},
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg
    assert "NOT been redeployed" in context_msg
    assert "1/2" in context_msg
    assert "TICKET-1" in context_msg

    # Checkpoint should have been updated with stale_worker_resume_count.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("stale_worker_resume_count") == 1


@pytest.mark.asyncio
async def test_check_resume_status_blocked_stale_worker_at_cap_closes():
    """Second stale-worker resume: closes the subsession."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        worker_started_at="2024-01-01T00:00:00Z",
        stale_worker_resume_count=1,
    )

    mock = _mock_async_client_dual(
        ticket_json={"state": "blocked"},
        health_json={"status": "alive", "started_at": "2024-01-01T00:00:00Z"},
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert context_msg is not None
    assert "not been redeployed" in context_msg
    assert "TICKET-1" in context_msg

    await asyncio.sleep(0)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "stale_worker"


@pytest.mark.asyncio
async def test_check_resume_status_blocked_worker_redeployed_resets_counter():
    """Worker redeployed (different started_at): resets counter, normal context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        worker_started_at="2024-01-01T00:00:00Z",
        stale_worker_resume_count=1,
    )

    mock = _mock_async_client_dual(
        ticket_json={"state": "blocked"},
        health_json={"status": "alive", "started_at": "2024-06-15T12:00:00Z"},
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg
    # Should NOT contain the stale-worker warning.
    assert "NOT been redeployed" not in context_msg

    # Checkpoint should have new started_at and NO stale counter.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("worker_started_at") == "2024-06-15T12:00:00Z"
    assert "stale_worker_resume_count" not in updated.checkpoint


@pytest.mark.asyncio
async def test_check_resume_status_blocked_health_probe_fails_graceful():
    """When the health probe fails, proceed with normal blocked context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    # Health endpoint returns 503; ticket endpoint returns blocked.
    async def _dispatch(url, **kwargs):
        url_str = str(url)
        if "/health" in url_str:
            resp = MagicMock()
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "boom", request=MagicMock(), response=MagicMock(status_code=503)
            )
            return resp
        return _make_response({"state": "blocked"})

    mock_client = MagicMock()
    mock_client.get = _dispatch
    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)
    mock = MagicMock(return_value=mock_instance)

    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg
    # Should be the normal context, not the stale-worker variant.
    assert "NOT been redeployed" not in context_msg


@pytest.mark.asyncio
async def test_check_resume_status_blocked_no_previous_started_at_stores_it():
    """First resume with no stored worker_started_at: stores it, normal context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        # No worker_started_at key.
    )

    mock = _mock_async_client_dual(
        ticket_json={"state": "blocked"},
        health_json={"status": "alive", "started_at": "2024-01-01T00:00:00Z"},
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg
    assert "NOT been redeployed" not in context_msg

    # worker_started_at should be stored for next time.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("worker_started_at") == "2024-01-01T00:00:00Z"


# -- blocked-resume threshold detection --------------------------------------


@pytest.mark.asyncio
async def test_check_resume_status_blocked_increments_blocked_resume_count():
    """First blocked resume increments blocked_resume_count and returns context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(response_json={"state": "blocked"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg

    # Counter should be 1 after first blocked resume.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("blocked_resume_count") == 1


@pytest.mark.asyncio
async def test_check_resume_status_blocked_second_attempt_adds_warning():
    """Second blocked resume adds a repeated-block warning to the context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        blocked_resume_count=1,
    )

    mock = _mock_async_client(response_json={"state": "blocked"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "Repeated block" in context_msg
    assert "2/3" in context_msg
    assert "1 remaining" in context_msg

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("blocked_resume_count") == 2


@pytest.mark.asyncio
async def test_check_resume_status_blocked_at_cap_closes():
    """Third consecutive blocked resume closes the subsession."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        blocked_resume_count=2,
    )

    mock = _mock_async_client(response_json={"state": "blocked"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert context_msg is not None
    assert "3 consecutive" in context_msg
    assert "TICKET-1" in context_msg

    await asyncio.sleep(0)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "repeated_blocked"


@pytest.mark.asyncio
async def test_check_resume_status_blocked_resets_counter_on_non_blocked():
    """When ticket transitions to a non-blocked state, the counter resets."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="blocked",
        blocked_resume_count=2,
    )

    mock = _mock_async_client(response_json={"state": "open"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "Continue monitoring" in context_msg

    # Counter should be reset to 0.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("blocked_resume_count") == 0


@pytest.mark.asyncio
async def test_check_resume_status_blocked_stale_and_blocked_caps_independent():
    """Stale-worker cap closes independently of blocked-resume cap.

    When the stale-worker cap fires first (at 2), the blocked-resume
    counter is still tracked but the stale-worker close takes precedence.
    """
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        worker_started_at="2024-01-01T00:00:00Z",
        stale_worker_resume_count=1,
        blocked_resume_count=1,
    )

    mock = _mock_async_client_dual(
        ticket_json={"state": "blocked"},
        health_json={"status": "alive", "started_at": "2024-01-01T00:00:00Z"},
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    # Stale-worker cap (2) fires before blocked-resume cap (3).
    assert should_continue is False
    assert context_msg is not None
    assert "not been redeployed" in context_msg

    await asyncio.sleep(0)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "stale_worker"


# -- _get_mill_started_at ----------------------------------------------------


@pytest.mark.asyncio
async def test_get_mill_started_at_returns_timestamp():
    """When health returns started_at, it is returned as a string."""
    mock = _mock_async_client(
        response_json={"status": "alive", "started_at": "2024-06-15T12:00:00Z"}
    )
    with patch("httpx.AsyncClient", mock):
        result = await _get_mill_started_at("https://mill.example.com")
    assert result == "2024-06-15T12:00:00Z"


@pytest.mark.asyncio
async def test_get_mill_started_at_missing_key_returns_none():
    """When health response lacks started_at, returns None."""
    mock = _mock_async_client(response_json={"status": "alive"})
    with patch("httpx.AsyncClient", mock):
        result = await _get_mill_started_at("https://mill.example.com")
    assert result is None


@pytest.mark.asyncio
async def test_get_mill_started_at_http_error_returns_none():
    """When health endpoint errors, returns None."""
    mock = _mock_async_client(
        side_effect=httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=MagicMock(status_code=500)
        )
    )
    with patch("httpx.AsyncClient", mock):
        result = await _get_mill_started_at("https://mill.example.com")
    assert result is None


@pytest.mark.asyncio
async def test_get_mill_started_at_connect_error_returns_none():
    """When health endpoint is unreachable, returns None."""
    mock = _mock_async_client(side_effect=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient", mock):
        result = await _get_mill_started_at("https://mill.example.com")
    assert result is None


@pytest.mark.asyncio
async def test_check_resume_status_human_issue_approval_injects_context():
    """human_issue_approval ticket injects context and updates checkpoint."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(response_json={"state": "human_issue_approval"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "HUMAN_ISSUE_APPROVAL" in context_msg
    assert "TICKET-1" in context_msg

    # Checkpoint was updated with the current state.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("last_known_state") == "human_issue_approval"


@pytest.mark.asyncio
async def test_check_resume_status_pre_authorized_escalates_immediately():
    """Pre-authorized ticket in human_issue_approval escalates on resume."""
    env = _env_with_board()
    # Set pre_authorized_ticket_patterns on the subsession settings.
    env.settings.subsessions.pre_authorized_ticket_patterns = ["TICKET-*"]
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(response_json={"state": "human_issue_approval"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert context_msg is not None
    assert "pre-authorized" in (context_msg or "").lower()
    assert "TICKET-1" in (context_msg or "")

    # Subsessions should be closed.
    closed_info = env.registry.get(info.id)
    assert closed_info is not None
    assert closed_info.status is SubsessionStatus.CLOSED
    assert closed_info.close_reason == "pre_authorized_approval"


@pytest.mark.asyncio
async def test_check_resume_status_pre_authorized_no_match_injects_context():
    """Non-matching pre-authorized pattern falls through to normal context injection."""
    env = _env_with_board()
    env.settings.subsessions.pre_authorized_ticket_patterns = ["OTHER-*"]
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(response_json={"state": "human_issue_approval"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "HUMAN_ISSUE_APPROVAL" in context_msg


@pytest.mark.asyncio
async def test_check_resume_status_open_injects_context():
    """An open/in_progress/pending ticket continues with a context note."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="in_progress",
    )

    mock = _mock_async_client(response_json={"state": "open"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "Continue monitoring" in context_msg
    assert "TICKET-1" in context_msg


# -- HTTP error handling -----------------------------------------------------


@pytest.mark.asyncio
async def test_check_resume_status_http_404_closes_immediately():
    """A 404 response closes the subsession immediately (not counted as unreachable)."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    error_response = AsyncMock()
    error_response.status_code = 404
    http_error = httpx.HTTPStatusError(
        "not found", request=AsyncMock(), response=error_response
    )

    mock = _mock_async_client(side_effect=http_error)
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert "deleted" in (context_msg or "")
    # Check that checkpoint was NOT updated with a failure counter (404 is not
    # counted as unreachable).
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "ticket_unreachable"

    # Delivery is fire-and-forget — let the background task run.
    await asyncio.sleep(0)

    # Summary was delivered.
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    assert "deleted" in history[0][1]


@pytest.mark.asyncio
async def test_check_resume_status_http_401_closes_immediately():
    """A 401/403 closes immediately with an auth-error message."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    error_response = AsyncMock()
    error_response.status_code = 401
    http_error = httpx.HTTPStatusError(
        "unauthorized", request=AsyncMock(), response=error_response
    )

    mock = _mock_async_client(side_effect=http_error)
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert "Authentication error" in (context_msg or "")

    # Delivery is fire-and-forget — let the background task run.
    await asyncio.sleep(0)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED

    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    assert "Authentication" in history[0][1]


@pytest.mark.asyncio
async def test_check_resume_status_http_5xx_counts_as_unreachable():
    """A 5xx response is treated as transient — increments the failure counter."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    error_response = AsyncMock()
    error_response.status_code = 503
    http_error = httpx.HTTPStatusError(
        "server error", request=AsyncMock(), response=error_response
    )

    mock = _mock_async_client(side_effect=http_error)
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    # Should still continue (first failure, below cap).
    assert should_continue is True
    assert context_msg is None

    # Checkpoint was updated with failure counter = 1.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("consecutive_mill_failures") == 1


# -- network errors ----------------------------------------------------------


@pytest.mark.asyncio
async def test_check_resume_status_connect_error_counts_as_unreachable():
    """A ConnectError is treated as transient (same as 5xx)."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(side_effect=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is None
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("consecutive_mill_failures") == 1


# -- _handle_mill_unreachable unit tests -------------------------------------


@pytest.mark.asyncio
async def test_handle_mill_unreachable_increments_counter():
    """Each call increments consecutive_mill_failures by 1."""
    env = build_env()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=0,
    )

    should_continue = await _handle_mill_unreachable(env, info, info.id)

    assert should_continue is True
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("consecutive_mill_failures") == 1


@pytest.mark.asyncio
async def test_handle_mill_unreachable_cap_enters_recovery():
    """At the cap the subsession enters recovery instead of closing.

    With ``consecutive_mill_failures`` already at (cap - 1), the next
    call reaches the cap, sleeps with backoff, probes health, and
    returns ``True`` (continue) when the health probe fails — it does
    NOT close the subsession.
    """
    env = build_env()
    # _MAX_MILL_FAILURES is 2, so one below the cap is 1.
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=1,
    )

    # Patch asyncio.sleep so the recovery backoff is instant.
    with patch("robotsix_chat.subsessions.worker.asyncio.sleep", new=AsyncMock()):
        should_continue = await _handle_mill_unreachable(env, info, info.id)

    assert should_continue is True
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.SLEEPING  # not CLOSED
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("consecutive_mill_failures") == 2


@pytest.mark.asyncio
async def test_handle_mill_unreachable_recovery_success_resets_counter():
    """When the health probe succeeds after recovery sleep, reset counter.

    The subsession returns to normal (counter cleared, continues).
    """
    env = _env_with_board("https://mill.example.com")
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=1,
    )

    # Patch asyncio.sleep and the health probe to simulate mill recovery.
    with (
        patch("robotsix_chat.subsessions.worker.asyncio.sleep", new=AsyncMock()),
        patch(
            "robotsix_chat.subsessions.worker_mill._get_mill_started_at",
            new=AsyncMock(return_value="2025-01-01T00:00:00Z"),
        ),
    ):
        should_continue = await _handle_mill_unreachable(env, info, info.id)

    assert should_continue is True
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.SLEEPING  # set during sleep
    assert updated.checkpoint is not None
    # Counter was reset on successful health probe.
    assert updated.checkpoint.get("consecutive_mill_failures") == 0


@pytest.mark.asyncio
async def test_handle_mill_unreachable_recovery_exhausted_closes():
    """After mill_recovery_max_retries retries the subsession is closed.

    Default max retries is 10, so with the cap at 2, closing happens
    at failure count = 2 + 10 = 12.
    """
    env = build_env()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=11,  # cap(2) + 9 retries = one below close
    )

    should_continue = await _handle_mill_unreachable(env, info, info.id)

    assert should_continue is False
    # Let the fire-and-forget delivery background task run.
    await asyncio.sleep(0)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "mill_unreachable"
    assert updated.summary is not None
    assert "Mill unreachable" in updated.summary

    # Summary was delivered to the conversation store.
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    label, reply = history[0]
    assert "mill_unreachable" in label
    assert "Mill unreachable" in reply


# -- _reset_mill_failure_counter ---------------------------------------------


@pytest.mark.asyncio
async def test_reset_mill_failure_counter_clears_on_success():
    """After a successful mill query the failure counter is reset to 0."""
    env = build_env()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=1,
    )

    _reset_mill_failure_counter(env, info, info.id)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("consecutive_mill_failures") == 0


@pytest.mark.asyncio
async def test_reset_mill_failure_counter_noop_when_already_zero():
    """Calling reset when counter is already 0 is harmless (no error)."""
    env = build_env()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=0,
    )

    # Should not raise.
    _reset_mill_failure_counter(env, info, info.id)

    updated = env.registry.get(info.id)
    assert updated is not None
    # Counter stays 0 (or is absent from checkpoint if already 0/absent).
    ck = updated.checkpoint or {}
    assert ck.get("consecutive_mill_failures", 0) == 0


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
            transient_error_backoff_base=0.0,
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
            transient_error_backoff_base=0.0,
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
async def test_periodic_non_transient_error_not_retried() -> None:
    """Periodic subsessions do NOT retry non-transient errors — they fail."""
    agent = FakeAgent(error=ValueError("not transient"))
    env = build_env(agent=agent)

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
    # Non-transient errors should still fail the subsession.
    assert info.status is SubsessionStatus.FAILED


@pytest.mark.asyncio
async def test_periodic_transient_error_transcript_recorded() -> None:
    """When transient retries are exhausted, a transcript entry is recorded."""
    agent = FakeAgent(error=_FakeTransientError("upstream hiccup"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            transient_error_max_retries=0,
            transient_error_backoff_base=0.0,
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
    # Check transcript has a system message about the skipped run.
    system_entries = [e for e in info.transcript if e.role == "system"]
    assert any("TRANSIENT_ERROR" in (e.text or "") for e in system_entries) or any(
        "transient" in (e.text or "").lower() for e in system_entries
    )


# ---------------------------------------------------------------------------
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

    # Simulate the agent calling set_checkpoint to record a state
    # transition — this replaces the checkpoint, dropping ticket_id.
    env.registry.update_checkpoint(sub_id, {"last_known_state": "in_progress"})
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.checkpoint == {"last_known_state": "in_progress"}
    assert "ticket_id" not in (info.checkpoint or {})

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

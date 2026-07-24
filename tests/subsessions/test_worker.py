"""Tests for the subsession worker: spawn validation and the turn loop."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robotsix_chat.chat.events import SSE_SUBSESSION_RESULT_TYPE
from robotsix_chat.subsessions import (
    SubsessionCapacityError,
    SubsessionDepthError,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionLevelError,
    SubsessionPeriodicSpawnError,
    SubsessionStatus,
    spawn_subsession,
)
from robotsix_chat.subsessions.worker import (
    CloseState,
    SubsessionContext,
    SubsessionEnv,
    _is_duplicate_reply,
    _is_no_change,
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
    assert reply == "user satisfied"


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


@pytest.mark.asyncio
async def test_periodic_run_delivers_result_frame_and_turn() -> None:
    """Each non-suppressed run is delivered to the store and the event sink."""
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
    assert len(history) == 3  # two run results + the terminal summary
    assert history[0] == (f"[Subsession {sub_id[:8]} 'watch' run 1]", "report 1")
    assert history[1] == (f"[Subsession {sub_id[:8]} 'watch' run 2]", "report 2")
    assert "max_runs" in history[2][0]


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
    assert info.summary == "Auto-stopped after 2 consecutive no-change runs."
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
    assert info.error == "kaboom"
    assert info.summary is not None
    assert info.summary.startswith("Failed: kaboom")

    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    label, reply = history[0]
    assert "failed" in label
    assert reply == info.summary


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
async def test_periodic_parent_cannot_spawn_periodic_child() -> None:
    """A periodic subsession cannot spawn another periodic subsession."""
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

    with pytest.raises(SubsessionPeriodicSpawnError, match="periodic"):
        _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            parent_id=parent.id,
            depth=2,
            interval_seconds=5.0,
        )

    # Non-periodic children (e.g. task) are still allowed.
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

    mock = _mock_async_client(response_json={"state": "closed"})
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
async def test_handle_mill_unreachable_cap_closes_and_delivers():
    """When the counter reaches the cap the subsession is closed.

    Summary delivered to the parent conversation.
    """
    env = build_env()
    # _MAX_MILL_FAILURES is 2, so one below the cap is 1.
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=1,
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
    """Verbose replies that repeat verbatim are suppressed like NO_CHANGE."""
    agent = FakeAgent(["Status: all clear", "Status: all clear"])
    env = build_env(agent=agent)

    sub_id = _spawn(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02, max_runs=2
    )
    await _await_worker(env, sub_id)

    # First run is delivered (it's new); second run is suppressed (duplicate).
    history = env.conversation_store.history(OWNER)
    # Only the first run result and the terminal summary appear — no second run.
    assert len(history) == 2
    assert history[0] == (
        f"[Subsession {sub_id[:8]} 'job' run 1]",
        "Status: all clear",
    )
    assert "max_runs" in history[1][0]


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

"""Spawn-dedup guard / run-guard / wait-for-event write-barrier tests.

Split out of test_worker.py into this module (spawn-dedup guard,
run-guard, and wait-for-event write-barrier coverage).
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from robotsix_chat.subsessions import (
    SubsessionKind,
    SubsessionStatus,
    spawn_subsession,
)
from robotsix_chat.subsessions.worker import SubsessionContext
from tests.common.subsession_fakes import (
    CapturingAgentFactory,
    FakeAgent,
    build_env,
    make_settings,
    wait_until,
)
from tests.subsessions.conftest import OWNER, _await_worker, _spawn

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

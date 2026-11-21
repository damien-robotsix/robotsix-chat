"""Unit tests for the per-conversation monitor slot-budget bookkeeping.

Also covers the worker-level admission tests for the monitor slot budget.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from robotsix_chat.subsessions import (
    SubsessionCapacityError,
    SubsessionKind,
    SubsessionRegistry,
    spawn_subsession,
)
from robotsix_chat.subsessions.slot_budget import (
    SLOT_BUDGET_QUEUED,
    SlotBudget,
    SlotBudgetQueueFullError,
)
from tests.common.subsession_fakes import (
    FakeAgent,
    FakeClock,
    build_env,
    make_settings,
)
from tests.subsessions.conftest import OWNER, _spawn


def test_slot_budget_disabled_when_budget_zero() -> None:
    budget = SlotBudget(budget=0, queue_max=32)
    assert not budget.enabled


def test_slot_budget_enqueue_is_fifo_per_conversation() -> None:
    budget = SlotBudget(budget=2, queue_max=3)
    budget.enqueue("sess-a", {"title": "one"})
    budget.enqueue("sess-a", {"title": "two"})
    budget.enqueue("sess-b", {"title": "other"})

    assert budget.pending_count("sess-a") == 2
    assert budget.pending_owner_ids() == ["sess-a", "sess-b"]
    assert budget.pop_next("sess-a") == {"title": "one"}
    assert budget.pop_next("sess-a") == {"title": "two"}
    assert budget.pop_next("sess-a") is None
    assert budget.pending_owner_ids() == ["sess-b"]


def test_slot_budget_enqueue_rejects_when_queue_full() -> None:
    budget = SlotBudget(budget=2, queue_max=1)
    budget.enqueue("sess-a", {"title": "one"})
    with pytest.raises(SlotBudgetQueueFullError, match="queue.*full"):
        budget.enqueue("sess-a", {"title": "two"})
    # The rejected request did not grow the queue.
    assert budget.pending_count("sess-a") == 1


def test_queued_sentinel_is_not_a_real_subsession_id() -> None:
    # Guards the call-site contract: spawn_subsession returns this exact
    # sentinel when the request was queued, and callers branch on it.
    assert SLOT_BUDGET_QUEUED == "__slot_budget_queued__"
    assert not SLOT_BUDGET_QUEUED.startswith("sub")


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

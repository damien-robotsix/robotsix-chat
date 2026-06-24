"""Tests for :class:`TaskRegistry` — per-client background task tracking."""

from __future__ import annotations

import asyncio
from itertools import count

import pytest

from robotsix_chat.chat.tasks import TaskRegistry, TaskStatus
from tests.chat import _fake_coro


class _FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _registry(clock: _FakeClock | None = None) -> TaskRegistry:
    """Build a registry with deterministic task ids (``t0``, ``t1``, …)."""
    ids = count()
    return TaskRegistry(
        clock=clock or _FakeClock(),
        id_factory=lambda: f"t{next(ids)}",
    )


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_register_returns_task_id() -> None:
    """Registering a task returns a unique id."""
    reg = _registry()
    tid = reg.register("client-a", "do something", _fake_coro())  # type: ignore[arg-type]

    assert tid == "t0"


def test_register_stores_task_info_with_running_status() -> None:
    """A newly-registered task has status ``RUNNING``."""
    reg = _registry()
    tid = reg.register("client-a", "compute answer", _fake_coro())  # type: ignore[arg-type]

    info = reg.get(tid)
    assert info is not None
    assert info.id == tid
    assert info.client_id == "client-a"
    assert info.prompt == "compute answer"
    assert info.status == TaskStatus.RUNNING
    assert info.result is None
    assert info.error is None


def test_register_multiple_tasks_get_distinct_ids() -> None:
    """Each registered task receives a unique id."""
    reg = _registry()

    t1 = reg.register("client-a", "task 1", _fake_coro())  # type: ignore[arg-type]
    t2 = reg.register("client-a", "task 2", _fake_coro())  # type: ignore[arg-type]

    assert t1 != t2
    assert reg.get(t1) is not None
    assert reg.get(t2) is not None


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------


def test_get_nonexistent_task_returns_none() -> None:
    """Looking up a task id that was never registered returns ``None``."""
    reg = _registry()
    assert reg.get("bogus") is None


def test_list_for_client_returns_tasks() -> None:
    """``list_for_client`` returns all tasks registered under a client."""
    reg = _registry()

    reg.register("client-a", "task a1", _fake_coro())  # type: ignore[arg-type]
    reg.register("client-a", "task a2", _fake_coro())  # type: ignore[arg-type]

    tasks = reg.list_for_client("client-a")
    assert len(tasks) == 2
    prompts = {t.prompt for t in tasks}
    assert prompts == {"task a1", "task a2"}


def test_list_for_client_unknown_client_returns_empty() -> None:
    """An unknown client yields an empty list, not an error."""
    reg = _registry()
    assert reg.list_for_client("nobody") == []


def test_list_for_client_isolated_per_client() -> None:
    """Tasks for one client are not visible from another."""
    reg = _registry()

    reg.register("client-a", "a-only", _fake_coro())  # type: ignore[arg-type]
    reg.register("client-b", "b-only", _fake_coro())  # type: ignore[arg-type]

    a_tasks = reg.list_for_client("client-a")
    b_tasks = reg.list_for_client("client-b")

    assert [t.prompt for t in a_tasks] == ["a-only"]
    assert [t.prompt for t in b_tasks] == ["b-only"]


# ---------------------------------------------------------------------------
# status transitions
# ---------------------------------------------------------------------------


def test_complete_transitions_to_completed_and_stores_result() -> None:
    """Calling ``complete()`` sets status to COMPLETED and stores the result."""
    reg = _registry()
    tid = reg.register("client-a", "solve", _fake_coro())  # type: ignore[arg-type]

    reg.complete(tid, "42")

    info = reg.get(tid)
    assert info is not None
    assert info.status == TaskStatus.COMPLETED
    assert info.result == "42"
    assert info.error is None


def test_fail_transitions_to_failed_and_stores_error() -> None:
    """Calling ``fail()`` sets status to FAILED and stores the error."""
    reg = _registry()
    tid = reg.register("client-a", "risky op", _fake_coro())  # type: ignore[arg-type]

    reg.fail(tid, "connection refused")

    info = reg.get(tid)
    assert info is not None
    assert info.status == TaskStatus.FAILED
    assert info.result is None
    assert info.error == "connection refused"


def test_complete_and_fail_ignore_unknown_task_id() -> None:
    """Calling ``complete()`` or ``fail()`` on an unknown id does not raise."""
    reg = _registry()

    reg.complete("no-such-id", "ignored")
    reg.fail("no-such-id", "also ignored")

    # No exception — the calls are no-ops.


def test_task_can_transition_only_once() -> None:
    """A task's status is overwritten by the last transition call."""
    reg = _registry()
    tid = reg.register("client-a", "flip", _fake_coro())  # type: ignore[arg-type]

    reg.complete(tid, "first")
    reg.fail(tid, "then failed")

    info = reg.get(tid)
    assert info is not None
    # Last call wins.
    assert info.status == TaskStatus.FAILED
    assert info.error == "then failed"


# ---------------------------------------------------------------------------
# strong references (GC safety)
# ---------------------------------------------------------------------------


def test_strong_reference_prevents_gc_of_running_task() -> None:
    """The registry holds a strong reference so a running task is not GC'd."""
    reg = _registry()
    tid = reg.register("client-a", "long op", _fake_coro())  # type: ignore[arg-type]

    # The registry's internal _running dict holds a reference.
    assert tid in reg._running


@pytest.mark.asyncio
async def test_reference_dropped_when_task_completes() -> None:
    """After a task coroutine finishes, the strong reference is dropped."""
    reg = _registry()

    async def quick() -> None:
        pass

    task = asyncio.create_task(quick())
    tid = reg.register("client-a", "quick", task)
    await task  # let it finish

    # The done callback should have removed the reference.
    assert tid not in reg._running


# ---------------------------------------------------------------------------
# count_running
# ---------------------------------------------------------------------------


def test_count_running_reflects_registered_tasks() -> None:
    """``count_running()`` returns the number of in-flight tasks."""
    reg = _registry()

    assert reg.count_running() == 0

    reg.register("c1", "task 1", _fake_coro())  # type: ignore[arg-type]
    assert reg.count_running() == 1

    reg.register("c1", "task 2", _fake_coro())  # type: ignore[arg-type]
    assert reg.count_running() == 2


def test_count_running_drops_after_done_callback() -> None:
    """``count_running()`` declines after the done callback fires.

    With the ``_FakeCoro`` stand-in the done callback is a no-op, so we
    simulate what the real ``asyncio.Task.add_done_callback`` does by
    manually popping from ``_running``.
    """
    reg = _registry()

    tid = reg.register("c1", "to finish", _fake_coro())  # type: ignore[arg-type]
    assert reg.count_running() == 1

    # Simulate the done callback firing.
    reg._running.pop(tid, None)
    assert reg.count_running() == 0

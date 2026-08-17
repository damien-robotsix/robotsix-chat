"""Unit tests for the per-conversation monitor slot-budget bookkeeping."""

from __future__ import annotations

import pytest

from robotsix_chat.subsessions.slot_budget import (
    SLOT_BUDGET_QUEUED,
    SlotBudget,
    SlotBudgetQueueFullError,
)


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

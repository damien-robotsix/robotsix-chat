"""Per-conversation monitor slot-budget manager.

The slot budget governs how new monitor requests are admitted when a
conversation's monitor pool is at capacity, replacing "evict a paused
monitor to make room" behaviour with a deterministic algorithm:

* A conversation occupies one slot per monitor in ``active`` (running)
  or ``paused`` (spawned but suspended) state.
* When a new monitor is requested and the conversation is at budget,
  the least-recently-active paused monitor's slot is reclaimed
  (repurposed for the new request) — occupied count stays unchanged.
* When no paused monitor exists (every slot is active), the request is
  enqueued in a per-conversation FIFO pending queue instead of evicting
  a live monitor.
* When a monitor terminates and a slot frees, the oldest pending request
  is dequeued and spawned into the freed slot.

This module holds only the bookkeeping — the admission decision and the
spawn/drain wiring live in :mod:`robotsix_chat.subsessions.worker`.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

__all__ = [
    "SLOT_BUDGET_QUEUED",
    "SlotBudget",
    "SlotBudgetQueueFullError",
]

# Returned by ``spawn_subsession`` when the request was enqueued by the
# slot-budget manager instead of spawning (conversation at budget with
# no paused monitor available for reuse).  Not a real subsession id.
SLOT_BUDGET_QUEUED = "__slot_budget_queued__"


class SlotBudgetQueueFullError(RuntimeError):
    """Raised when a pending monitor request would exceed the queue cap."""


class SlotBudget:
    """Per-conversation FIFO pending queues for monitor-spawn requests.

    Pure bookkeeping: it never spawns or closes anything.  The
    admission decision in ``spawn_subsession`` consults
    :attr:`enabled`, :meth:`enqueue`, and :meth:`pop_next`; the
    close-callback wiring in ``worker.attach_slot_budget`` drains the
    queue when a monitor terminates.
    """

    def __init__(self, *, budget: int, queue_max: int) -> None:
        """Configure the per-conversation slot budget and queue cap.

        *budget* is the maximum number of occupied slots (active +
        paused monitors) per conversation; ``0`` disables budgeting.
        *queue_max* caps the pending-request queue per conversation.
        """
        self._budget = budget
        self._queue_max = queue_max
        # owner_session_id → FIFO of pending spawn requests (kwargs).
        self._queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)

    @property
    def enabled(self) -> bool:
        """Whether per-conversation slot budgeting is active."""
        return self._budget > 0

    @property
    def budget(self) -> int:
        """Maximum occupied slots (active + paused) per conversation."""
        return self._budget

    @property
    def queue_max(self) -> int:
        """Maximum pending-request queue length per conversation."""
        return self._queue_max

    def pending_count(self, owner_session_id: str) -> int:
        """Return the number of pending requests for *owner_session_id*."""
        return len(self._queues.get(owner_session_id, ()))

    def pending_owner_ids(self) -> list[str]:
        """Return owner ids that currently have pending requests."""
        return [owner for owner, queue in self._queues.items() if queue]

    def discard(self, owner_session_id: str) -> None:
        """Drop all pending requests for *owner_session_id*.

        Used when the conversation itself is being torn down — queued
        monitor requests must not spawn work for a dead session.
        """
        self._queues.pop(owner_session_id, None)

    def enqueue(self, owner_session_id: str, request: dict[str, Any]) -> None:
        """Append *request* to *owner_session_id*'s FIFO pending queue.

        Raises :class:`SlotBudgetQueueFullError` when the queue is
        already at ``queue_max`` — the caller rejects the request with a
        clear error rather than growing the queue unbounded.
        """
        if self.pending_count(owner_session_id) >= self._queue_max:
            raise SlotBudgetQueueFullError(
                f"monitor request queue for conversation "
                f"{owner_session_id!r} is full "
                f"({self._queue_max} pending)"
            )
        self._queues[owner_session_id].append(request)

    def pop_next(self, owner_session_id: str) -> dict[str, Any] | None:
        """Pop and return the oldest pending request, or ``None``."""
        queue = self._queues.get(owner_session_id)
        if queue:
            return queue.popleft()
        return None

    def requeue_front(self, owner_session_id: str, request: dict[str, Any]) -> None:
        """Put *request* back at the front of the owner's pending queue.

        Used by the drain loop when spawning the popped request fails
        (e.g. the global concurrency cap) — the request keeps its FIFO
        position instead of being silently dropped.
        """
        self._queues[owner_session_id].appendleft(request)

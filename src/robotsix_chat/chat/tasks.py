"""In-memory task registry for background sub-agent tasks.

Tracks spawned background sub-agent tasks per ``client_id`` so the server can
report their status to the browser. Each task is assigned a unique id and holds
its current status (``running`` / ``completed`` / ``failed``), result or error,
and a strong reference to the owning ``asyncio.Task`` — preventing the event
loop from garbage-collecting an in-flight task that has no other referent.

The registry is process-local and unsynchronised: it is sized for the
single-worker ``uvicorn.run`` the server uses. Running multiple workers would
split a client's tasks across processes — each worker would only see the subset
it spawned.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class TaskStatus(StrEnum):
    """Lifecycle status of a background task."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskInfo:
    """Public snapshot of a single background task.

    Returned by :meth:`TaskRegistry.get` and
    :meth:`TaskRegistry.list_for_client`.
    """

    id: str
    client_id: str
    prompt: str
    status: TaskStatus
    result: str | None = None
    error: str | None = None


class TaskRegistry:
    """Track per-client background sub-agent tasks in memory.

    Holds a strong reference to every in-flight :class:`asyncio.Task` so it is
    not garbage-collected before completion — mirroring the ``_write_tasks``
    pattern used by
    :class:`~robotsix_chat.llm.agent.LlmioChatAgent._schedule_remember`.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        """Configure the clock and id factory used by the registry."""
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        # task_id → TaskInfo (status + metadata snapshot).
        self._tasks: dict[str, TaskInfo] = {}
        # task_id → asyncio.Task (strong reference to prevent GC).
        self._running: dict[str, asyncio.Task[None]] = {}
        # client_id → set of task_ids.
        self._by_client: dict[str, set[str]] = defaultdict(set)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def register(
        self,
        client_id: str,
        prompt: str,
        coro: asyncio.Task[None],
    ) -> str:
        """Register a new *coro* as a background task for *client_id*.

        *coro* must be an already-scheduled :class:`asyncio.Task` (created via
        :func:`asyncio.create_task`). The registry stores a strong reference to
        it and arranges for that reference to be dropped when the task
        finishes.

        Returns the newly-assigned task id.
        """
        task_id = self._id_factory()
        info = TaskInfo(
            id=task_id,
            client_id=client_id,
            prompt=prompt,
            status=TaskStatus.RUNNING,
        )
        self._tasks[task_id] = info
        self._running[task_id] = coro
        self._by_client[client_id].add(task_id)
        coro.add_done_callback(lambda _t: self._running.pop(task_id, None))
        return task_id

    def get(self, task_id: str) -> TaskInfo | None:
        """Return the current snapshot of *task_id*, or ``None``."""
        return self._tasks.get(task_id)

    def list_for_client(self, client_id: str) -> list[TaskInfo]:
        """Return all tasks for *client_id*."""
        ids = self._by_client.get(client_id, set())
        return [self._tasks[tid] for tid in ids if tid in self._tasks]

    def complete(self, task_id: str, result: str) -> None:
        """Mark *task_id* as completed with the given *result*."""
        info = self._tasks.get(task_id)
        if info is not None:
            info.status = TaskStatus.COMPLETED
            info.result = result

    def fail(self, task_id: str, error: str) -> None:
        """Mark *task_id* as failed with the given *error*."""
        info = self._tasks.get(task_id)
        if info is not None:
            info.status = TaskStatus.FAILED
            info.error = error

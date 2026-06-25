"""In-memory task registry for background sub-agent tasks.

Tracks spawned background sub-agent tasks per ``session_id`` so the server can
report their status to the browser, scoped to the chat session that spawned
them. Each task is assigned a unique id and holds its current status
(``running`` / ``completed`` / ``failed``), result or error, and a strong
reference to the owning ``asyncio.Task`` — preventing the event loop from
garbage-collecting an in-flight task that has no other referent.

The registry is process-local and unsynchronised: it is sized for the
single-worker ``uvicorn.run`` the server uses. Running multiple workers would
split a session's tasks across processes — each worker would only see the
subset it spawned.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .events import (
    EventSink,
    task_completed_frame,
    task_failed_frame,
    task_started_frame,
)


class TaskStatus(StrEnum):
    """Lifecycle status of a background task."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskInfo:
    """Public snapshot of a single background task.

    Returned by :meth:`TaskRegistry.get` and
    :meth:`TaskRegistry.list_for_session`.
    """

    id: str
    session_id: str
    prompt: str
    status: TaskStatus
    result: str | None = None
    error: str | None = None


class TaskRegistry:
    """Track per-session background sub-agent tasks in memory.

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
        event_sink: EventSink | None = None,
    ) -> None:
        """Configure the clock, id factory, and optional event sink.

        When *event_sink* is provided, lifecycle frames are published on
        :meth:`register`, :meth:`complete`, and :meth:`fail`.
        """
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._event_sink = event_sink
        # task_id → TaskInfo (status + metadata snapshot).
        self._tasks: dict[str, TaskInfo] = {}
        # task_id → asyncio.Task (strong reference to prevent GC).
        self._running: dict[str, asyncio.Task[None]] = {}
        # session_id → set of task_ids.
        self._by_session: dict[str, set[str]] = defaultdict(set)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def register(
        self,
        session_id: str,
        prompt: str,
        coro: asyncio.Task[None],
    ) -> str:
        """Register a new *coro* as a background task for *session_id*.

        *coro* must be an already-scheduled :class:`asyncio.Task` (created via
        :func:`asyncio.create_task`). The registry stores a strong reference to
        it and arranges for that reference to be dropped when the task
        finishes.

        Returns the newly-assigned task id.
        """
        task_id = self._id_factory()
        info = TaskInfo(
            id=task_id,
            session_id=session_id,
            prompt=prompt,
            status=TaskStatus.RUNNING,
        )
        self._tasks[task_id] = info
        self._running[task_id] = coro
        self._by_session[session_id].add(task_id)
        coro.add_done_callback(lambda _t: self._running.pop(task_id, None))
        if self._event_sink is not None:
            self._event_sink.publish(
                session_id, task_started_frame(task_id, session_id, prompt)
            )
        return task_id

    def get(self, task_id: str) -> TaskInfo | None:
        """Return the current snapshot of *task_id*, or ``None``."""
        return self._tasks.get(task_id)

    def count_running(self) -> int:
        """Return the number of in-flight background tasks (process-wide)."""
        return len(self._running)

    def list_for_session(self, session_id: str) -> list[TaskInfo]:
        """Return all tasks for *session_id*."""
        ids = self._by_session.get(session_id, set())
        return [self._tasks[tid] for tid in ids if tid in self._tasks]

    def cancel_all_for_session(self, session_id: str) -> int:
        """Cancel every in-flight background task owned by *session_id*.

        Used when a session is closed/deleted so its background sub-agents do
        not outlive it.  Each still-running :class:`asyncio.Task` is cancelled
        and its snapshot flipped to ``FAILED`` (cancelled).  Already-finished
        tasks are skipped.  Returns the number of tasks actually cancelled.
        """
        cancelled = 0
        for task_id in list(self._by_session.get(session_id, ())):
            task = self._running.get(task_id)
            if task is not None and not task.done():
                task.cancel()
                info = self._tasks.get(task_id)
                if info is not None and info.status is TaskStatus.RUNNING:
                    info.status = TaskStatus.FAILED
                    info.error = "cancelled: session closed"
                cancelled += 1
        return cancelled

    def complete(self, task_id: str, result: str) -> None:
        """Mark *task_id* as completed with the given *result*."""
        info = self._tasks.get(task_id)
        if info is not None:
            info.status = TaskStatus.COMPLETED
            info.result = result
            if self._event_sink is not None:
                self._event_sink.publish(
                    info.session_id, task_completed_frame(task_id, result)
                )

    def fail(self, task_id: str, error: str) -> None:
        """Mark *task_id* as failed with the given *error*."""
        info = self._tasks.get(task_id)
        if info is not None:
            info.status = TaskStatus.FAILED
            info.error = error
            if self._event_sink is not None:
                self._event_sink.publish(
                    info.session_id, task_failed_frame(task_id, error)
                )

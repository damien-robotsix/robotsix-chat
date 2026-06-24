"""In-memory per-client SSE event bus for background-task lifecycle events.

Provides frame builders, type constants, and a publish/subscribe registry so
the chat server can push ``task_started`` / ``task_completed`` / ``task_failed``
notification frames to connected browsers via a persistent SSE channel.

This module must NOT import from ``tasks.py`` — the dependency is one-way:
``tasks.py`` → ``events.py``, never a cycle.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Protocol

# ---------------------------------------------------------------------------
# SSE frame-type constants (mirror the SSE_*_TYPE naming convention in server.py)
# ---------------------------------------------------------------------------

SSE_TASK_STARTED_TYPE = "task_started"
SSE_TASK_COMPLETED_TYPE = "task_completed"
SSE_TASK_FAILED_TYPE = "task_failed"

SSE_LOOP_STARTED_TYPE = "loop_started"
SSE_LOOP_TICK_TYPE = "loop_tick"
SSE_LOOP_STOPPED_TYPE = "loop_stopped"
SSE_LOOP_FAILED_TYPE = "loop_failed"
SSE_LOOP_REPLY_TYPE = "loop_reply"

# ---------------------------------------------------------------------------
# EventSink — structural Protocol for dependency injection
# ---------------------------------------------------------------------------


class EventSink(Protocol):
    """Structural interface for publishing lifecycle frames to a client.

    ``TaskRegistry`` depends on this protocol (dependency injection) so it
    never imports the concrete :class:`EventBus` — any object with a matching
    ``publish`` method satisfies the contract.
    """

    def publish(self, client_id: str, frame: dict[str, object]) -> None:
        """Deliver *frame* to the owner of *client_id*."""
        ...


# ---------------------------------------------------------------------------
# Frame builders
#
# Each takes plain primitive fields (NOT a ``TaskInfo``, to avoid importing
# ``tasks``) and returns a dict of the exact shape documented in its docstring.
# ---------------------------------------------------------------------------


def task_started_frame(task_id: str, client_id: str, prompt: str) -> dict[str, object]:
    """Build a ``task_started`` notification frame.

    Returns a dict with shape::

        {
            "type": "task_started",
            "task_id": <str>,
            "client_id": <str>,
            "prompt": <str>,
            "status": "running",
        }
    """
    return {
        "type": SSE_TASK_STARTED_TYPE,
        "task_id": task_id,
        "client_id": client_id,
        "prompt": prompt,
        "status": "running",
    }


def task_completed_frame(task_id: str, result: str) -> dict[str, object]:
    """Build a ``task_completed`` notification frame.

    Returns a dict with shape::

        {
            "type": "task_completed",
            "task_id": <str>,
            "status": "completed",
            "result": <str>,
        }
    """
    return {
        "type": SSE_TASK_COMPLETED_TYPE,
        "task_id": task_id,
        "status": "completed",
        "result": result,
    }


def task_failed_frame(task_id: str, error: str) -> dict[str, object]:
    """Build a ``task_failed`` notification frame.

    Returns a dict with shape::

        {
            "type": "task_failed",
            "task_id": <str>,
            "status": "failed",
            "error": <str>,
        }
    """
    return {
        "type": SSE_TASK_FAILED_TYPE,
        "task_id": task_id,
        "status": "failed",
        "error": error,
    }


# ---------------------------------------------------------------------------
# Loop frame builders
# ---------------------------------------------------------------------------


def loop_started_frame(
    loop_id: str,
    client_id: str,
    prompt: str,
    interval_seconds: float,
    max_iterations: int | None,
    *,
    reason: str | None = None,
) -> dict[str, object]:
    """Build a ``loop_started`` notification frame.

    Returns a dict with shape::

        {
            "type": "loop_started",
            "loop_id": <str>,
            "client_id": <str>,
            "prompt": <str>,
            "interval_seconds": <float>,
            "max_iterations": <int | None>,
            "status": "running",
            "reason": <str | None>,
        }
    """
    result: dict[str, object] = {
        "type": SSE_LOOP_STARTED_TYPE,
        "loop_id": loop_id,
        "client_id": client_id,
        "prompt": prompt,
        "interval_seconds": interval_seconds,
        "max_iterations": max_iterations,
        "status": "running",
    }
    if reason is not None:
        result["reason"] = reason
    return result


def loop_tick_frame(
    loop_id: str,
    iteration: int,
    result: str,
    next_run: float | None,
    *,
    last_result_at: float | None = None,
) -> dict[str, object]:
    """Build a ``loop_tick`` notification frame.

    Returns a dict with shape::

        {
            "type": "loop_tick",
            "loop_id": <str>,
            "iteration": <int>,
            "result": <str>,
            "next_run": <float | None>,
            "status": "running",
            "last_result_at": <float | None>,
        }
    """
    return {
        "type": SSE_LOOP_TICK_TYPE,
        "loop_id": loop_id,
        "iteration": iteration,
        "result": result,
        "next_run": next_run,
        "status": "running",
        "last_result_at": last_result_at,
    }


def loop_stopped_frame(
    loop_id: str,
    reason: str,
    iterations: int,
) -> dict[str, object]:
    """Build a ``loop_stopped`` notification frame.

    Returns a dict with shape::

        {
            "type": "loop_stopped",
            "loop_id": <str>,
            "reason": <str>,
            "iterations": <int>,
            "status": "stopped",
        }
    """
    return {
        "type": SSE_LOOP_STOPPED_TYPE,
        "loop_id": loop_id,
        "reason": reason,
        "iterations": iterations,
        "status": "stopped",
    }


def loop_failed_frame(
    loop_id: str,
    error: str,
) -> dict[str, object]:
    """Build a ``loop_failed`` notification frame.

    Returns a dict with shape::

        {
            "type": "loop_failed",
            "loop_id": <str>,
            "error": <str>,
            "status": "failed",
        }
    """
    return {
        "type": SSE_LOOP_FAILED_TYPE,
        "loop_id": loop_id,
        "error": error,
        "status": "failed",
    }


def loop_reply_frame(
    loop_id: str,
    iteration: int,
    reply: str,
) -> dict[str, object]:
    """Build a ``loop_reply`` notification frame.

    Emitted when a tick-triggered foreground agent run completes, carrying
    the full assistant reply for rendering in the browser as a normal
    assistant bubble.

    Returns a dict with shape::

        {
            "type": "loop_reply",
            "loop_id": <str>,
            "iteration": <int>,
            "reply": <str>,
        }
    """
    return {
        "type": SSE_LOOP_REPLY_TYPE,
        "loop_id": loop_id,
        "iteration": iteration,
        "reply": reply,
    }


# ---------------------------------------------------------------------------
# EventBus — per-client asyncio.Queue registry
# ---------------------------------------------------------------------------


class EventBus:
    """Per-client :class:`asyncio.Queue` registry for SSE notification frames.

    Callers publish frames to a ``client_id``; every queue currently subscribed
    for that id receives the frame.  A browser that (re)connects re-syncs
    current state via ``TaskRegistry.list_for_client(client_id)`` rather than
    replaying a buffer — so when no queue is subscribed for a ``client_id``,
    :meth:`publish` **silently drops the frame** (no buffering).  The in-memory
    model favours bounded memory over guaranteed delivery.
    """

    def __init__(self) -> None:
        """Create an empty bus with no subscribers."""
        self._subscribers: defaultdict[str, set[asyncio.Queue[dict[str, object]]]] = (
            defaultdict(set)
        )

    def subscribe(self, client_id: str) -> asyncio.Queue[dict[str, object]]:
        """Create a fresh queue, add to *client_id*'s subscribers, return it."""
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._subscribers[client_id].add(queue)
        return queue

    def unsubscribe(
        self, client_id: str, queue: asyncio.Queue[dict[str, object]]
    ) -> None:
        """Discard *queue*; drop the *client_id* key when its set becomes empty."""
        subscribers = self._subscribers.get(client_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            del self._subscribers[client_id]

    def publish(self, client_id: str, frame: dict[str, object]) -> None:
        """Put *frame* on every queue currently subscribed for *client_id*.

        If no queue is subscribed, the frame is dropped silently — it is
        **not** buffered.  See the class docstring for the rationale.
        """
        for queue in self._subscribers.get(client_id, ()):
            queue.put_nowait(frame)

    def subscriber_count(self, client_id: str | None = None) -> int:
        """Return the number of subscribed queues (read-only).

        With no argument returns the total count across all clients.  With a
        *client_id* returns that client's count (0 if unknown).  Does not
        mutate ``_subscribers``.
        """
        if client_id is None:
            return sum(len(qs) for qs in self._subscribers.values())
        return len(self._subscribers.get(client_id, ()))

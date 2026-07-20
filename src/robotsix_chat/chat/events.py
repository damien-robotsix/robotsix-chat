"""In-memory per-session SSE event bus for subsession lifecycle events.

Provides frame builders, type constants, and a publish/subscribe registry so
the chat server can push ``subsession_*`` notification frames to connected
browsers via a persistent SSE channel, scoped to the chat session that owns
the work.

This module must NOT import from ``robotsix_chat.subsessions`` — the
dependency is one-way: ``subsessions`` → ``events``, never a cycle.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Protocol

# ---------------------------------------------------------------------------
# SSE frame-type constants (mirror the SSE_*_TYPE naming convention in server.py)
# ---------------------------------------------------------------------------

SSE_SUBSESSION_STARTED_TYPE = "subsession_started"
SSE_SUBSESSION_UPDATED_TYPE = "subsession_updated"
SSE_SUBSESSION_MESSAGE_TYPE = "subsession_message"
SSE_SUBSESSION_RESULT_TYPE = "subsession_result"
SSE_SUBSESSION_CLOSED_TYPE = "subsession_closed"
SSE_SUBSESSION_FAILED_TYPE = "subsession_failed"

# Autonomous session lifecycle events.
SSE_AUTONOMOUS_STATE_CHANGED = "autonomous_state_changed"
SSE_AUTONOMOUS_RESPAWNED = "autonomous_respawned"
SSE_AUTONOMOUS_APPROVAL_REQUIRED = "autonomous_approval_required"

# Live claudeSDK activity (tool calls/results, thinking, intermediate text)
# streamed during an in-flight /chat turn — see ``activity_frame``.
SSE_ACTIVITY_TYPE = "activity"

# A background-triggered agent reply (not a live /chat request) pushed to a
# connected browser as soon as it's ready — see ``agent_message_frame``.
SSE_AGENT_MESSAGE_TYPE = "agent_message"

# ---------------------------------------------------------------------------
# EventSink — structural Protocol for dependency injection
# ---------------------------------------------------------------------------


class EventSink(Protocol):
    """Structural interface for publishing lifecycle frames to a session.

    ``SubsessionRegistry`` depends on this protocol (dependency injection) so
    it never imports the concrete :class:`EventBus` — any object with a
    matching ``publish`` method satisfies the contract.
    """

    def publish(self, session_id: str, frame: dict[str, object]) -> None:
        """Deliver *frame* to subscribers of *session_id*."""
        ...


# ---------------------------------------------------------------------------
# Subsession frame builders
#
# ``subsession_started_frame`` takes the plain-dict snapshot produced by
# ``SubsessionInfo.snapshot()`` (a dict, NOT the dataclass — keeps this
# module free of ``subsessions`` imports; the dependency stays one-way).
# ---------------------------------------------------------------------------


def subsession_started_frame(snapshot: dict[str, object]) -> dict[str, object]:
    """Build a ``subsession_started`` frame from an info *snapshot* dict.

    Returns the full snapshot (id, kind, parent_id, depth, title, status,
    model_level, interval/next-run fields, …) plus ``"type":
    "subsession_started"`` so the UI can insert the row directly.
    """
    return {"type": SSE_SUBSESSION_STARTED_TYPE, **snapshot}


def subsession_updated_frame(
    subsession_id: str,
    status: str,
    *,
    runs: int = 0,
    next_run_at: float | None = None,
    last_activity_at: float | None = None,
    last_result: str | None = None,
) -> dict[str, object]:
    """Build a ``subsession_updated`` frame (status / scheduling delta).

    Returns a dict with shape::

        {
            "type": "subsession_updated",
            "subsession_id": <str>,
            "status": <str>,
            "runs": <int>,
            "next_run_at": <float | None>,
            "last_activity_at": <float | None>,
            "last_result": <str | None>,
        }
    """
    return {
        "type": SSE_SUBSESSION_UPDATED_TYPE,
        "subsession_id": subsession_id,
        "status": status,
        "runs": runs,
        "next_run_at": next_run_at,
        "last_activity_at": last_activity_at,
        "last_result": last_result,
    }


def subsession_message_frame(
    subsession_id: str,
    role: str,
    text: str,
    timestamp: float,
) -> dict[str, object]:
    """Build a ``subsession_message`` frame (one transcript append).

    Returns a dict with shape::

        {
            "type": "subsession_message",
            "subsession_id": <str>,
            "role": <"user" | "parent" | "assistant" | "system">,
            "text": <str>,
            "timestamp": <float>,
        }
    """
    return {
        "type": SSE_SUBSESSION_MESSAGE_TYPE,
        "subsession_id": subsession_id,
        "role": role,
        "text": text,
        "timestamp": timestamp,
    }


def subsession_result_frame(
    subsession_id: str,
    kind: str,
    title: str,
    run: int,
    text: str,
    parent_id: str | None,
) -> dict[str, object]:
    """Build a ``subsession_result`` frame (non-suppressed periodic run result).

    Returns a dict with shape::

        {
            "type": "subsession_result",
            "subsession_id": <str>,
            "kind": <str>,
            "title": <str>,
            "run": <int>,
            "text": <str>,
            "parent_id": <str | None>,
        }
    """
    return {
        "type": SSE_SUBSESSION_RESULT_TYPE,
        "subsession_id": subsession_id,
        "kind": kind,
        "title": title,
        "run": run,
        "text": text,
        "parent_id": parent_id,
    }


def subsession_closed_frame(
    subsession_id: str,
    *,
    kind: str,
    title: str,
    reason: str,
    summary: str,
    closed_by: str,
    parent_id: str | None,
) -> dict[str, object]:
    """Build a ``subsession_closed`` frame (terminal, clean close).

    Returns a dict with shape::

        {
            "type": "subsession_closed",
            "subsession_id": <str>,
            "kind": <str>,
            "title": <str>,
            "reason": <str>,
            "summary": <str>,
            "closed_by": <"agent" | "user" | "parent" | "system">,
            "parent_id": <str | None>,
            "status": "closed",
        }
    """
    return {
        "type": SSE_SUBSESSION_CLOSED_TYPE,
        "subsession_id": subsession_id,
        "kind": kind,
        "title": title,
        "reason": reason,
        "summary": summary,
        "closed_by": closed_by,
        "parent_id": parent_id,
        "status": "closed",
    }


def subsession_failed_frame(
    subsession_id: str,
    *,
    kind: str,
    title: str,
    error: str,
    summary: str,
    parent_id: str | None,
) -> dict[str, object]:
    """Build a ``subsession_failed`` frame (terminal, error).

    Returns a dict with shape::

        {
            "type": "subsession_failed",
            "subsession_id": <str>,
            "kind": <str>,
            "title": <str>,
            "error": <str>,
            "summary": <str>,
            "parent_id": <str | None>,
            "status": "failed",
        }
    """
    return {
        "type": SSE_SUBSESSION_FAILED_TYPE,
        "subsession_id": subsession_id,
        "kind": kind,
        "title": title,
        "error": error,
        "summary": summary,
        "parent_id": parent_id,
        "status": "failed",
    }


def activity_frame(
    kind: str,
    turn: int,
    *,
    tool_name: str | None = None,
    detail: str = "",
    is_error: bool = False,
) -> dict[str, object]:
    """Build an ``activity`` frame from a live claudeSDK activity event.

    Mirrors ``robotsix_llmio.claude_sdk.ClaudeSDKActivityEvent`` — one tool
    call, tool result, thinking block, or intermediate assistant text
    streamed during an in-flight turn. *kind* is one of ``"tool_call"``,
    ``"tool_result"``, ``"thinking"``, ``"text"``.

    Returns a dict with shape::

        {
            "type": "activity",
            "kind": <str>,
            "turn": <int>,
            "tool_name": <str | None>,
            "detail": <str>,
            "is_error": <bool>,
        }
    """
    return {
        "type": SSE_ACTIVITY_TYPE,
        "kind": kind,
        "turn": turn,
        "tool_name": tool_name,
        "detail": detail,
        "is_error": is_error,
    }


def agent_message_frame(text: str, timestamp: float) -> dict[str, object]:
    """Build an ``agent_message`` frame for a background-triggered reply.

    Published when the agent reacts to something that happened outside a
    live ``POST /chat`` request (e.g. a subsession concluding) — there is no
    open request/response to carry the reply, so it is pushed over the
    persistent ``/events`` channel instead, and the browser appends it as a
    normal assistant chat bubble.

    Returns a dict with shape::

        {
            "type": "agent_message",
            "text": <str>,
            "timestamp": <float>,
        }
    """
    return {"type": SSE_AGENT_MESSAGE_TYPE, "text": text, "timestamp": timestamp}


def autonomous_state_changed_frame(
    session_id: str,
    old_state: str | None,
    new_state: str,
) -> dict[str, object]:
    """Build an ``autonomous_state_changed`` frame.

    Published when an autonomous session transitions between lifecycle
    states (e.g. ``selecting_subject`` → ``awaiting_approval``).

    Returns a dict with shape::

        {
            "type": "autonomous_state_changed",
            "session_id": <str>,
            "old_state": <str | null>,
            "new_state": <str>,
        }
    """
    return {
        "type": SSE_AUTONOMOUS_STATE_CHANGED,
        "session_id": session_id,
        "old_state": old_state,
        "new_state": new_state,
    }


def autonomous_respawned_frame(
    session_id: str,
    previous_session_id: str,
) -> dict[str, object]:
    """Build an ``autonomous_respawned`` frame.

    Published when a completed autonomous session auto-closes and a new
    autonomous session is spawned to continue the cycle.

    Returns a dict with shape::

        {
            "type": "autonomous_respawned",
            "session_id": <str>,
            "previous_session_id": <str>,
        }
    """
    return {
        "type": SSE_AUTONOMOUS_RESPAWNED,
        "session_id": session_id,
        "previous_session_id": previous_session_id,
    }


def autonomous_approval_required_frame(
    session_id: str,
    plan_summary: str,
) -> dict[str, object]:
    """Build an ``autonomous_approval_required`` frame.

    Published when an autonomous session has drafted a plan and is waiting
    for explicit operator approval before executing.

    Returns a dict with shape::

        {
            "type": "autonomous_approval_required",
            "session_id": <str>,
            "plan_summary": <str>,
        }
    """
    return {
        "type": SSE_AUTONOMOUS_APPROVAL_REQUIRED,
        "session_id": session_id,
        "plan_summary": plan_summary,
    }


# ---------------------------------------------------------------------------
# EventBus — per-client asyncio.Queue registry
# ---------------------------------------------------------------------------


class EventBus:
    """Per-session :class:`asyncio.Queue` registry for SSE notification frames.

    Callers publish frames to a ``session_id``; every queue currently
    subscribed for that id receives the frame.  A browser that (re)connects
    re-syncs current state via ``GET /subsessions?session_id=...``
    rather than replaying a buffer — so when no queue is subscribed for a
    ``session_id``, :meth:`publish` **silently drops the frame** (no
    buffering).  The in-memory model favours bounded memory over guaranteed
    delivery.
    """

    def __init__(self) -> None:
        """Create an empty bus with no subscribers."""
        self._subscribers: defaultdict[str, set[asyncio.Queue[dict[str, object]]]] = (
            defaultdict(set)
        )

    def subscribe(self, session_id: str) -> asyncio.Queue[dict[str, object]]:
        """Create a fresh queue, add to *session_id*'s subscribers, return it."""
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._subscribers[session_id].add(queue)
        return queue

    def unsubscribe(
        self, session_id: str, queue: asyncio.Queue[dict[str, object]]
    ) -> None:
        """Discard *queue*; drop the *session_id* key when its set becomes empty."""
        subscribers = self._subscribers.get(session_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            del self._subscribers[session_id]

    def publish(self, session_id: str, frame: dict[str, object]) -> None:
        """Put *frame* on every queue currently subscribed for *session_id*.

        If no queue is subscribed, the frame is dropped silently — it is
        **not** buffered.  See the class docstring for the rationale.
        """
        for queue in self._subscribers.get(session_id, ()):
            queue.put_nowait(frame)

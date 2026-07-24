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

# Live claudeSDK activity (tool calls/results, thinking, intermediate text)
# streamed during an in-flight /chat turn — see ``activity_frame``.
SSE_ACTIVITY_TYPE = "activity"

# A background-triggered agent reply (not a live /chat request) pushed to a
# connected browser as soon as it's ready — see ``agent_message_frame``.
SSE_AGENT_MESSAGE_TYPE = "agent_message"

# Autonomous session state transitions pushed over the persistent /events
# channel so the session-list row updates live without polling.
SSE_AUTONOMOUS_STATE_TYPE = "autonomous_state"

# Streaming token from an autonomous background turn (live progress).
SSE_AUTONOMOUS_TOKEN_TYPE = "autonomous_token"

# Foreground ``POST /chat`` turn lifecycle, mirrored onto the /events channel
# so a browser that is NOT the originating request — a second tab, or the same
# tab after it switched away from the session and came back — can see the
# in-progress turn.  The originating request still renders live tokens from its
# own POST response body and ignores these echoes; everyone else renders from
# them.  The bus buffers the CURRENT turn per session so a late subscriber
# replays what has been emitted so far via a ``chat_turn_resume`` frame and
# then follows the live ``chat_token`` frames.
SSE_CHAT_TURN_STARTED_TYPE = "chat_turn_started"
SSE_CHAT_TOKEN_TYPE = "chat_token"
SSE_CHAT_TURN_DONE_TYPE = "chat_turn_done"
SSE_CHAT_TURN_ERROR_TYPE = "chat_turn_error"
SSE_CHAT_TURN_RESUME_TYPE = "chat_turn_resume"

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


ACTIVITY_KINDS: frozenset[str] = frozenset(
    {"tool_call", "tool_result", "thinking", "text"}
)


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


def autonomous_token_frame(token: str) -> dict[str, object]:
    """Build an ``autonomous_token`` frame for a single streamed token.

    Published during an autonomous background turn so a connected browser
    can render live progress — the same way the normal ``/chat`` SSE path
    fans out ``token`` frames.

    Returns a dict with shape::

        {
            "type": "autonomous_token",
            "token": <str>,
        }
    """
    return {"type": SSE_AUTONOMOUS_TOKEN_TYPE, "token": token}


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


def autonomous_state_frame(
    session_id: str,
    state: str,
    *,
    plan_text: str = "",
    auto_turn_count: int = 0,
    max_auto_turns: int = 0,
    session_color: str = "",
) -> dict[str, object]:
    """Build an ``autonomous_state`` frame for live session-list updates.

    Published by :class:`~robotsix_chat.autonomous.runner.AutonomousRunner`
    whenever an autonomous session transitions state, so the browser can
    update the session-row status, plan preview, and approve/reject buttons
    without polling.

    Returns a dict with shape::

        {
            "type": "autonomous_state",
            "session_id": <str>,
            "state": <"selecting_subject|awaiting_approval|executing|completed">,
            "plan_text": <str>,
            "auto_turn_count": <int>,
            "max_auto_turns": <int>,
            "session_color": <str>,
        }
    """
    return {
        "type": SSE_AUTONOMOUS_STATE_TYPE,
        "session_id": session_id,
        "state": state,
        "plan_text": plan_text,
        "auto_turn_count": auto_turn_count,
        "max_auto_turns": max_auto_turns,
        "session_color": session_color,
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
        # Buffer of the CURRENT in-flight foreground turn per session:
        # session_id -> (turn_id, [token, token, ...]).  Lets a browser that
        # subscribes mid-turn replay what has been emitted and then follow the
        # live tokens.  Populated by begin_turn/append_turn_token and cleared
        # by end_turn — at most one entry per session, so memory stays bounded.
        self._current_turn: dict[str, tuple[str, list[str]]] = {}

    def subscribe(self, session_id: str) -> asyncio.Queue[dict[str, object]]:
        """Create a fresh queue, add to *session_id*'s subscribers, return it.

        If a foreground turn is in flight for *session_id*, the queue is
        primed with a ``chat_turn_resume`` frame carrying the text emitted so
        far so the new subscriber re-attaches to the live turn.  Runs to
        completion between event-loop steps (no ``await``), so no token can
        interleave between registering the queue and priming it.
        """
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._subscribers[session_id].add(queue)
        current = self._current_turn.get(session_id)
        if current is not None:
            turn_id, parts = current
            queue.put_nowait(
                {
                    "type": SSE_CHAT_TURN_RESUME_TYPE,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "content": "".join(parts),
                }
            )
        return queue

    def begin_turn(self, session_id: str, turn_id: str) -> None:
        """Start buffering a foreground turn and announce it to subscribers."""
        self._current_turn[session_id] = (turn_id, [])
        self.publish(
            session_id,
            {
                "type": SSE_CHAT_TURN_STARTED_TYPE,
                "session_id": session_id,
                "turn_id": turn_id,
            },
        )

    def append_turn_token(self, session_id: str, turn_id: str, content: str) -> None:
        """Buffer *content* for the current turn and publish it as a token.

        A no-op mismatch guard: only the turn that :meth:`begin_turn`
        registered appends to the buffer, but the token is still published so
        an in-flight subscriber is never starved by a race.
        """
        current = self._current_turn.get(session_id)
        if current is not None and current[0] == turn_id:
            current[1].append(content)
        self.publish(
            session_id,
            {
                "type": SSE_CHAT_TOKEN_TYPE,
                "session_id": session_id,
                "turn_id": turn_id,
                "content": content,
            },
        )

    def end_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        timestamp: float | None = None,
        error: str | None = None,
    ) -> None:
        """Finish the current turn: clear its buffer and publish done/error."""
        current = self._current_turn.get(session_id)
        if current is not None and current[0] == turn_id:
            del self._current_turn[session_id]
        if error is not None:
            self.publish(
                session_id,
                {
                    "type": SSE_CHAT_TURN_ERROR_TYPE,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "message": error,
                },
            )
        else:
            self.publish(
                session_id,
                {
                    "type": SSE_CHAT_TURN_DONE_TYPE,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "timestamp": timestamp,
                },
            )

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

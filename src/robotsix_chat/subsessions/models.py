"""Data model for the unified subsession system.

A **subsession** is an agent run spawned from a chat session (or from
another subsession) that executes in the background:

* ``task`` — one-shot job: runs to completion and reports a summary back.
* ``periodic`` — re-runs its instructions on an interval until closed.
* ``user_chat`` — an agent-initiated side-chat with the user.

This module holds only enums and dataclasses — no asyncio, no imports
from ``robotsix_chat.chat`` — so every other subsession module (and the
event/frame layer) can depend on it without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "ACTIVE_STATUSES",
    "InboxMessage",
    "SubsessionCapacityError",
    "SubsessionDepthError",
    "SubsessionInfo",
    "SubsessionIntervalError",
    "SubsessionKind",
    "SubsessionLevelError",
    "SubsessionStatus",
    "TranscriptEntry",
]


class SubsessionKind(StrEnum):
    """What flavour of background work a subsession performs."""

    TASK = "task"
    PERIODIC = "periodic"
    USER_CHAT = "user_chat"


class SubsessionStatus(StrEnum):
    """Lifecycle status of a subsession."""

    RUNNING = "running"  # an agent turn is in flight
    WAITING = "waiting"  # idle, waiting for an inbox message (user_chat)
    SLEEPING = "sleeping"  # periodic, waiting for the next scheduled run
    CLOSED = "closed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # server restarted while the work was live


# Statuses that count against the concurrency cap and accept inbox messages.
ACTIVE_STATUSES = frozenset(
    {
        SubsessionStatus.RUNNING,
        SubsessionStatus.WAITING,
        SubsessionStatus.SLEEPING,
    }
)


class SubsessionCapacityError(RuntimeError):
    """Raised when the process-wide active-subsession cap is reached."""


class SubsessionDepthError(RuntimeError):
    """Raised when spawning would exceed the maximum nesting depth."""


class SubsessionIntervalError(ValueError):
    """Raised when a periodic interval is below the configured minimum."""


class SubsessionLevelError(ValueError):
    """Raised when the requested model level is invalid or unusable."""


class SubsessionPeriodicSpawnError(RuntimeError):
    """Raised when a periodic subsession attempts to spawn a periodic child."""


@dataclass
class TranscriptEntry:
    """One rendered line of a subsession's conversation transcript."""

    role: str  # "user" | "parent" | "assistant" | "system"
    text: str
    timestamp: float  # wall clock (time.time)

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-serialisable form used by the API and store."""
        return {"role": self.role, "text": self.text, "timestamp": self.timestamp}


@dataclass
class InboxMessage:
    """A message queued for delivery at the subsession's next turn boundary."""

    role: str  # "user" | "parent"
    text: str
    timestamp: float


@dataclass
class SubsessionInfo:
    """Full state of a single subsession (registry-owned, mutated in place)."""

    id: str
    kind: SubsessionKind
    owner_session_id: str  # root UI chat session — EventBus / REST scope key
    parent_id: str | None  # None → parent is the main chat session
    depth: int  # 1..max_depth (the main chat session is depth 0)
    title: str
    prompt: str  # initial, self-contained instructions
    model_level: int  # llmio capability level (1 = cheapest .. 4 = frontier)
    status: SubsessionStatus
    created_at: float
    last_activity_at: float
    # periodic-only fields:
    interval_seconds: float | None = None
    next_run_at: float | None = None  # wall clock, for the UI countdown
    include_previous_result: bool = False
    runs: int = 0
    max_runs: int | None = None
    last_result: str | None = None
    # run guard (persisted) — tracks which run numbers have been executed
    # so a duplicate worker cannot re-execute runs that already completed.
    completed_runs: set[int] = field(default_factory=set)
    # terminal fields:
    summary: str | None = None
    close_reason: str | None = None
    error: str | None = None
    transcript: list[TranscriptEntry] = field(default_factory=list)
    # Capped rolling window of (turn_input, reply) pairs, persisted so a
    # periodic subsession's worker can rebuild its agent-visible history
    # when resumed after a restart instead of starting blank — separate
    # from `transcript` (UI-facing, role-tagged, may omit the composed
    # periodic turn_input) since this needs the exact text the model saw.
    turn_history: list[tuple[str, str]] = field(default_factory=list)
    # Task-specific checkpoint data persisted across restarts — for ticket
    # monitors this carries the watched ticket_id, last-known state, and a
    # consecutive-failures counter so recovery can decide whether to resume
    # the monitoring loop or close the subsession.
    checkpoint: dict[str, object] | None = None

    def snapshot(self, *, with_transcript: bool = False) -> dict[str, object]:
        """Return a JSON-serialisable snapshot for SSE frames and REST bodies.

        The transcript is omitted by default (it can be large); pass
        ``with_transcript=True`` for the single-subsession detail endpoint.
        """
        data: dict[str, object] = {
            "subsession_id": self.id,
            "kind": self.kind.value,
            "owner_session_id": self.owner_session_id,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "title": self.title,
            "prompt": self.prompt,
            "model_level": self.model_level,
            "status": self.status.value,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "interval_seconds": self.interval_seconds,
            "next_run_at": self.next_run_at,
            "include_previous_result": self.include_previous_result,
            "runs": self.runs,
            "max_runs": self.max_runs,
            "last_result": self.last_result,
            "summary": self.summary,
            "close_reason": self.close_reason,
            "error": self.error,
            "completed_runs": sorted(self.completed_runs),
            "turn_history": [list(pair) for pair in self.turn_history],
            "checkpoint": self.checkpoint,
        }
        if with_transcript:
            data["transcript"] = [entry.as_dict() for entry in self.transcript]
        return data

    @property
    def is_active(self) -> bool:
        """Whether the subsession still counts against the concurrency cap."""
        return self.status in ACTIVE_STATUSES

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

#: llmio capability level at or above which a subsession runs on the costly
#: frontier level (level 3 — fable on the default slot). Every level rides
#: the flat-rate subscription in normal operation, but frontier headroom
#: against the Claude weekly cap is the scarce resource (and under provider
#: failover it bills real tokens).  Agents built at this level are told to
#: orchestrate — delegate bulk reading/extraction to cheaper child
#: subsessions — rather than burn frontier-model turns on it (see
#: ``create_agent_from_settings``).
COSTLY_TIER_MIN_LEVEL = 3

#: System-prompt directive appended to a subsession agent built at
#: :data:`COSTLY_TIER_MIN_LEVEL` or above so that proper orchestration is the
#: default rather than requiring human steering.
COSTLY_TIER_ORCHESTRATION_DIRECTIVE = (
    "\n\n## Costly-tier orchestration\n\n"
    "You are running at a costly model tier (frontier Claude); your own "
    "turns are the most expensive resource in the fleet. Orchestrate rather "
    "than do bulk work yourself: delegate large reads and mechanical "
    "extraction (reading long traces, scanning source files, collecting "
    "logs) to level-1/2 child subsessions via spawn_subsession, and reserve "
    "your own turns for decomposition and synthesis. When you spawn a child "
    "for reading/extraction, leave model_level unset so it defaults to a "
    "cheap tier; only raise model_level when the subtask genuinely needs "
    "reasoning. Keep each delegated child task bounded and well-scoped so "
    "the fan-out stays cheap."
)

__all__ = [
    "ACTIVE_STATUSES",
    "COSTLY_TIER_MIN_LEVEL",
    "COSTLY_TIER_ORCHESTRATION_DIRECTIVE",
    "InboxMessage",
    "SubsessionAnchorError",
    "SubsessionCapacityError",
    "SubsessionDepthError",
    "SubsessionInfo",
    "SubsessionIntervalError",
    "SubsessionKind",
    "SubsessionLevelError",
    "SubsessionNoChangeThresholdError",
    "SubsessionPeriodicSpawnError",
    "SubsessionStatus",
    "SubsessionUserChatSpawnError",
    "SubsessionWaitForEventSpawnError",
    "TranscriptEntry",
]


class SubsessionKind(StrEnum):
    """What flavour of background work a subsession performs."""

    TASK = "task"
    PERIODIC = "periodic"
    WAIT_FOR_EVENT = "wait_for_event"
    USER_CHAT = "user_chat"
    ON_CLOSE = "on_close"


class SubsessionStatus(StrEnum):
    """Lifecycle status of a subsession."""

    RUNNING = "running"  # an agent turn is in flight
    WAITING = "waiting"  # idle, waiting for an inbox message (user_chat)
    SLEEPING = "sleeping"  # periodic, waiting for the next scheduled run
    PAUSED = "paused"  # periodic, auto-paused by idle-guard — retains worker
    CLOSED = "closed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # server restarted while the work was live


# Statuses that count against the concurrency cap and accept inbox messages.
# PAUSED is active (its worker is alive and can receive inbox messages) but
# is excluded from the concurrency cap by ``count_active`` — see note there.
ACTIVE_STATUSES = frozenset(
    {
        SubsessionStatus.RUNNING,
        SubsessionStatus.WAITING,
        SubsessionStatus.SLEEPING,
        SubsessionStatus.PAUSED,
    }
)


class SubsessionCapacityError(RuntimeError):
    """Raised when the process-wide active-subsession cap is reached."""


class SubsessionDepthError(RuntimeError):
    """Raised when spawning would exceed the maximum nesting depth."""


class SubsessionIntervalError(ValueError):
    """Raised when a periodic interval is below the configured minimum."""


class SubsessionAnchorError(ValueError):
    """Raised when a periodic ``anchor_time`` spec is malformed.

    Covers a bad shape (not ``HH:MM[:SS] [tz]``), an out-of-range field, or
    an unknown timezone.  The tool layer maps it to a polite refusal.
    """


class SubsessionNoChangeThresholdError(ValueError):
    """Raised when a per-spawn no-change threshold is below 1."""


class SubsessionLevelError(ValueError):
    """Raised when the requested model level is invalid or unusable."""


class SubsessionPeriodicSpawnError(RuntimeError):
    """Raised when a periodic or wait_for_event subsession.

    attempts to spawn a periodic or wait_for_event child.
    """


class SubsessionWaitForEventSpawnError(RuntimeError):
    """Raised when a wait_for_event subsession attempts to.

    spawn a periodic or wait_for_event child.
    """


class SubsessionUserChatSpawnError(RuntimeError):
    """Raised when a user_chat subsession attempts to spawn a user_chat child."""


class SubsessionDedupError(RuntimeError):
    """Raised by ``SubsessionRegistry.create`` when *dedup_key* is already active.

    Carries the *existing_id* of the active subsession so the caller can
    return it instead of creating a duplicate.
    """

    def __init__(self, existing_id: str) -> None:
        super().__init__(f"dedup_key already active for subsession {existing_id}")
        self.existing_id = existing_id


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

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-serialisable form used by the persistence store."""
        return {"role": self.role, "text": self.text, "timestamp": self.timestamp}


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
    model_level: int  # llmio capability level (1 cheap .. 3 frontier)
    status: SubsessionStatus
    created_at: float
    last_activity_at: float
    # periodic-only fields:
    interval_seconds: float | None = None
    # Optional absolute wall-clock anchor for the recurrence, e.g.
    # "09:00" or "09:00 Europe/Paris" (default timezone UTC).  When set,
    # the scheduler pins each recurrence to the next occurrence of this
    # time-of-day phase-aligned to interval_seconds instead of
    # ``now + interval`` — eliminating cumulative drift.  None → the
    # legacy relative-interval behaviour.  See ``schedule.py``.
    anchor_time: str | None = None
    next_run_at: float | None = None  # wall clock, for the UI countdown
    include_previous_result: bool = False
    runs: int = 0
    max_runs: int | None = None
    last_result: str | None = None
    # run guard (persisted) — tracks which run numbers have been executed
    # so a duplicate worker cannot re-execute runs that already completed.
    completed_runs: set[int] = field(default_factory=set)
    # wait_for_event-only fields:
    event_timeout_seconds: float | None = None
    # Per-subsession hard per-run timeout override (seconds) — overrides the
    # global subsessions.run_timeout_seconds for this subsession's agent
    # turns.  None → fall back to the global default (600 s).  Set via the
    # spawn_subsession tool's run_timeout_seconds argument (capped at
    # subsessions.max_run_timeout_seconds); persisted so it survives resume.
    run_timeout_seconds: float | None = None
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
    # Number of consecutive NO_CHANGE replies — persisted so a periodic
    # monitor's counter survives server restarts and the auto-stop /
    # auto-pause thresholds are not defeated by process restarts.
    consecutive_no_change: int = 0
    # Number of consecutive runs that ended in an error (tool-retry
    # exhaustion, transient-error exhaustion, timeout, or any other
    # run-level failure).  Persisted so the consecutive-error fail
    # threshold survives server restarts.  Reset to 0 on any successful
    # run.
    consecutive_errored_runs: int = 0
    # Global-issue deduplication key — when set on a user_chat subsession,
    # spawn_subsession refuses to create another user_chat with the same key
    # while this one is active, so a single root-cause error (e.g. an
    # asyncio.run crash affecting multiple ticket monitors) produces only
    # one side-chat instead of a flood of duplicate notifications.
    dedup_key: str | None = None
    # Ticket ID of a pre-requisite ticket whose monitor must complete
    # before this monitor should proceed.  When set, if the pre-requisite
    # monitor closes without the pre-requisite reaching a terminal state,
    # this subsession is paused (closed with reason
    # ``waiting_for_prerequisite``) and the watcher polls the
    # pre-requisite until it resolves — at which point the monitor is
    # reopened.
    depends_on_ticket_id: str | None = None
    # Retry counter for user_chat / task subsessions that fail with a
    # recoverable error.  Persisted so the retry budget survives restarts.
    retry_count: int = 0
    # The last formatted error message, carried forward into the retry
    # prompt so the agent knows what went wrong on the prior attempt.
    _last_error: str | None = None

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
            "anchor_time": self.anchor_time,
            "next_run_at": self.next_run_at,
            "include_previous_result": self.include_previous_result,
            "runs": self.runs,
            "max_runs": self.max_runs,
            "last_result": self.last_result,
            "summary": self.summary,
            "close_reason": self.close_reason,
            "error": self.error,
            "completed_runs": sorted(self.completed_runs),
            "event_timeout_seconds": self.event_timeout_seconds,
            "run_timeout_seconds": self.run_timeout_seconds,
            "turn_history": [list(pair) for pair in self.turn_history],
            "checkpoint": self.checkpoint,
            "dedup_key": self.dedup_key,
            "depends_on_ticket_id": self.depends_on_ticket_id,
            "consecutive_no_change": self.consecutive_no_change,
            "consecutive_errored_runs": self.consecutive_errored_runs,
            "retry_count": self.retry_count,
        }
        if with_transcript:
            data["transcript"] = [entry.as_dict() for entry in self.transcript]
        return data

    @property
    def is_active(self) -> bool:
        """Whether the subsession still counts against the concurrency cap."""
        return self.status in ACTIVE_STATUSES

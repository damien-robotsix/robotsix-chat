"""Periodic session settings models.

A periodic session is a NORMAL chat session that a scheduler starts on an
interval with one initial prompt. There is no execution state machine, no
self-scheduled continuation, and no restart-resume: the scheduler's whole job
is "when due, create a session and post the preset's initial prompt through
the same path an operator message takes". Anything that happens after that is
ordinary session behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Default spacing between runs of a preset. One day: these are digest-style
# jobs (mail triage, cost review), and anything that needs to react faster
# belongs in a subsession monitor, not a periodic session.
DEFAULT_SCHEDULE_INTERVAL_SECONDS = 86400.0

# Floor guarding against config typos: a 0-second interval would create a
# session storm (one fresh session + LLM turn per scheduler tick).
MIN_SCHEDULE_INTERVAL_SECONDS = 300.0


class PeriodicSessionDefinition(BaseModel):
    """One named periodic session preset.

    Attributes:
        name: Unique identifier for the preset; also used in session titles
            (``"<name> — <date>"``).
        initial_prompt: The one message the scheduler posts into the freshly
            created session. Write it like a complete task brief: what to do,
            what the scope is, hard constraints (e.g. read-only), and what
            the final report should contain. The scheduler prepends a short
            shared preamble (see ``robotsix_chat.periodic.prompts``).
        schedule_interval_seconds: Spacing between runs, measured from the
            last time the preset fired. Default one day.
        anchor_utc: Optional fixed UTC instant anchoring the schedule. When
            set, the preset fires at this instant and then every
            ``schedule_interval_seconds`` thereafter (e.g. an anchor of
            ``2026-09-03T06:00:00Z`` with a 24h interval fires daily at
            06:00 UTC). This pins a deterministic daily reference time
            instead of deriving it from first-registration time. ``None``
            keeps the legacy behaviour: the first run fires promptly after
            startup, then spaced by ``schedule_interval_seconds`` from the
            last firing.
        model_level: llmio capability level for this preset's sessions
            (1 cheap … 3 frontier). ``None`` uses the global
            ``chat_default_model_level`` resolution, exactly like an operator
            session.
        enabled: When ``False`` the preset never fires.

    """

    model_config = ConfigDict(extra="forbid")

    name: str
    initial_prompt: str = ""
    schedule_interval_seconds: float = Field(
        default=DEFAULT_SCHEDULE_INTERVAL_SECONDS,
        ge=MIN_SCHEDULE_INTERVAL_SECONDS,
    )
    anchor_utc: datetime | None = Field(
        default=None,
        description=(
            "Optional fixed UTC instant anchoring the schedule. When set, "
            "the preset fires at this instant and then every "
            "schedule_interval_seconds thereafter (e.g. an anchor of "
            "'2026-09-03T06:00:00Z' with a 24h interval fires daily at "
            "06:00 UTC). Unset keeps the legacy behaviour: first run "
            "promptly after startup, then spaced by "
            "schedule_interval_seconds from the last firing."
        ),
    )
    model_level: int | None = Field(
        default=None,
        ge=1,
        le=3,
        description=(
            "llmio capability level for this preset's sessions (1 cheap … "
            "3 frontier). None follows the global model-level resolution."
        ),
    )
    enabled: bool = True

    @field_validator("anchor_utc", mode="after")
    @classmethod
    def _normalise_anchor_utc(cls, v: datetime | None) -> datetime | None:
        """Enforce the field's 'UTC' contract regardless of input form.

        A naive datetime would otherwise be interpreted by
        ``datetime.timestamp()`` in the scheduler in the HOST's local
        timezone, silently shifting the anchor. Normalising here — naive
        means UTC, and any other offset is converted to UTC — makes the
        stored value an explicit UTC instant.
        """
        if v is None:
            return v
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v.astimezone(UTC)


class PeriodicSettings(BaseModel):
    """Periodic chat sessions — scheduler presets plus board-tool thresholds.

    Attributes:
        sessions: The preset list. An empty list simply means nothing fires.
        ready_staleness_minutes: Minutes a ticket may sit in ``ready`` before
            ``list_stale_ready_tickets`` reports it (board-tool threshold;
            historically configured alongside the scheduled sessions that
            use it, kept here).
        priority_ready_staleness_minutes: The same threshold for
            priority-flagged tickets, which legitimately wait longer in a
            serial implementation queue.

    """

    model_config = ConfigDict(extra="forbid")

    sessions: list[PeriodicSessionDefinition] = Field(
        default_factory=list,
        description=(
            "Named periodic session presets. Each fires on its own interval, "
            "creating a fresh ordinary session seeded with its "
            "initial_prompt."
        ),
    )
    ready_staleness_minutes: int = Field(
        default=10,
        ge=1,
        description=(
            "Minutes a ticket can remain in the ``ready`` state before "
            "``list_stale_ready_tickets`` surfaces it as stale."
        ),
    )
    priority_ready_staleness_minutes: int = Field(
        default=60,
        ge=1,
        description=(
            "Minutes a priority-flagged ticket can remain ``ready`` before "
            "it is considered stale (longer than the normal threshold: "
            "priority tickets often wait in a serial implementation queue)."
        ),
    )

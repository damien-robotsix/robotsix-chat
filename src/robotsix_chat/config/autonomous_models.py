"""Autonomous Settings Models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Delay between one autonomous run completing and the next starting, when a
# preset does not set its own.  One hour: frequent enough for a "continuous"
# preset to feel live, far enough apart that a restart storm cannot turn it
# into a spin loop (the old 45 s default did exactly that).
DEFAULT_TRIGGER_INTERVAL_SECONDS = 3600.0

# What the retired ``on_close`` trigger becomes on load.  Presets written
# under the old model carry a placeholder ``trigger_interval_seconds`` that
# the runner *ignored* (45 s on the shipped default), so honouring it now
# would restart the session every 45 s.  Adopt the standard interval instead.
_RETIRED_CONTINUOUS_TRIGGER = "on_close"


class AutonomousSessionDefinition(BaseModel):
    """Definition of one named autonomous session.

    Each definition maps to one autonomous session owner (``autonomous:<name>``
    when the preset name is not ``"default"``, otherwise the bare
    ``autonomous`` pseudo-owner).  The runner respects per-definition prompts,
    trigger type, and the enabled flag independently.

    Attributes:
        name: Unique identifier for this session definition.
        prompt: Custom kickoff prompt appended to the autonomous protocol
            supplement.  When empty, the agent uses the standard "Begin a new
            autonomous session and work it to completion" prompt.
        trigger_interval_seconds: Delay between one run completing and the
            next starting.  Every preset is periodic — there is exactly one
            scheduling model — so a "continuous" preset is just a short
            interval.  Default 1 h.
        enabled: When ``False``, the definition is skipped — no session is
            created for it.
        self_refine: When ``True``, after each run completes an LLM
            refinement step proposes an updated prompt addendum that folds
            in the run's feedback.  The next run uses the refined prompt.
            Default ``False`` (static presets keep running verbatim).
        self_refine_require_approval: When ``True``, refinements enter
            ``pending`` state and require operator approval before they
            take effect.  When ``False``, refinements are auto-accepted.
            Default ``False``.

    """

    name: str
    prompt: str = ""
    trigger_interval_seconds: float = Field(
        default=DEFAULT_TRIGGER_INTERVAL_SECONDS, ge=0.0
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_retired_trigger_type(cls, data: Any) -> Any:
        """Drop the retired ``trigger_type`` key, rescuing the interval.

        Presets used to choose between ``"periodic"`` and ``"on_close"``.
        There is now one scheduling model — every preset waits
        ``trigger_interval_seconds`` — so the key is removed rather than
        kept as a single-valued enum.  ``extra="forbid"`` means a stored
        config carrying it would fail to load, so strip it here.

        ``on_close`` presets need their interval replaced, not kept: the
        runner ignored ``trigger_interval_seconds`` for them, so the stored
        value is a meaningless placeholder (45 s on the shipped default) that
        would now fire the session every 45 seconds.
        """
        if not isinstance(data, dict):
            return data
        if "trigger_type" not in data:
            return data
        data = dict(data)
        was_continuous = str(data.pop("trigger_type", "")) == (
            _RETIRED_CONTINUOUS_TRIGGER
        )
        if was_continuous:
            data["trigger_interval_seconds"] = DEFAULT_TRIGGER_INTERVAL_SECONDS
        return data

    max_auto_turns: int = Field(
        default=20,
        description=(
            "Retained for config compatibility. No longer used — autonomous "
            "runs receive a single prompt, so there is no per-run turn cap."
        ),
    )
    model_level: int | None = Field(
        default=None,
        description=(
            "llmio capability level for this autonomous session (1 cheapest "
            "… 4 frontier).  When ``None`` (the default), the session uses "
            "the global ``llmio_model_level``.  Set per-preset to run "
            "cheap monitors on level 1-2 while keeping frontier work on "
            "level 3-4."
        ),
    )
    max_runs: int = Field(
        default=0,
        ge=0,
        description=(
            "Maximum number of times this preset may fire.  After the "
            "preset has completed *max_runs* runs it is automatically "
            "disabled — no further sessions are created for it.  ``0`` "
            "(the default) means unlimited."
        ),
    )
    enabled: bool = True
    self_refine: bool = False
    self_refine_require_approval: bool = False
    model_config = ConfigDict(extra="forbid")


class AutonomousSettings(BaseModel):
    """Native autonomous chat sessions — self-directed agent loops.

    Autonomous sessions are defined entirely through the ``sessions`` presets
    list.  Each preset carries its own prompt, trigger type, max turns, and
    enabled flag — there are no legacy single-session keys.

    The built-in default preset ``{"name": "default"}`` ships in the schema
    defaults (the field default) and in the committed config template so it
    is always visible in the UI.  The runner reads only the configured
    presets list — there is no hidden or implicit fallback session.

    Attributes:
        completion_marker: Marker string the agent emits when the run is
            complete.  The session closes automatically on completion.
        continue_interval_seconds: Retained for config compatibility.  No
            longer used — autonomous runs receive a single prompt, so there
            is no auto-continue pacing loop.
        max_idle_auto_turns: Retained for config compatibility.  No longer
            used — there is no auto-continue loop, so there is no idle turn
            cap.
        stale_monitor_runs_before_completion: Number of consecutive NO_CHANGE
            cycles after which a periodic monitor is considered 'stale'.
        queue_tolerance_runs_before_escalation: Number of consecutive
            NO_CHANGE monitor cycles to accept as queue wait before
            escalating a possible stall on a serial board.
        sessions: List of named autonomous session definitions.  An explicit
            empty list is migrated to the built-in default preset on load so
            the default session is always surfaced.  Each entry defines a
            prompt, trigger, max turns, and enabled flag for one autonomous
            session.

    """

    completion_marker: str = "---AUTONOMOUS COMPLETE---"
    continue_interval_seconds: float = Field(
        default=45.0,
        description=(
            "Retained for config compatibility. No longer used — autonomous "
            "runs receive a single prompt, so there is no auto-continue "
            "pacing loop."
        ),
    )
    max_idle_auto_turns: int = Field(
        default=5,
        description=(
            "Retained for config compatibility. No longer used — there is no "
            "auto-continue loop, so there is no idle turn cap."
        ),
    )
    stale_monitor_runs_before_completion: int = Field(
        default=3,
        description=(
            "Number of consecutive NO_CHANGE cycles after which a periodic "
            "monitor is considered 'stale' — the agent may declare the "
            "autonomous session complete even while the monitor is still "
            "running.  Monitors continue in the background.  "
            "Env override: ``AUTONOMOUS_STALE_MONITOR_RUNS_BEFORE_COMPLETION``."
        ),
    )
    queue_tolerance_runs_before_escalation: int = Field(
        default=3,
        description=(
            "Number of consecutive NO_CHANGE monitor cycles to accept as "
            "queue wait before escalating a possible stall when the board "
            "processes tickets serially.  During this window the agent "
            "checks whether earlier-queued tickets are actively being "
            "worked before auto-stopping a monitor or reporting a stall."
        ),
    )
    ready_staleness_minutes: int = Field(
        default=10,
        ge=1,
        description=(
            "Number of minutes a ticket can remain in the ``ready`` state "
            "before it is considered stale.  The ``list_stale_ready_tickets`` "
            "tool surfaces tickets that have been sitting in ``ready`` "
            "longer than this threshold, enabling the agent to detect queue "
            "stalls and escalate or nudge the operator."
        ),
    )
    priority_ready_staleness_minutes: int = Field(
        default=60,
        ge=1,
        description=(
            "Number of minutes a priority-flagged ticket can remain in the "
            "``ready`` state before it is considered stale.  Priority tickets "
            "get a longer grace period than non-priority tickets because they "
            "are often waiting in a serial implementation queue rather than "
            "being truly stalled.  The ``list_stale_ready_tickets`` tool uses "
            "this threshold for tickets where ``priority`` or ``flagged`` is "
            "``True``."
        ),
    )
    sessions: list[AutonomousSessionDefinition] = Field(
        default_factory=lambda: [AutonomousSessionDefinition(name="default")],
        description=(
            "Named autonomous session definitions.  The built-in default "
            'preset ``{"name": "default"}`` ships in the schema defaults '
            "and in the committed config template so it is always visible.  "
            "An explicit empty list is migrated to the built-in default "
            "preset on load so the default session is always surfaced.  "
            "Each entry defines a prompt, trigger, max turns, and enabled "
            "flag for one autonomous session."
        ),
    )
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_autonomous_keys(cls, data: Any) -> Any:
        """Strip removed single-session keys and relocate ``max_auto_turns``.

        Legacy keys ``enabled``, ``initial_task``, ``session_color``,
        ``persist_path``, ``pending_subsession_wait_timeout``,
        ``proposal_marker``, and ``approval_marker`` are stripped silently —
        they have no equivalent in the current model.

        The built-in default preset (``{"name": "default"}``) is now carried
        in the ``sessions`` field default (schema default), not injected here.
        Existing deployments that lack a ``sessions`` key receive the default
        from the field default; an explicit empty ``sessions`` list is
        migrated to the built-in default preset so older on-disk configs
        (which serialized ``sessions: []``) still surface the default
        session.

        The global ``max_auto_turns`` value is migrated into every session
        preset that does not already define its own ``max_auto_turns``,
        then the global key is removed.
        """
        if not isinstance(data, dict):
            return data

        _stripped_keys = (
            "enabled",
            "initial_task",
            "session_color",
            "persist_path",
            "pending_subsession_wait_timeout",
            "proposal_marker",
            "approval_marker",
        )
        for key in _stripped_keys:
            data.pop(key, None)

        # Migrate global max_auto_turns into each preset that lacks it.
        legacy_max_turns = data.pop("max_auto_turns", None)
        if legacy_max_turns is not None and isinstance(data.get("sessions"), list):
            for preset in data["sessions"]:
                if isinstance(preset, dict) and "max_auto_turns" not in preset:
                    preset["max_auto_turns"] = legacy_max_turns

        # Older on-disk configs serialized ``sessions: []`` explicitly, which
        # overrides the field's schema default and hides the default preset.
        # Replace the explicit empty list with the built-in default preset.
        if data.get("sessions") == []:
            data["sessions"] = [{"name": "default"}]

        return data

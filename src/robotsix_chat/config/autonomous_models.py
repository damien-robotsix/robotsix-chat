"""Autonomous Settings Models."""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TriggerType(enum.StrEnum):
    """How an autonomous session is re-triggered after completion."""

    periodic = "periodic"
    """Wait ``trigger_interval_seconds``, then restart."""

    on_close = "on_close"
    """Restart immediately when the previous run completes (continuous mode)."""


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
        trigger_type: How the session is re-triggered after completion —
            ``"periodic"`` (wait ``trigger_interval_seconds``) or
            ``"on_close"`` (restart immediately, continuous mode).
        trigger_interval_seconds: Delay between completion and restart for
            ``periodic`` trigger.  Ignored for ``on_close``.  Default 45 s.
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
    trigger_type: TriggerType = TriggerType.periodic
    trigger_interval_seconds: float = Field(default=45.0, ge=0.0)
    max_auto_turns: int = Field(
        default=20,
        description=(
            "Retained for config compatibility. No longer used — autonomous "
            "runs receive a single prompt, so there is no per-run turn cap."
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


class AutonomySettings(BaseModel):
    """Operator-configurable autonomy tier for reducing interruptions.

    When enabled, the chat agent can self-authorize certain low-risk
    actions that would normally require operator approval.  The default
    is conservative — every action is gated so behaviour only changes
    when the operator explicitly opts in.

    Even at the highest tier these actions remain HARD-GATED:
    merges touching ``.github/workflows/**``, ``secrets/**``, ``.env*``
    or any security-sensitive path; deletions of tracked files or
    directories; priority/scope changes with broad blast radius;
    ambiguous or novel mutation types; and any action whose safety the
    agent cannot independently verify.

    Attributes:
        auto_approve_self_authored: When ``True``, the agent may
            auto-approve ``human_issue_approval`` tickets that it
            (or a chat-agent feedback source) authored, provided the
            target repo is in ``auto_approve_repo_allowlist`` and the
            change is non-destructive / reversible.
        auto_approve_repo_allowlist: Repository names (e.g.
            ``"robotsix-chat"``) eligible for auto-approval when
            ``auto_approve_self_authored`` is enabled.  Tickets
            targeting repos not listed here are always gated.
        auto_approve_routine_secret_provisioning: When ``True``, the
            agent may auto-approve routine secret provisioning
            ``human_issue_approval`` tickets even when they touch
            security-sensitive paths (``secrets/**``, ``credentials``),
            provided the change has no code modifications, no
            destructive operations, and is limited to
            credential/secret/token provisioning.  This covers standard
            operations like adding API keys, rotating credentials, or
            provisioning access tokens where the agent's own assessment
            confirms the change is routine and non-destructive.
        suppress_no_change_monitors: When ``True``, periodic and event
            monitor outcomes that carry no actionable delta
            (NO_CHANGE, completed normally, auto-paused) do not
            generate an operator-facing turn.  Only blockers, decisions
            that fail auto-approval criteria, and terminal failures
            are surfaced.

    """

    model_config = ConfigDict(extra="forbid")

    auto_approve_self_authored: bool = Field(
        default=False,
        description=(
            "When True, auto-approve self-authored human_issue_approval "
            "tickets for repos in the allowlist."
        ),
    )
    auto_approve_repo_allowlist: list[str] = Field(
        default_factory=list,
        description=(
            "Repos eligible for auto-approval when "
            "auto_approve_self_authored is enabled."
        ),
    )
    auto_approve_routine_secret_provisioning: bool = Field(
        default=False,
        description=(
            "When True, the agent may auto-approve routine secret "
            "provisioning human_issue_approval tickets even when they "
            "touch security-sensitive paths — provided the change has "
            "no code modifications, no destructive operations, and is "
            "limited to credential/secret/token provisioning."
        ),
    )
    suppress_no_change_monitors: bool = Field(
        default=False,
        description=(
            "When True, suppress operator-facing turns for monitor "
            "outcomes with no actionable delta."
        ),
    )
    auto_self_restart: bool = Field(
        default=False,
        description=(
            "When True, the agent may call self_restart without operator "
            "approval after deploying capability changes (code changes, "
            "component roster updates) that affect the agent's own "
            "behaviour.  The agent must announce the restart with a brief "
            "delay (e.g. 30 seconds) so the operator can interrupt if "
            "needed.  Self-restart for any other reason still requires "
            "explicit operator authorization."
        ),
    )

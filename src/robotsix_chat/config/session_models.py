"""Session Settings Models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class KindTurnBudget(BaseModel):
    """Per-subsession-kind agent-turn budget.

    Each subsession kind (task, periodic, user_chat, on_close) carries its
    own soft-warn and hard-stop turn thresholds.  A subsession that exceeds
    these limits is either nudged to wrap up (soft warn) or force-closed
    with a partial-work summary (hard stop).

    Attributes:
        soft_warn_turns: Number of agent turns before the worker injects a
            system reminder telling the agent to wrap up and call
            ``complete_subsession``.  Default ``25``.
        hard_stop_turns: Number of agent turns before the worker force-closes
            the subsession with a summary of work-so-far.  Default ``40``.

    """

    soft_warn_turns: int = 25
    hard_stop_turns: int = 40

    @model_validator(mode="after")
    def _validate_ordering(self) -> KindTurnBudget:
        if (
            self.soft_warn_turns > 0
            and self.hard_stop_turns > 0
            and self.soft_warn_turns >= self.hard_stop_turns
        ):
            raise ValueError(
                f"soft_warn_turns ({self.soft_warn_turns}) must be less "
                f"than hard_stop_turns ({self.hard_stop_turns})"
            )
        return self

    model_config = ConfigDict(extra="forbid")


class TurnBudgetSettings(BaseModel):
    """Per-kind turn budgets for subsession agents.

    Attributes:
        task: Budget for one-shot ``task`` subsessions.
        periodic: Budget for ``periodic`` monitor subsessions.  Defaults to
            ``0``/``0`` (disabled) — monitors are already bounded by
            ``monitor_max_model_level``, ``run_timeout_seconds``, and
            ``periodic_max_total_runs``, and are designed to stay alive for
            the whole life of a ticket, so a per-subsession turn ceiling
            would force-close otherwise-healthy long-lived monitors.
        user_chat: Budget for ``user_chat`` side-chat subsessions.
        on_close: Budget for ``on_close`` subsessions.

    """

    task: KindTurnBudget = Field(default_factory=KindTurnBudget)
    periodic: KindTurnBudget = Field(
        default_factory=lambda: KindTurnBudget(soft_warn_turns=0, hard_stop_turns=0)
    )
    user_chat: KindTurnBudget = Field(default_factory=KindTurnBudget)
    on_close: KindTurnBudget = Field(default_factory=KindTurnBudget)

    model_config = ConfigDict(extra="forbid")


class SubsessionsSettings(BaseModel):
    """Unified subsession system — background agents spawned from a chat.

    A subsession is a background agent run (``task``, ``periodic``, or
    ``user_chat``) spawned by the main chat agent — or, nested, by another
    subsession — with its own model level chosen by task difficulty.

    Attributes:
        max_concurrent: Process-wide cap on simultaneously active
            subsessions (all kinds, all depths).
            Env override: ``SUBSESSIONS_MAX_CONCURRENT``.
        max_concurrent_per_session: Per-session cap on simultaneously
            active subsessions owned by a single chat session.  When a
            session reaches this limit, new spawns are rejected even if
            the global pool has room.  Set to ``0`` to disable (no
            per-session limit).  Env override:
            ``SUBSESSIONS_MAX_CONCURRENT_PER_SESSION``.
        stale_reclaim_seconds: When the global pool
            (``max_concurrent``) is full but the spawning session is
            under its per-session limit, SLEEPING or PAUSED subsessions
            owned by **other** sessions that have been idle for longer
            than this many seconds are eligible for reclamation — the
            stalest is closed to free a slot for the new spawn.
            SLEEPING subsessions are preferred over PAUSED because they
            count against the global capacity cap; reclaiming one
            actually frees a slot.  Set to ``0`` to disable reclamation.
            Env override:
            ``SUBSESSIONS_STALE_RECLAIM_SECONDS``.
        max_depth: Maximum nesting depth.  The main chat session is depth
            0; its subsessions are depth 1.  Agents at ``max_depth`` get
            no spawn tools.  Env override: ``SUBSESSIONS_MAX_DEPTH``.
        default_model_level: llmio capability level used when the
            spawning agent does not pick one explicitly (1 cheapest … 4
            frontier).  Env override: ``SUBSESSIONS_DEFAULT_MODEL_LEVEL``.
        monitor_max_model_level: Maximum model level for periodic and
            wait_for_event monitor subsessions.  Routine monitors
            (ticket polling, periodic checks) are capped at this level
            to prevent them from burning expensive keyless Claude
            subscription tiers.  Levels 1-2 use OpenRouter (cheap real
            $); levels 3-4 use keyless Claude SDK (subscription cap).
            Default ``2``.  Set to ``4`` to remove the cap.
            Env override: ``SUBSESSIONS_MONITOR_MAX_MODEL_LEVEL``.
        min_interval_seconds: Minimum interval for ``periodic``
            subsessions.  Env override: ``SUBSESSIONS_MIN_INTERVAL_SECONDS``.
        auto_stop_no_change_runs: A periodic subsession auto-closes after
            this many consecutive ``NO_CHANGE`` runs.
            Env override: ``SUBSESSIONS_AUTO_STOP_NO_CHANGE_RUNS``.
        max_idle_runs: A periodic subsession auto-pauses (closes with
            reason ``"paused"``) after this many consecutive
            ``NO_CHANGE`` runs.  Set to ``0`` to disable.
            Default ``15``.
            Env override: ``SUBSESSIONS_MAX_IDLE_RUNS``.
        max_no_change_pauses: A periodic monitor that repeatedly
            auto-pauses (and is then auto-resumed) without the monitored
            ticket ever changing state will auto-close with reason
            ``no_change_pause_limit`` after this many consecutive
            no-change pauses, instead of repeating the same pause message
            forever.  The counter resets when the monitor observes a real
            change.  Set to ``0`` to disable (always pause).
            Default ``3``.
            Env override: ``SUBSESSIONS_MAX_NO_CHANGE_PAUSES``.
        human_approval_timeout_runs: When a periodic subsession's checkpoint
            indicates the monitored ticket is in ``human_issue_approval``
            state, auto-escalate (close with reason
            ``human_approval_timeout``) after this many consecutive
            ``NO_CHANGE`` runs.  Default ``5``.
            Env override: ``SUBSESSIONS_HUMAN_APPROVAL_TIMEOUT_RUNS``.
        human_approval_timeout_seconds: Wall-clock backstop for the
            ``human_issue_approval`` stuck-ticket gate.  When the checkpoint
            has carried ``last_known_state='human_issue_approval'`` for
            longer than this many seconds, auto-escalate (close with reason
            ``human_approval_timeout``) even if the ``NO_CHANGE`` run count
            has not yet reached ``human_approval_timeout_runs``.  Default
            ``300`` (5 minutes).
            Env override: ``SUBSESSIONS_HUMAN_APPROVAL_TIMEOUT_SECONDS``.
        pre_authorized_ticket_patterns: Glob patterns (``fnmatch``) matching
            ticket IDs that are pre-authorized under a standing operator
            directive.  When a monitored ticket's ID matches any pattern,
            the ``human_issue_approval`` gate is bypassed — the system
            auto-escalates immediately (reason ``pre_authorized_approval``)
            instead of waiting for ``human_approval_timeout_runs``.
            Default ``[]``.
        auto_drive_promote_ready_drafts: Opt-in gate for the auto-drive
            monitor's promotable-draft branch.  When ``True`` and a
            monitored ticket is a promotable draft (state ``draft``,
            refine-complete spec, no open blocking review thread) whose
            ID matches ``pre_authorized_ticket_patterns``, the monitor
            transitions it into the ready queue.  When ``False`` (the
            default) the monitor never auto-promotes — it posts at most
            one operator-decision comment and waits.
            Default ``False``.
        run_timeout_seconds: Hard per-run timeout for a single subsession
            agent turn (recall + LLM call + delivery).  On expiry the run
            is marked failed and the schedule continues instead of staying
            ``running`` forever.  Default 600 s.
            Env override: ``SUBSESSIONS_RUN_TIMEOUT_SECONDS``.
        store_path: JSON persistence file (periodic subsessions resume
            across restarts).  Env override: ``SUBSESSIONS_STORE_PATH``.
        transcript_max_entries: Per-subsession transcript retention cap.
            Env override: ``SUBSESSIONS_TRANSCRIPT_MAX_ENTRIES``.
        mill_recovery_initial_backoff_seconds: Initial backoff (seconds)
            when a ticket monitor enters mill-recovery mode after
            consecutive failures.  Doubles on each retry up to
            *mill_recovery_max_backoff_seconds*.  Default ``60.0``.
            Env override: ``SUBSESSIONS_MILL_RECOVERY_INITIAL_BACKOFF_SECONDS``.
        mill_recovery_max_backoff_seconds: Maximum backoff (seconds) for
            mill-recovery retries.  Default ``3600.0`` (1 hour).
            Env override: ``SUBSESSIONS_MILL_RECOVERY_MAX_BACKOFF_SECONDS``.
        mill_recovery_max_retries: Maximum number of recovery retries
            before the subsession is permanently closed.  Default ``10``.
            Env override: ``SUBSESSIONS_MILL_RECOVERY_MAX_RETRIES``.
        periodic_max_interval_seconds: Upper bound (seconds) for a
            periodic subsession's self-adjusted interval.  The
            ``adjust_periodic_interval`` tool clamps to this value.
            Default ``3600.0`` (1 hour).
            Env override: ``SUBSESSIONS_PERIODIC_MAX_INTERVAL_SECONDS``.
        periodic_max_total_runs: Upper bound for a periodic subsession's
            self-adjusted ``max_runs`` (total run budget).  The
            ``adjust_periodic_budget`` tool clamps to this value.
            Default ``100``.
            Env override: ``SUBSESSIONS_PERIODIC_MAX_TOTAL_RUNS``.
        user_chat_max_retries: Maximum number of automatic retries for
            ``user_chat`` and ``task`` subsession failures.  Each retry
            re-launches the subsession with the prior error folded into
            the prompt so the agent can self-correct.  Once exhausted the
            subsession is failed and, for ``user_chat``, the original
            decision prompt is surfaced in the main conversation as a
            fallback so the operator can answer directly.  Default ``3``.
            Env override: ``SUBSESSIONS_USER_CHAT_MAX_RETRIES``.
        transient_error_max_retries: Maximum retry attempts when a
            periodic subsession's agent turn fails with a transient API
            error (e.g. upstream provider hiccup).  Retries use
            ``robotsix_http``'s exponential backoff with jitter; the
            delays themselves are not operator-configurable, only the
            attempt count.  When retries are exhausted the run is
            skipped and the schedule continues rather than permanently
            failing the subsession.
            Default ``3``.
            Env override: ``SUBSESSIONS_TRANSIENT_ERROR_MAX_RETRIES``.
        max_runs_escalation_threshold: Number of consecutive times a
            periodic subsession can hit its ``max_runs`` limit before
            auto-escalating with a follow-up ticket.  When the threshold
            is reached, a follow-up ticket is created on the board and
            the monitor closes with reason ``max_runs_escalated``.
            Set to ``0`` to disable escalation.  Default ``3``.
            Env override: ``SUBSESSIONS_MAX_RUNS_ESCALATION_THRESHOLD``.
        max_runs_progress_extension: Number of additional runs granted
            to a periodic subsession when it reaches its ``max_runs``
            cap but has observed progress within the recent window (a
            non-``NO_CHANGE``, non-duplicate reply — i.e. the agent
            acknowledged a state transition or reported activity).  The
            extended cap is clamped by ``periodic_max_total_runs``.
            Set to ``0`` to disable adaptive extension.  Default ``20``.
            Env override: ``SUBSESSIONS_MAX_RUNS_PROGRESS_EXTENSION``.
        max_runs_progress_window: Number of recent runs inspected for
            progress when a periodic subsession reaches its ``max_runs``
            cap.  A run counts as progress when the agent replies with
            something other than ``NO_CHANGE`` and other than a verbatim
            duplicate of the prior reply.  Set to ``0`` to disable
            adaptive extension.  Default ``5``.
            Env override: ``SUBSESSIONS_MAX_RUNS_PROGRESS_WINDOW``.
        monitor_slot_budget: Maximum number of occupied monitor slots per
            conversation (active + paused periodic subsessions).  When a
            conversation reaches this budget, new monitor requests first
            try to reuse the least-recently-active paused monitor; if
            none is paused the request is queued rather than evicting a
            live monitor.  Set to ``0`` to disable per-conversation slot
            budgeting (all spawns proceed immediately).
            Default ``8``.
            Env override: ``SUBSESSIONS_MONITOR_SLOT_BUDGET``.
        monitor_slot_queue_max: Maximum pending monitor-spawn requests
            queued per conversation when the slot budget is exhausted
            and no paused monitor is available for reuse.  A request
            that would exceed this limit is rejected with a clear error.
            Default ``32``.
            Env override: ``SUBSESSIONS_MONITOR_SLOT_QUEUE_MAX``.
        turn_budget: Per-kind agent-turn budget (soft-warn + hard-stop
            thresholds) that bounds a single subsession's turn count
            before force-closing it with a partial-work summary.  The
            hard stop prevents a looping subagent from consuming
            unbounded Claude subscription cap.
            Defaults: ``task``, ``user_chat``, and ``on_close`` warn at
            25 turns and hard-stop at 40 turns; ``periodic`` (and
            ``wait_for_event``) defaults to ``0``/``0`` (disabled) —
            monitors are already bounded by ``monitor_max_model_level``,
            ``run_timeout_seconds``, and ``periodic_max_total_runs``.
            See :class:`TurnBudgetSettings`.

    """

    max_concurrent: int = 8
    max_concurrent_per_session: int = Field(
        default=0,
        description=(
            "Per-session cap on simultaneously active subsessions "
            "owned by a single chat session.  When a session reaches "
            "this limit, new spawns are rejected even if the global "
            "pool has room.  Set to 0 to disable (no per-session "
            "limit)."
        ),
    )
    stale_reclaim_seconds: float = Field(
        default=0.0,
        description=(
            "When the global pool (max_concurrent) is full but the "
            "spawning session is under its per-session limit, SLEEPING "
            "or PAUSED subsessions owned by OTHER sessions that have "
            "been idle for longer than this many seconds are eligible "
            "for reclamation — the stalest is closed to free a slot "
            "for the new spawn.  SLEEPING subsessions are preferred "
            "over PAUSED because they count against the global capacity "
            "cap; reclaiming one actually frees a slot.  Set to 0 to "
            "disable reclamation."
        ),
    )
    max_depth: int = 3
    default_model_level: int = 3
    monitor_max_model_level: int = 2
    min_interval_seconds: float = 60.0
    auto_stop_no_change_runs: int = 3
    max_idle_runs: int = 15
    max_no_change_pauses: int = 3
    human_approval_timeout_runs: int = 5
    human_approval_timeout_seconds: float = 300.0
    pre_authorized_ticket_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Glob patterns (fnmatch) matching ticket IDs that are "
            "pre-authorized under a standing operator directive.  When a "
            "monitored ticket's ID matches any pattern, the "
            "human_issue_approval gate is bypassed — the system "
            "auto-escalates immediately (reason 'pre_authorized_approval') "
            "instead of waiting for human_approval_timeout_runs."
        ),
    )
    auto_drive_promote_ready_drafts: bool = Field(
        default=False,
        description=(
            "Opt-in gate for the auto-drive monitor's promotable-draft "
            "branch.  When a monitored ticket is a promotable draft "
            "(state 'draft', refine-complete spec, no open blocking "
            "review thread) and its ID matches a "
            "pre_authorized_ticket_patterns entry, the monitor "
            "transitions it into the ready queue instead of posting an "
            "operator-decision comment.  Default False — drafts are "
            "never auto-promoted without this opt-in."
        ),
    )
    run_timeout_seconds: float = 600.0
    store_path: str = "/data/subsessions.json"
    transcript_max_entries: int = 200
    mill_recovery_initial_backoff_seconds: float = 60.0
    mill_recovery_max_backoff_seconds: float = 3600.0
    mill_recovery_max_retries: int = 10
    paused_monitor_poll_interval_seconds: float = Field(
        default=60.0,
        description=(
            "Interval (seconds) between polls of paused periodic "
            "monitors.  The background watcher checks each paused "
            "monitor's ticket state via the mill API; when the "
            "ticket's state differs from the checkpoint's "
            "``last_known_state`` the monitor is reopened and "
            "re-spawned.  Set to ``0`` to disable runtime polling "
            "(paused monitors only resume on service restart)."
        ),
    )
    paused_monitor_long_poll_interval_seconds: float = Field(
        default=15.0,
        description=(
            "Interval (seconds) between direct mill API polls by a "
            "paused periodic monitor in its wait loop.  Each paused "
            "monitor polls the mill for its tracked ticket's state "
            "at this interval; when the state differs from the "
            "checkpoint's ``last_known_state`` the monitor resumes "
            "immediately (zero added latency).  The background "
            "watcher's ``paused_monitor_poll_interval_seconds`` "
            "(60 s default) serves as a safety-net backup.  Set to "
            "``0`` to disable per-monitor long-polling (watcher-only "
            "wake)."
        ),
    )
    paused_monitor_auto_resume_seconds: float = Field(
        default=1800.0,
        description=(
            "Maximum wall-clock seconds a paused periodic monitor "
            "remains paused before auto-resuming regardless of "
            "ticket-state changes.  When a monitor has been paused "
            "for longer than this interval (e.g. 1800 s = 30 min), "
            "it resumes its normal periodic cycle so the operator "
            "does not need to manually intervene.  Set to ``0`` to "
            "disable time-based auto-resume (monitor stays paused "
            "until a state change or manual message arrives)."
        ),
    )
    paused_monitor_max_reblock_resumes: int = Field(
        default=3,
        description=(
            "Maximum number of consecutive BLOCKED-on-resume events "
            "before a paused periodic monitor is closed with reason "
            "``repeated_blocked``.  When a ticket is BLOCKED on every "
            "resume (the agent keeps hitting the same failure without "
            "making progress), auto-retry is futile — the monitor is "
            "closed so the operator can intervene.  Default ``3``."
        ),
    )
    paused_monitor_reblock_notify_threshold: int = Field(
        default=2,
        description=(
            "Number of consecutive BLOCKED-on-resume events before an "
            "SSE notification is sent to the parent conversation "
            "alerting the operator that the monitor is re-blocking.  "
            "This surfaces silent auto-resume→re-block loops so the "
            "operator can decide whether to rebase the branch, revert "
            "problematic files, or take other action before the "
            "``paused_monitor_max_reblock_resumes`` cap is reached.  "
            "Set to ``0`` to disable notifications.  Default ``2``."
        ),
    )
    event_driven_timeout_seconds: float = Field(
        default=900.0,
        description=(
            "Default timeout (seconds) for wait-for-event subsessions. "
            "When no matching mill event arrives within this window, the "
            "monitor runs a safety-net turn to verify state via the board "
            "API in case an event was lost, then re-arms the wait."
        ),
    )
    periodic_max_interval_seconds: float = Field(
        default=3600.0,
        description=(
            "Upper bound (seconds) for a periodic subsession's "
            "self-adjusted interval.  The adjust_periodic_interval tool "
            "clamps to this value.  Default 3600 (1 hour)."
        ),
    )
    periodic_max_total_runs: int = Field(
        default=100,
        description=(
            "Upper bound for a periodic subsession's self-adjusted "
            "max_runs (total run budget).  The adjust_periodic_budget "
            "tool clamps to this value.  Default 100."
        ),
    )
    user_chat_max_retries: int = 3
    monitor_error_max_retries: int = Field(
        default=2,
        description=(
            "Maximum number of automatic retries for periodic and "
            "wait_for_event monitor subsessions that fail with a "
            "non-transient error (e.g. tool retry limit, unexpected "
            "exception).  Each retry re-launches the subsession worker "
            "with a system note about the prior failure so the agent "
            "can self-correct.  Once exhausted the monitor is "
            "permanently failed and the parent is notified.  Set to "
            "``0`` to disable monitor error retries (monitors fail "
            "on the first error, matching legacy behaviour).  "
            "Default ``2``.  "
            "Env override: ``SUBSESSIONS_MONITOR_ERROR_MAX_RETRIES``."
        ),
    )
    consecutive_error_fail_threshold: int = Field(
        default=3,
        description=(
            "Number of consecutive errored runs before a periodic or "
            "wait_for_event subsession is permanently failed.  A run "
            "is errored when it ends with a tool-retry exhaustion, "
            "transient-error exhaustion, timeout, or any other "
            "run-level failure.  The counter resets on any successful "
            "run.  The parent is notified at most once per error "
            "streak (when the streak begins).  Set to ``0`` to fail "
            "on the first errored run (legacy behaviour).  "
            "Default ``3``.  "
            "Env override: ``SUBSESSIONS_CONSECUTIVE_ERROR_FAIL_THRESHOLD``."
        ),
    )
    transient_error_max_retries: int = 3
    max_runs_escalation_threshold: int = Field(
        default=3,
        description=(
            "Number of consecutive times a periodic subsession can "
            "hit its ``max_runs`` limit before auto-escalating with a "
            "follow-up ticket.  When the threshold is reached, a "
            "follow-up ticket is created on the board and the monitor "
            "closes with reason ``max_runs_escalated``.  Set to ``0`` "
            "to disable escalation.  Default ``3``."
        ),
    )
    max_runs_progress_extension: int = Field(
        default=20,
        description=(
            "Number of additional runs granted to a periodic subsession "
            "when it reaches its ``max_runs`` cap but has observed "
            "progress within the recent window (a non-``NO_CHANGE``, "
            "non-duplicate reply — i.e. the agent acknowledged a state "
            "transition or reported activity).  The extended cap is "
            "clamped by ``periodic_max_total_runs``.  Set to ``0`` to "
            "disable adaptive extension.  Default ``20``."
        ),
    )
    max_runs_progress_window: int = Field(
        default=5,
        description=(
            "Number of recent runs inspected for progress when a "
            "periodic subsession reaches its ``max_runs`` cap.  A run "
            "counts as progress when the agent replies with something "
            "other than ``NO_CHANGE`` and other than a verbatim duplicate "
            "of the prior reply.  Set to ``0`` to disable adaptive "
            "extension.  Default ``5``."
        ),
    )
    monitor_slot_budget: int = Field(
        default=8,
        description=(
            "Maximum number of occupied monitor slots per conversation "
            "(active + paused periodic subsessions).  When a conversation "
            "reaches this budget, new monitor requests first try to "
            "reuse the least-recently-active paused monitor; if none is "
            "paused the request is queued rather than evicting a live "
            "monitor.  Set to ``0`` to disable per-conversation slot "
            "budgeting (all spawns proceed immediately)."
        ),
    )
    monitor_slot_queue_max: int = Field(
        default=32,
        description=(
            "Maximum number of pending monitor-spawn requests queued per "
            "conversation when the slot budget is exhausted and no paused "
            "monitor is available for reuse.  A request that would exceed "
            "this limit is rejected with a clear error instead of growing "
            "the queue unbounded."
        ),
    )
    image_publish_workflow_name: str = Field(
        default="release-image.yml",
        description=(
            "Filename of the image-publish workflow in the monitored "
            "repo's ``.github/workflows/`` directory.  After a tracked "
            "PR is merged, the watcher checks the most recent run of "
            "this workflow on the repo's default branch to verify the "
            "image was successfully published before resuming the "
            "monitor.  When the latest run failed or is still in "
            "progress, the watcher keeps the monitor paused and "
            "emits a notification so the operator can see the "
            "publish pipeline is not green.  Set to an empty string "
            "to disable post-merge image-publish verification "
            "(the watcher resumes immediately on merge, matching "
            "legacy behaviour)."
        ),
    )
    image_publish_verify_timeout_seconds: float = Field(
        default=1800.0,
        description=(
            "Maximum wall-clock seconds the watcher waits for the "
            "image-publish workflow to complete after a tracked PR "
            "is merged.  When the latest run is still in progress "
            "and this timeout has not elapsed, the watcher keeps the "
            "monitor paused and retries on the next poll cycle.  "
            "When the timeout elapses without a successful run, the "
            "watcher resumes the monitor with a warning in the "
            "notification so the agent can investigate.  Default "
            "1800 s (30 min)."
        ),
    )
    turn_budget: TurnBudgetSettings = Field(default_factory=TurnBudgetSettings)
    model_config = ConfigDict(extra="forbid")


class ConversationSettings(BaseModel):
    """Multi-session conversation continuity for the browser chat.

    The server groups conversations by a per-browser ``owner_id`` and addresses
    individual sessions by ``session_id``. Each owner can have multiple named
    sessions with independent turn histories. History is **never** wiped on
    idle — sessions are persistent when ``persist_path`` is configured.

    Attributes:
        max_history_turns: Most recent user/assistant turns kept per
            session and replayed to the agent (bounds prompt size).
        max_conversations: Maximum number of distinct sessions tracked at once
            (LRU-evicted); bounds the in-memory store.
        persist_path: Path to the JSON persistence file. Default
            ``/data/conversations.json``. Set to an empty string to disable.

    """

    max_history_turns: int = 50
    max_conversations: int = 1000
    persist_path: str = "/data/conversations.json"
    model_config = ConfigDict(extra="forbid")


class LifecycleSettings(BaseModel):
    """Deploy-lifecycle API access for the agent.

    When enabled, the chat agent gains tools to inspect the
    central-deploy lifecycle server: list services, check service status
    and health, read configuration and environment (with secrets
    already masked as ``***`` server-side by ``_mask_secrets``), and
    restart services.

    Attributes:
        enabled: Master switch.  When ``False``, no lifecycle tools are
            offered.
        base_url: Base URL of the deploy-lifecycle API server (no trailing
            slash), e.g. ``http://central-deploy:8100``.
        api_key: API key sent as the ``X-API-Key`` header.
        service_name: This service's own name as registered with the deploy
            server (e.g. ``"chat"``).  Required for ``self_restart`` — the
            deploy server has no bare ``/self/restart`` route, so a service
            restarts itself by naming itself at
            ``POST /chat/services/{service_name}/restart``.  When empty,
            ``self_restart`` (and the cognee frozen-store auto-recovery that
            depends on it) is unavailable.
        timeout: Per-request HTTP timeout in seconds.

    """

    enabled: bool = False
    base_url: str = ""
    default_protocol: str = "http"
    api_key: SecretStr = SecretStr("")
    service_name: str = ""
    timeout: float = 30.0
    self_restart_max_retries: int = 3
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _strip_removed_backoff_fields(cls, data: object) -> object:
        """Strip ``self_restart_backoff_base`` and ``_cap``.

        These fields were removed in favour of robotsix_http RetryConfig
        with hard-coded defaults.
        """
        if isinstance(data, dict):
            data.pop("self_restart_backoff_base", None)
            data.pop("self_restart_backoff_cap", None)
        return data

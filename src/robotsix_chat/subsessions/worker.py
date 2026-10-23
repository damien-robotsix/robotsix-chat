"""Subsession worker — spawn validation, the turn loop, and startup resume.

A subsession runs as a single coroutine (`_subsession_worker`) that loops
over **agent turns**.  llmio has no mid-run message injection, so steering
messages (from the parent agent or, for ``user_chat``, from the user) are
queued in the registry inbox and drained at turn boundaries:

* ``task``    — one turn; extra turns only while steering messages arrive.
* ``user_chat`` — turn per inbox batch; waits (cancellable) between turns.
* ``periodic`` — turn per tick; sleeps on the inbox event so a steering
  message wakes it early.  ``NO_CHANGE`` replies are suppressed (not
  delivered to the parent) and auto-stop the loop after N in a row.

Every kind can end itself by calling its ``complete_subsession`` tool —
the tool flips the shared :class:`CloseState`, checked after each turn.
External closes cancel the task (plain asyncio cancellation; the agent's
``finally: handle.close()`` reaps the LLM handle).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging

from .continuation import _handle_kind_continuation
from .models import (
    InboxMessage,
    SubsessionCapacityError,
    SubsessionDedupError,
    SubsessionDepthError,
    SubsessionInfo,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionNoChangeThresholdError,
    SubsessionPeriodicSpawnError,
    SubsessionStatus,
    SubsessionUserChatSpawnError,
)
from .prompt_context import (
    _build_ancestor_context,
    _operator_picked_suggestion,
    _render_turn_input,
)
from .prompts import USER_CHAT_FIRST_TURN_NOTE as _USER_CHAT_FIRST_TURN_NOTE
from .prompts import USER_CHAT_SETTLED_NOTE as _USER_CHAT_SETTLED_NOTE
from .schedule import parse_anchor_time
from .slot_budget import SLOT_BUDGET_QUEUED, SlotBudgetQueueFullError
from .slot_budget_wiring import attach_slot_budget
from .subsession_types import CloseState, SubsessionContext, SubsessionEnv
from .turn_errors import _is_github_rate_limit_error, _is_unexpected_model_behavior
from .turn_runner import (
    _MAX_PERIODIC_HISTORY_TURNS,
    _MAX_WORKER_HISTORY_TURNS,
    _history_cap,
    _run_turn,
    _run_turn_with_transient_retry,
    _run_user_chat_turn,
    _RunTimeoutError,
    _TransientExhaustedError,
)
from .worker_errors import (
    _DEGENERATE_SUCCESS_SIGNATURE as _DEGENERATE_SUCCESS_SIGNATURE,
)
from .worker_errors import _MODEL_TIER_NOT_FOUND_STATUS as _MODEL_TIER_NOT_FOUND_STATUS
from .worker_errors import _NO_CHANGE_SENTINEL as _NO_CHANGE_SENTINEL
from .worker_errors import _QUEUED_SENTINEL as _QUEUED_SENTINEL
from .worker_errors import _RETRY_PROMPT_TEMPLATE as _RETRY_PROMPT_TEMPLATE
from .worker_errors import _USAGE_EXHAUSTED_SIGNATURE as _USAGE_EXHAUSTED_SIGNATURE
from .worker_errors import _format_duration as _format_duration
from .worker_errors import _format_worker_error as _format_worker_error
from .worker_errors import _is_duplicate_reply as _is_duplicate_reply
from .worker_errors import _is_model_tier_not_found as _is_model_tier_not_found
from .worker_errors import _is_no_change as _is_no_change
from .worker_errors import _is_queued as _is_queued
from .worker_errors import _ordinal_suffix as _ordinal_suffix
from .worker_errors import _truncate as _truncate
from .worker_validation import _get_kind_turn_budget as _get_kind_turn_budget
from .worker_validation import _is_ticket_pre_authorized as _is_ticket_pre_authorized
from .worker_validation import _validate_model_level as _validate_model_level

logger = logging.getLogger(__name__)


# ``worker`` is a façade: it re-exports error-handling/validation helpers and
# prompt notes from its sibling modules so existing ``from .worker import X``
# call sites (sibling modules and tests) keep working after the module-size
# split.  Declaring the re-export surface in ``__all__`` documents it and marks
# the re-exported names as intentional exports for static analysis (CodeQL
# ``py/unused-import`` treats ``__all__`` membership as a use).
__all__ = [
    "_DEGENERATE_SUCCESS_SIGNATURE",
    "_MAX_PERIODIC_HISTORY_TURNS",
    "_MAX_WORKER_HISTORY_TURNS",
    "_MODEL_TIER_NOT_FOUND_STATUS",
    "_NO_CHANGE_SENTINEL",
    "_QUEUED_SENTINEL",
    "_RETRY_PROMPT_TEMPLATE",
    "_USAGE_EXHAUSTED_SIGNATURE",
    "_USER_CHAT_FIRST_TURN_NOTE",
    "_USER_CHAT_SETTLED_NOTE",
    "CloseState",
    "SubsessionContext",
    "SubsessionEnv",
    "_build_ancestor_context",
    "_draft_decision_comment_posted",
    "_format_duration",
    "_format_worker_error",
    "_get_kind_turn_budget",
    "_history_cap",
    "_is_duplicate_reply",
    "_is_github_rate_limit_error",
    "_is_model_tier_not_found",
    "_is_no_change",
    "_is_queued",
    "_is_ticket_pre_authorized",
    "_is_unexpected_model_behavior",
    "_operator_picked_suggestion",
    "_ordinal_suffix",
    "_render_turn_input",
    "_run_turn",
    "_subsession_worker",
    "_truncate",
    "_validate_model_level",
    "attach_slot_budget",
    "spawn_subsession",
]


# Turn-budget soft-warn reminder, appended to the agent's next turn input
# after ``soft_warn_turns`` turns.  The ``{used}`` and ``{remaining}``
# slots are filled by the worker at injection time.
_TURN_BUDGET_SOFT_WARN = (
    "[SYSTEM REMINDER — turn budget: you have used {used} turns "
    "(soft-warn threshold: {warn}). "
    "You have {remaining} turns remaining before the subsession is "
    "force-closed.  Please wrap up your work and call "
    "complete_subsession with a summary of what you have "
    "accomplished so far.]"
)


def spawn_subsession(
    *,
    env: SubsessionEnv,
    kind: SubsessionKind,
    owner_session_id: str,
    parent_id: str | None,
    depth: int,
    title: str,
    prompt: str,
    model_level: int,
    interval_seconds: float | None = None,
    anchor_time: str | None = None,
    include_previous_result: bool = False,
    max_runs: int | None = None,
    auto_stop_no_change_runs: int | None = None,
    inherit_context: bool = False,
    sub_id: str | None = None,
    runs: int = 0,
    completed_runs: set[int] | None = None,
    turn_history: list[tuple[str, str]] | None = None,
    checkpoint: dict[str, object] | None = None,
    dedup_key: str | None = None,
    depends_on_ticket_id: str | None = None,
    retry_count: int = 0,
    event_timeout_seconds: float | None = None,
    run_timeout_seconds: float | None = None,
    inbox: list[InboxMessage] | None = None,
    resume_waiting: bool = False,
) -> str:
    """Validate, register, and launch a subsession worker; return its id.

    Raises :class:`SubsessionCapacityError`, :class:`SubsessionDepthError`,
    :class:`SubsessionLevelError`, :class:`SubsessionIntervalError`,
    :class:`SubsessionAnchorError`, or
    :class:`SubsessionNoChangeThresholdError` on invalid requests — the
    tool layer maps these to polite refusals.

    *anchor_time* (kind="periodic" only) pins recurrences to an absolute
    wall-clock time — e.g. ``"09:00"`` or ``"09:00 Europe/Paris"`` (default
    timezone UTC).  When set, each recurrence lands on the next occurrence
    of that time-of-day phase-aligned to *interval_seconds* instead of
    ``now + interval``, eliminating cumulative drift.  The first run still
    fires immediately at spawn; the anchor governs every subsequent run.

    Idempotent: when *sub_id* is given and already registered (e.g. a
    duplicate resume), the existing worker is left alone and the id is
    returned immediately — no second worker is launched.

    *dedup_key* is an optional deduplication key.  When set and an
    active subsession with the same key already exists (of any kind),
    returns the existing subsession's id instead of launching a
    duplicate — this prevents a single root-cause event (e.g. filing
    the same ticket twice, or an ``asyncio.run`` crash affecting
    multiple ticket monitors) from spawning redundant workers.

    *auto_stop_no_change_runs* optionally overrides the global
    ``subsessions.auto_stop_no_change_runs`` threshold for this
    periodic monitor only.  It is persisted in the subsession
    checkpoint so the override survives resume/restart.  Set it higher
    for long-lived ticket monitors that naturally progress over days
    (waiting on human review or CI) so they are not auto-stopped after
    a handful of ``NO_CHANGE`` runs.

    *run_timeout_seconds* optionally overrides the global
    ``subsessions.run_timeout_seconds`` (default 600 s) per-run timeout
    for this subsession's agent turns — declare a longer budget for long
    batch work (e.g. 1800 for a 20-minute search batch).  Values above
    ``subsessions.max_run_timeout_seconds`` (default 3600) are clamped to
    the cap.  ``None`` (the default) falls back to the global value.

    *inbox* seeds queued inbox messages (re-enqueued on resume) and
    wakes the inbox event so they are drained at the next turn
    boundary.

    *resume_waiting* (user_chat only) marks a subsession resumed after a
    restart while it was waiting for the operator's reply: the worker
    skips the first agent turn and goes straight back to waiting, since
    the question was already delivered before the restart.
    """
    # Idempotency guard: if the subsession already exists (duplicate
    # spawn / resume race), return the existing id without launching
    # a second worker.  Must precede validation so a duplicate resume
    # never fails on capacity / depth / level checks.
    if sub_id is not None and env.registry.get(sub_id) is not None:
        return sub_id

    # Deduplication guard: when a subsession with a dedup_key already
    # exists and is active, return its id instead of spawning a duplicate.
    if dedup_key is not None:
        existing_id = env.registry.is_dedup_key_active(dedup_key)
        if existing_id is not None:
            return existing_id
        # Cross-reference: an existing PERIODIC monitor may have been
        # spawned without a dedup_key but recorded the watched ticket_id
        # in its checkpoint after the first run.  Scan for that match.
        if kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT):
            cp_match = env.registry.find_active_periodic_by_ticket_id(dedup_key)
            if cp_match is not None:
                return cp_match

    cfg = env.settings.subsessions

    # Per-subsession run timeout override — bound it by the operator's cap.
    # A spawn declaring a longer budget than the ceiling is clamped (never
    # silently exceeded): a task that plans around 2h would otherwise be
    # cut short at the cap and burn the whole budget before failing.
    if run_timeout_seconds is not None:
        cap = cfg.max_run_timeout_seconds
        if run_timeout_seconds > cap:
            logger.info(
                "Subsession %r requested run_timeout_seconds=%.0fs — "
                "clamped to subsessions.max_run_timeout_seconds (%.0fs)",
                title,
                run_timeout_seconds,
                cap,
            )
            run_timeout_seconds = cap

    if auto_stop_no_change_runs is not None and (
        isinstance(auto_stop_no_change_runs, bool)
        or not isinstance(auto_stop_no_change_runs, int)
        or auto_stop_no_change_runs < 1
    ):
        raise SubsessionNoChangeThresholdError(
            "auto_stop_no_change_runs must be an integer >= 1"
        )

    # Slot-budget admission for NEW monitors (periodic / wait_for_event).
    # Resume calls (sub_id is not None) re-activate monitors that already
    # existed before a restart — they are not new requests and are exempt.
    # When the conversation is at its occupied-slot budget:
    #   1. a paused monitor's slot is reclaimed (repurposed) for the new
    #      request — occupied count stays unchanged, no live monitor is
    #      evicted;
    #   2. otherwise the request is queued (FIFO) instead of evicting an
    #      active monitor; the queue drains when a slot frees.
    if (
        sub_id is None
        and env.slot_budget is not None
        and env.slot_budget.enabled
        and kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT)
        and env.registry.count_occupied_for_owner(owner_session_id)
        >= env.slot_budget.budget
    ):
        paused_id = env.registry.find_paused_for_reuse(owner_session_id)
        if paused_id is not None:
            logger.info(
                "slot budget: conversation %s at budget (%d occupied); "
                "reclaiming paused monitor %s to admit new %s monitor",
                owner_session_id,
                env.slot_budget.budget,
                paused_id,
                kind.value,
            )
            env.registry.cancel_and_close(
                paused_id,
                reason="slot_reclaimed",
                closed_by="system",
            )
        else:
            try:
                env.slot_budget.enqueue(
                    owner_session_id,
                    {
                        "kind": kind,
                        "owner_session_id": owner_session_id,
                        "parent_id": parent_id,
                        "depth": depth,
                        "title": title,
                        "prompt": prompt,
                        "model_level": model_level,
                        "interval_seconds": interval_seconds,
                        "include_previous_result": include_previous_result,
                        "max_runs": max_runs,
                        "auto_stop_no_change_runs": auto_stop_no_change_runs,
                        "inherit_context": inherit_context,
                        "sub_id": sub_id,
                        "runs": runs,
                        "completed_runs": completed_runs,
                        "turn_history": turn_history,
                        "checkpoint": checkpoint,
                        "dedup_key": dedup_key,
                        "depends_on_ticket_id": depends_on_ticket_id,
                        "retry_count": retry_count,
                        "event_timeout_seconds": event_timeout_seconds,
                        "run_timeout_seconds": run_timeout_seconds,
                    },
                )
            except SlotBudgetQueueFullError as exc:
                raise SubsessionCapacityError(
                    f"monitor slot budget for this conversation is full "
                    f"({env.slot_budget.budget} occupied, no paused monitor "
                    f"to reuse) and the pending queue is at its cap "
                    f"({env.slot_budget.queue_max}) — close an idle monitor "
                    f"before starting a new one"
                ) from exc
            logger.info(
                "slot budget: conversation %s at budget (%d occupied, no "
                "paused monitor); queued new %s monitor request (%d pending)",
                owner_session_id,
                env.slot_budget.budget,
                kind.value,
                env.slot_budget.pending_count(owner_session_id),
            )
            return SLOT_BUDGET_QUEUED

    # Per-session capacity check: reject spawns when the owning
    # session already holds its configured share of the pool.
    if (
        cfg.max_concurrent_per_session > 0
        and env.registry.count_active_for_owner(owner_session_id)
        >= cfg.max_concurrent_per_session
    ):
        raise SubsessionCapacityError(
            f"per-session subsession capacity reached "
            f"({cfg.max_concurrent_per_session} active for this session)"
        )

    # Global capacity check with stale-reclamation fallback.
    if env.registry.count_active() >= cfg.max_concurrent:
        reclaimed = False
        if cfg.stale_reclaim_seconds > 0:
            stale = env.registry.find_stale_for_reclaim(
                exclude_owner=owner_session_id,
                stale_seconds=cfg.stale_reclaim_seconds,
            )
            if stale is not None:
                logger.info(
                    "subsession capacity full (%s active); reclaiming "
                    "stale %s subsession %s (owner=%s, idle=%.0fs)",
                    cfg.max_concurrent,
                    stale.status.value,
                    stale.id,
                    stale.owner_session_id,
                    env.registry.now() - stale.last_activity_at,
                )
                env.registry.cancel_and_close(
                    stale.id,
                    reason="stale_reclaimed",
                    closed_by="system",
                )
                reclaimed = True

        if not reclaimed or env.registry.count_active() >= cfg.max_concurrent:
            raise SubsessionCapacityError(
                f"subsession capacity reached ({cfg.max_concurrent} active)"
            )
    if depth > cfg.max_depth:
        raise SubsessionDepthError(
            f"maximum subsession nesting depth is {cfg.max_depth}"
        )
    _validate_model_level(model_level)
    # -- cap monitor model levels to prevent routine monitors from
    #    burning expensive keyless Claude subscription tiers ---------
    if kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT):
        cap = cfg.monitor_max_model_level
        if model_level > cap:
            logger.warning(
                "spawn_subsession: %s monitor requested model_level=%d, "
                "clamping to monitor_max_model_level=%d",
                kind.value,
                model_level,
                cap,
            )
            model_level = cap
            # Re-validate: the clamped level may require an API key that
            # the original level did not.
            _validate_model_level(model_level)
    if parent_id is not None:
        parent = env.registry.get(parent_id)
        if parent is not None and parent.kind in (
            SubsessionKind.PERIODIC,
            SubsessionKind.WAIT_FOR_EVENT,
        ):
            if kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT):
                logger.warning(
                    "Subsession %s: periodic/wait_for_event spawn of nested "
                    "periodic/wait_for_event child rejected (kind=%s).",
                    parent_id,
                    kind.value,
                )
                raise SubsessionPeriodicSpawnError(
                    "periodic and wait_for_event subsessions cannot spawn "
                    "periodic or wait_for_event children."
                )
            if kind is SubsessionKind.ON_CLOSE:
                logger.warning(
                    "Subsession %s: periodic spawn of on_close child "
                    "rejected (kind=%s).",
                    parent_id,
                    kind.value,
                )
                raise SubsessionPeriodicSpawnError(
                    "periodic subsessions cannot spawn on_close children"
                )
    if kind is SubsessionKind.USER_CHAT and parent_id is not None:
        parent = env.registry.get(parent_id)
        if parent is not None and parent.kind is SubsessionKind.USER_CHAT:
            raise SubsessionUserChatSpawnError(
                "user_chat subsessions cannot spawn user_chat children"
            )
    if kind is SubsessionKind.PERIODIC:
        if interval_seconds is None or interval_seconds < cfg.min_interval_seconds:
            raise SubsessionIntervalError(
                f"periodic interval must be >= {cfg.min_interval_seconds} seconds"
            )
        if anchor_time is not None:
            # Validate the spec up front so a malformed anchor is a polite
            # spawn-time refusal, not a crash inside the worker later.
            parse_anchor_time(anchor_time)
    elif kind is SubsessionKind.WAIT_FOR_EVENT:
        interval_seconds = None
        anchor_time = None
    else:
        interval_seconds = None
        anchor_time = None

    if inherit_context and parent_id is not None:
        prompt = _build_ancestor_context(env.registry, parent_id) + prompt

    # -- write barrier: ticket_id must be in the checkpoint before spawn ----
    # A wait_for_event monitor's ticket id is carried by its dedup_key.  Some
    # callers (resume, legacy paths) can pass a checkpoint that is missing
    # the key; if the worker task were to start before the key reached the
    # persisted store, the monitor's first turn would observe an empty
    # checkpoint and report "No ticket_id in checkpoint".  Merge the
    # dedup_key into the checkpoint here so ``registry.create()`` writes it
    # in its single synchronous persist before the worker task is created.
    if kind is SubsessionKind.WAIT_FOR_EVENT and dedup_key:
        checkpoint = {**(checkpoint or {}), "ticket_id": dedup_key}
    if kind is SubsessionKind.PERIODIC and auto_stop_no_change_runs is not None:
        checkpoint = {
            **(checkpoint or {}),
            "auto_stop_no_change_runs": auto_stop_no_change_runs,
        }

    try:
        info = env.registry.create(
            kind=kind,
            owner_session_id=owner_session_id,
            parent_id=parent_id,
            depth=depth,
            title=title,
            prompt=prompt,
            model_level=model_level,
            interval_seconds=interval_seconds,
            anchor_time=anchor_time,
            include_previous_result=include_previous_result,
            max_runs=max_runs,
            sub_id=sub_id,
            runs=runs,
            completed_runs=completed_runs,
            turn_history=turn_history,
            checkpoint=checkpoint,
            dedup_key=dedup_key,
            depends_on_ticket_id=depends_on_ticket_id,
            retry_count=retry_count,
            event_timeout_seconds=event_timeout_seconds,
            run_timeout_seconds=run_timeout_seconds,
            inbox=inbox,
        )
    except SubsessionDedupError as exc:
        logger.info(
            "spawn_subsession: dedup_key %r already covered by subsession %s; "
            "returning existing id (no new worker launched).",
            dedup_key,
            exc.existing_id,
        )
        return exc.existing_id
    if resume_waiting and kind is SubsessionKind.USER_CHAT:
        # Runtime-only flag (not persisted): consumed by the worker's first
        # loop iteration, after which the normal turn cycle applies.
        info._resume_waiting = True  # type: ignore[attr-defined]
    # spawn_subsession runs inside the parent agent's turn, so a plain
    # create_task would snapshot that turn's context — including the active
    # OTEL span — and every span the subsession opens would nest inside the
    # owner session's Langfuse trace instead of forming its own (observed
    # 2026-07-11: subsession generations invisible as traces, grouped under
    # the owner's session). An empty Context() makes the worker's runs trace
    # roots, grouped under the subsession's own session id by langfuse_session.
    task = asyncio.create_task(
        _subsession_worker(env, info.id), context=contextvars.Context()
    )
    env.registry.attach_task(info.id, task)
    env._tasks.add(task)
    task.add_done_callback(env._tasks.discard)
    return info.id


"""Seconds of grace added to the configured run timeout for the
asyncio.timeout context so the warning + status update have time to
execute before the CancelledError propagates."""


def _draft_decision_comment_posted(info: SubsessionInfo) -> bool:
    """Return True if the operator-decision comment has already been posted.

    The auto-drive monitor records ``auto_drive_comment_posted`` in its
    checkpoint after posting its one operator-decision comment for a
    promotable draft.  When that flag is set and the checkpoint still
    carries ``last_known_state='draft'``, the ticket is waiting on the
    operator — the worker must skip agent turns and wait event-driven
    instead of burning its run budget re-driving an unchanged draft.
    """
    checkpoint = info.checkpoint or {}
    last_known = checkpoint.get("last_known_state")
    if not (isinstance(last_known, str) and last_known.lower() == "draft"):
        return False
    return bool(checkpoint.get("auto_drive_comment_posted"))


async def _subsession_worker(
    env: SubsessionEnv,
    sub_id: str,
    retry_input: list[InboxMessage] | None = None,
) -> None:
    """Drive one subsession to a terminal state (see module docstring).

    ``retry_input`` carries the inbox messages that were in flight when a
    user_chat / task turn failed, so the retry re-processes the drained
    operator answer instead of re-asking the original question.
    """
    from .subsession_waits import (
        _build_wait_for_event_input,
        _event_wait_loop,
        _handle_monitor_run_error,
        _paused_wait_loop,
        _queued_wait_loop,
    )
    from .worker_periodic import _build_periodic_input

    registry = env.registry
    info = registry.get(sub_id)
    if info is None:  # pragma: no cover - spawn always registers first
        return
    close_state = CloseState()
    ctx = SubsessionContext(
        owner_session_id=info.owner_session_id,
        subsession_id=sub_id,
        depth=info.depth,
    )
    try:
        # env.agent_factory (-> create_agent_from_settings) calls
        # fetch_roster_sync, which does asyncio.run(...) internally — safe
        # only when no event loop is running. _subsession_worker runs as a
        # task on the server's already-running loop, so calling the factory
        # directly here raises "asyncio.run() cannot be called from a
        # running event loop" for every subsession spawn. Offload to a
        # thread, which has no running loop of its own.
        agent = await asyncio.to_thread(
            env.agent_factory, env.settings, info.model_level, ctx, close_state
        )
        # Seed from any persisted replay window — non-empty when this
        # worker is resuming a periodic subsession after a restart, so
        # the agent picks up with its prior context instead of blank.
        history: list[tuple[str, str]] = list(info.turn_history)
        previous_result: str | None = None
        consecutive_no_change = info.consecutive_no_change
        first_turn = True
        turn_count_local = 0
        turn_budget_warned = False
        pending: list[InboxMessage] = []
        in_flight_inbox: list[InboxMessage] | None = None

        # -- checkpoint ticket_id repair on resume ---------------------
        # Event-driven monitors need ticket_id in the checkpoint to
        # register as event waiters.  If the checkpoint lost the key
        # (e.g. agent set_checkpoint cleared it, the server restarted
        # after a timeout before the post-turn repair could run, or a
        # pre-existing monitor was persisted without it), recover from
        # dedup_key now so the monitor survives the restart.
        if (
            info.kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT)
            and info.dedup_key
        ):
            cp = info.checkpoint or {}
            ticket_id_raw = cp.get("ticket_id")
            ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
            if not ticket_id:
                cp["ticket_id"] = info.dedup_key
                registry.update_checkpoint(sub_id, cp)
                logger.debug(
                    "Subsession %s: ticket_id %r recovered from dedup_key "
                    "on resume; written to checkpoint.",
                    sub_id,
                    info.dedup_key,
                )

        # -- resume status check for ticket monitors -------------------
        # Extended to all subsession kinds: a TASK or USER_CHAT with a
        # ticket_id in its checkpoint must also verify the ticket is still
        # active before proceeding — a closed ticket means the work is moot.
        if info.checkpoint is not None:
            from .worker_mill import _check_resume_status

            should_continue, context_msg = await _check_resume_status(env, info, sub_id)
            if not should_continue:
                return
            if context_msg is not None:
                pending = [
                    InboxMessage(
                        role="system",
                        text=context_msg,
                        timestamp=env.registry.now(),
                    )
                ]

        # -- paused restart: if the subsession was PAUSED before a server
        #    restart, go straight to the paused wait loop — do not run
        #    an agent turn.
        if info.status is SubsessionStatus.PAUSED:
            logger.info(
                "Subsession %s: restored in PAUSED state — entering wait loop.",
                sub_id,
            )
            result = await _paused_wait_loop(env, info, sub_id, previous_result)
            if result is None:
                return
            pending, previous_result, consecutive_no_change = result
            # Fall through to the main loop — the subsession is now RUNNING.

        # Retry re-entry: carry the in-flight drained inbox messages so a
        # failed turn never discards the operator's answer.
        if retry_input is not None:
            pending = list(retry_input)

        # -- component_request availability check ----------------------
        _cd = getattr(env.settings, "central_deploy", None)
        _cd_url = getattr(_cd, "url", "") if _cd is not None else ""
        if (
            info.kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT)
            and not _cd_url
        ):
            logger.warning(
                "Periodic subsession %s requires component_request but "
                "central_deploy.url is not configured — the tool is not "
                "available.  Closing subsession to prevent futile retries.",
                sub_id,
            )
            summary = (
                "component_request is not available: "
                "central_deploy.url is not configured. "
                "The monitor cannot fetch ticket state from the board API "
                "and would fail every tick."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="missing_tool",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(closed, summary, "missing_tool")
            return

        while True:
            in_flight_inbox = None
            # -- verify the subsession is still alive --------------------
            info = registry.get(sub_id)
            if info is None or not info.is_active:
                logger.warning(
                    "Subsession %s is no longer active — worker exiting.", sub_id
                )
                return

            registry.set_status(sub_id, SubsessionStatus.RUNNING)

            # -- user_chat resumed mid-wait: the question was delivered
            #    before the restart, so go straight back to waiting for
            #    the operator instead of re-driving an agent turn that
            #    would only re-ask it.
            if (
                info.kind is SubsessionKind.USER_CHAT
                and first_turn
                and not pending
                and getattr(info, "_resume_waiting", False)
            ):
                info._resume_waiting = False  # type: ignore[attr-defined]
                first_turn = False
                logger.info(
                    "Subsession %s: user_chat resumed while waiting for the "
                    "operator — re-entering the wait without an agent turn.",
                    sub_id,
                )
                pending = await _run_user_chat_turn(env, sub_id)
                continue

            # -- WAIT_FOR_EVENT: pre-turn event wait ----------------------
            if (
                info.kind is SubsessionKind.WAIT_FOR_EVENT
                and not first_turn
                and not pending
            ):
                result = await _event_wait_loop(
                    env, info, sub_id, previous_result, consecutive_no_change
                )
                if result is None:
                    return
                pending, previous_result, consecutive_no_change = result

            # -- on_close: wait for the parent session to close, then run
            #    as a one-shot task.  Polls is_session_closed every 5 s;
            #    exits cleanly when the subsession is externally closed
            #    while waiting.
            if info.kind is SubsessionKind.ON_CLOSE and first_turn:
                while not env.conversation_store.is_session_closed(
                    info.owner_session_id
                ):
                    info = registry.get(sub_id)
                    if info is None or not info.is_active:
                        return
                    await asyncio.sleep(5.0)
                # Parent is now closed — proceed as a one-shot task below.

            if info.kind is SubsessionKind.PERIODIC:
                # -- promotable-draft wait: once the operator-decision
                #    comment for an unchanged draft is posted, skip the
                #    agent turn and wait event-driven for the operator's
                #    promote/close transition.  This guarantees the run
                #    budget (e.g. a 60-run cap) is never exhausted
                #    re-driving an unchanged promotable draft.  Steering
                #    messages are never dropped: when the inbox holds a
                #    pending message, fall through and run the agent turn
                #    so the message reaches the monitor.
                if _draft_decision_comment_posted(info) and not pending:
                    logger.info(
                        "Subsession %s: draft decision comment already "
                        "posted — waiting event-driven for the operator.",
                        sub_id,
                    )
                    # Mirror the post-turn QUEUED path: mark SLEEPING so
                    # the registry shows the worker waiting, not running
                    # an agent turn, then long-poll without consuming runs.
                    registry.set_status(
                        sub_id,
                        SubsessionStatus.SLEEPING,
                        runs=info.runs,
                        next_run_at=registry.now() + (info.interval_seconds or 60.0),
                    )
                    result = await _queued_wait_loop(
                        env,
                        info,
                        sub_id,
                        previous_result,
                        consecutive_no_change,
                    )
                    if result is None:
                        return
                    pending, previous_result, consecutive_no_change = result
                    continue

                # -- run guard: prevent duplicate execution of run N -----
                next_run = info.runs + 1
                if not registry.claim_run(sub_id, next_run):
                    logger.warning(
                        "Run %d of subsession %s was already executed; "
                        "skipping duplicate.",
                        next_run,
                        sub_id,
                    )
                    # Advance the run counter and retry immediately.  A
                    # collision means the counter is behind completed_runs
                    # (a pre-fix persisted store); sleeping an interval per
                    # historical run number starves the schedule — with
                    # regular restarts the subsession never runs again.
                    registry.set_status(
                        sub_id,
                        SubsessionStatus.RUNNING,
                        runs=next_run,
                    )
                    continue

                # Reset the turn-budget counter at the start of each run.
                # Periodic monitors make one agent turn per cycle and are
                # designed to stay alive across pauses/resumes for the whole
                # life of a ticket; the turn budget guards a single
                # run-burst, not the monitor's lifetime ceiling (that is
                # ``periodic_max_total_runs``'s job).
                turn_count_local = 0
                turn_budget_warned = False

                steering = pending
                turn_input = _build_periodic_input(
                    info,
                    previous_result,
                    steering,
                    auto_drive_promote_ready_drafts=bool(
                        getattr(
                            env.settings.subsessions,
                            "auto_drive_promote_ready_drafts",
                            False,
                        )
                    ),
                    sub_id=sub_id,
                    registry=registry,
                )
            elif info.kind is SubsessionKind.WAIT_FOR_EVENT:
                next_run = info.runs + 1
                if not registry.claim_run(sub_id, next_run):
                    logger.warning(
                        "Run %d of subsession %s was already executed; "
                        "skipping duplicate.",
                        next_run,
                        sub_id,
                    )
                    registry.set_status(
                        sub_id,
                        SubsessionStatus.RUNNING,
                        runs=next_run,
                    )
                    continue
                # Reset the turn-budget counter at the start of each run —
                # wait_for_event monitors are long-lived event-driven
                # monitors; the turn budget guards a single run-burst, not
                # their lifetime.
                turn_count_local = 0
                turn_budget_warned = False
                steering = pending
                turn_input = _build_wait_for_event_input(
                    info,
                    previous_result,
                    steering,
                    sub_id=sub_id,
                    registry=registry,
                )
            elif first_turn:
                if retry_input is not None:
                    turn_input = _render_turn_input(pending)
                    in_flight_inbox = pending
                else:
                    turn_input = info.prompt
                    if info.kind is SubsessionKind.USER_CHAT:
                        turn_input = _USER_CHAT_FIRST_TURN_NOTE + "\n\n" + turn_input
            else:
                turn_input = _render_turn_input(pending)
                in_flight_inbox = pending
                if _operator_picked_suggestion(info, pending):
                    turn_input += _USER_CHAT_SETTLED_NOTE
            first_turn = False

            # -- turn budget soft-warn --------------------------------------
            _budget = _get_kind_turn_budget(
                env.settings.subsessions.turn_budget, info.kind
            )
            if (
                _budget is not None
                and _budget.soft_warn_turns > 0
                and turn_count_local >= _budget.soft_warn_turns
                and not turn_budget_warned
            ):
                if _budget.hard_stop_turns > 0:
                    remaining = _budget.hard_stop_turns - turn_count_local
                else:
                    # No hard-stop configured — the reminder still nudges
                    # the agent to wrap up, but there is no ceiling.
                    remaining = 0
                turn_input += _TURN_BUDGET_SOFT_WARN.format(
                    used=turn_count_local,
                    warn=_budget.soft_warn_turns,
                    remaining=remaining,
                )
                turn_budget_warned = True

            try:
                reply = await _run_turn_with_transient_retry(
                    env,
                    agent,
                    turn_input,
                    history,
                    sub_id,
                    info,
                )
            except _RunTimeoutError:
                # Periodic runs continue the schedule after a timeout;
                # task / user_chat runs fail the whole subsession.
                if info.kind in (
                    SubsessionKind.PERIODIC,
                    SubsessionKind.WAIT_FOR_EVENT,
                ):
                    timeout_msg = "subsession run exceeded the per-run timeout."
                    (
                        run_failed,
                        previous_result,
                        consecutive_no_change,
                    ) = await _handle_monitor_run_error(
                        env,
                        info,
                        sub_id,
                        timeout_msg,
                        "TIMEOUT",
                        previous_result,
                        consecutive_no_change,
                    )
                    if run_failed:
                        return
                    if info.kind is SubsessionKind.WAIT_FOR_EVENT:
                        # No sleep — the main loop re-enters the event wait.
                        env.registry.reap_orphans()
                    else:
                        woke = await registry.wait_for_inbox(
                            sub_id,
                            timeout=info.interval_seconds or 60.0,
                        )
                        pending = registry.drain_inbox(sub_id) if woke else []
                        env.registry.reap_orphans()
                    continue
                # TASK / USER_CHAT: let the outer handler fail the subsession.
                raise
            except _TransientExhaustedError:
                # Periodic runs whose transient errors could not be cleared
                # skip this cycle and continue the schedule instead of
                # failing permanently.
                if info.kind in (
                    SubsessionKind.PERIODIC,
                    SubsessionKind.WAIT_FOR_EVENT,
                ):
                    error_label = "TRANSIENT_ERROR"
                    error_msg = (
                        "Transient API errors persisted across all retry attempts."
                    )
                    (
                        run_failed,
                        previous_result,
                        consecutive_no_change,
                    ) = await _handle_monitor_run_error(
                        env,
                        info,
                        sub_id,
                        error_msg,
                        error_label,
                        previous_result,
                        consecutive_no_change,
                    )
                    if run_failed:
                        return
                    if info.kind is SubsessionKind.WAIT_FOR_EVENT:
                        # No sleep — the main loop re-enters the event wait.
                        env.registry.reap_orphans()
                    else:
                        woke = await registry.wait_for_inbox(
                            sub_id,
                            timeout=info.interval_seconds or 60.0,
                        )
                        pending = registry.drain_inbox(sub_id) if woke else []
                        env.registry.reap_orphans()
                    continue
                # TASK / USER_CHAT: let the outer handler fail the subsession.
                raise
            except Exception as exc:
                # Non-transient run-level error (e.g. tool-retry exhaustion,
                # unexpected exception).  For periodic / wait_for_event
                # monitors, route through _handle_monitor_run_error so the
                # run is recorded as errored and the subsession stays alive
                # until the consecutive-error threshold is reached.
                #
                # Model-tier 404 errors are re-raised so the outer handler
                # can attempt a model-level fallback.
                if info.kind in (
                    SubsessionKind.PERIODIC,
                    SubsessionKind.WAIT_FOR_EVENT,
                ) and not _is_model_tier_not_found(exc):
                    error_msg = _format_worker_error(exc)
                    error_label = "RUN_ERROR"
                    (
                        run_failed,
                        previous_result,
                        consecutive_no_change,
                    ) = await _handle_monitor_run_error(
                        env,
                        info,
                        sub_id,
                        error_msg,
                        error_label,
                        previous_result,
                        consecutive_no_change,
                    )
                    if run_failed:
                        return
                    if info.kind is SubsessionKind.WAIT_FOR_EVENT:
                        # No sleep — the main loop re-enters the event wait.
                        env.registry.reap_orphans()
                    else:
                        woke = await registry.wait_for_inbox(
                            sub_id,
                            timeout=info.interval_seconds or 60.0,
                        )
                        pending = registry.drain_inbox(sub_id) if woke else []
                        env.registry.reap_orphans()
                    continue
                # TASK / USER_CHAT or model-tier 404: let the outer handler
                # fail the subsession (or attempt model-level fallback).
                raise

            # Successful run — reset the consecutive-error counter.
            if (
                info.kind
                in (
                    SubsessionKind.PERIODIC,
                    SubsessionKind.WAIT_FOR_EVENT,
                )
                and info.consecutive_errored_runs > 0
            ):
                info.consecutive_errored_runs = 0

            history.append((turn_input, reply))
            registry.append_turn_history(sub_id, turn_input, reply)
            # Inbox messages were transcripted at enqueue time; only the
            # assistant side is appended here.
            registry.append_transcript(sub_id, "assistant", reply)

            turn_count_local += 1

            # -- turn budget hard stop ---------------------------------------
            if (
                _budget is not None
                and _budget.hard_stop_turns > 0
                and turn_count_local >= _budget.hard_stop_turns
                and not close_state.requested
            ):
                summary = (
                    f"The subsession was force-closed after exceeding the "
                    f"per-kind turn budget: {turn_count_local} turns used "
                    f"(hard-stop at {_budget.hard_stop_turns}). "
                    f"The agent's last reply was:\n\n{reply}"
                )
                closed = registry.mark_closed(
                    sub_id,
                    summary=summary,
                    reason="turn_budget_exceeded",
                    closed_by="system",
                )
                if closed is not None:
                    await env.delivery.deliver_summary(
                        closed, summary, "turn_budget_exceeded"
                    )
                else:
                    # External close already won the race — deliver the
                    # partial-work report anyway so the parent agent sees it.
                    closed_info = registry.get(sub_id)
                    if closed_info is not None:
                        await env.delivery.deliver_summary(
                            closed_info, summary, "turn_budget_exceeded"
                        )
                return

            # -- agent-requested close (any kind) --------------------------
            if close_state.requested:
                summary = close_state.summary or reply
                closed = registry.mark_closed(
                    sub_id, summary=summary, reason="completed", closed_by="agent"
                )
                if closed is not None:
                    await env.delivery.deliver_summary(closed, summary, "completed")
                elif not close_state.delivery_done:
                    # Already closed by the complete_subsession tool (which
                    # persists immediately to survive a process restart).
                    # The tool may have already delivered the summary to
                    # survive a race with an external close; only deliver
                    # here when the tool did NOT already deliver.
                    closed_info = registry.get(sub_id)
                    if closed_info is not None:
                        await env.delivery.deliver_summary(
                            closed_info, summary, "completed"
                        )
                return

            # -- kind-specific continuation --------------------------------
            continuation = await _handle_kind_continuation(
                env, info, sub_id, reply, previous_result, consecutive_no_change
            )
            if continuation is None:
                return
            pending, previous_result, consecutive_no_change = continuation
            info.consecutive_no_change = consecutive_no_change

    except asyncio.CancelledError:
        # External close already set the terminal state and (if wanted)
        # delivered the summary — nothing to do here.
        raise
    except Exception as exc:
        logger.exception("Subsession %s worker failed", sub_id)
        error_msg = _format_worker_error(exc)

        # -- retry for user_chat / task kinds --------------------------
        info = registry.get(sub_id)
        if (
            info is not None
            and info.kind
            in (
                SubsessionKind.USER_CHAT,
                SubsessionKind.TASK,
                SubsessionKind.ON_CLOSE,
            )
            # A turn that hit the hard run timeout will hit it again with
            # the identical input — fail fast instead of burning the retry
            # budget on a doomed turn (2026-09-04: three 10-minute retries
            # of the same timed-out Hexarchy batch).  kind-specific timeout
            # handling for monitors already happens inside the run loop.
            and not isinstance(exc, _RunTimeoutError)
        ):
            max_retries = env.settings.subsessions.user_chat_max_retries
            if info.retry_count < max_retries:
                info.retry_count += 1
                info._last_error = error_msg
                retry_notice = _RETRY_PROMPT_TEMPLATE.format(
                    attempt=info.retry_count,
                    max_retries=max_retries,
                    error=error_msg,
                )
                # Prepend the retry notice to the original prompt.
                # Strip a prior retry notice if present so they don't
                # accumulate across attempts.
                if _RETRY_PROMPT_TEMPLATE.split("{", 1)[0] in info.prompt:
                    # There is a prior retry notice — replace it.
                    info.prompt = info.prompt.split("]\n\n", 1)[-1]
                info.prompt = retry_notice + info.prompt
                # Persist the retry state before re-launching so a
                # crash restart picks up the updated retry_count.
                registry.persist()
                logger.info(
                    "Subsession %s: retry %d/%d after error: %s",
                    sub_id,
                    info.retry_count,
                    max_retries,
                    error_msg,
                )
                if in_flight_inbox:
                    logger.warning(
                        "Subsession %s: re-delivering %d drained inbox "
                        "message(s) on retry that would otherwise have "
                        "been discarded.",
                        sub_id,
                        len(in_flight_inbox),
                    )
                await _subsession_worker(
                    env,
                    sub_id,
                    retry_input=in_flight_inbox or None,
                )
                return

        # -- retry for periodic / wait_for_event monitors -------------
        # When a monitor fails with a non-transient error (e.g. tool
        # retry limit, unexpected exception), retry up to
        # monitor_error_max_retries times before failing permanently.
        # Each retry re-launches the worker so the agent can
        # self-correct with the error context.
        if (
            info is not None
            and info.kind
            in (
                SubsessionKind.PERIODIC,
                SubsessionKind.WAIT_FOR_EVENT,
            )
            and not isinstance(exc, _RunTimeoutError)
        ):
            max_monitor_retries = env.settings.subsessions.monitor_error_max_retries
            if info.retry_count < max_monitor_retries:
                info.retry_count += 1
                info._last_error = error_msg
                retry_notice = _RETRY_PROMPT_TEMPLATE.format(
                    attempt=info.retry_count,
                    max_retries=max_monitor_retries,
                    error=error_msg,
                )
                # Prepend the retry notice to the original prompt.
                # Strip a prior retry notice if present so they don't
                # accumulate across attempts.
                if _RETRY_PROMPT_TEMPLATE.split("{", 1)[0] in info.prompt:
                    info.prompt = info.prompt.split("]\n\n", 1)[-1]
                info.prompt = retry_notice + info.prompt
                registry.persist()
                logger.info(
                    "Monitor %s: retry %d/%d after error: %s",
                    sub_id,
                    info.retry_count,
                    max_monitor_retries,
                    error_msg,
                )
                if in_flight_inbox:
                    logger.warning(
                        "Monitor %s: re-delivering %d drained inbox "
                        "message(s) on retry.",
                        sub_id,
                        len(in_flight_inbox),
                    )
                await _subsession_worker(
                    env,
                    sub_id,
                    retry_input=in_flight_inbox or None,
                )
                return

        # -- exhausted retries or non-retryable kind ------------------
        failed = registry.fail(sub_id, error=error_msg)
        if failed is not None:
            summary = failed.summary or f"Failed: {error_msg}"
            # For user_chat subsessions that exhausted retries, append
            # the original prompt so the operator can answer the
            # decision directly in the main conversation — the
            # side-chat panel is no longer available.
            if (
                failed.kind is SubsessionKind.USER_CHAT
                and info is not None
                and info.retry_count >= env.settings.subsessions.user_chat_max_retries
            ):
                summary += (
                    "\n\nThe side-chat could not be delivered after "
                    f"{info.retry_count} retries. "
                    "You can answer the original decision here:\n\n"
                    f"{info.prompt}"
                )
            await env.delivery.deliver_summary(failed, summary, "failed")

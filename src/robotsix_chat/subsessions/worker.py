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
import fnmatch
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from robotsix_http import RetryConfig, acall_with_retry
from robotsix_llmio.openrouter import is_openrouter_transient

from .delivery import ParentDelivery
from .models import (
    InboxMessage,
    SubsessionCapacityError,
    SubsessionDedupError,
    SubsessionDepthError,
    SubsessionInfo,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionLevelError,
    SubsessionNoChangeThresholdError,
    SubsessionPeriodicSpawnError,
    SubsessionStatus,
    SubsessionUserChatSpawnError,
)
from .registry import OWNER_CLOSED_REASON, SubsessionRegistry
from .slot_budget import SLOT_BUDGET_QUEUED, SlotBudget, SlotBudgetQueueFullError

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.events import EventSink
    from robotsix_chat.chat.server.routes import ChatAgent
    from robotsix_chat.config import KindTurnBudget, Settings, TurnBudgetSettings

logger = logging.getLogger(__name__)

# Prior turns replayed to the subsession agent are capped so a
# long-running periodic/user_chat subsession cannot grow its own prompt
# without bound.
_MAX_WORKER_HISTORY_TURNS = 20

# Periodic/wait-for-event monitors run on a fresh, never-compacted CLI
# transcript per tick, so replaying the full 20-turn window re-carries an
# ever-growing prefix as cache traffic on every step of every tool loop.
# Bound monitor replay much tighter — the current tick only needs a couple
# of prior turns for continuity.
_MAX_PERIODIC_HISTORY_TURNS = 3

# The Claude Agent SDK's wording when it collapses a self-contradictory
# ``is_error=True`` / ``errors=[]`` / ``subtype="success"`` frame into a
# bare message — a known transient bug, not a real tool failure.
_DEGENERATE_SUCCESS_SIGNATURE = "returned an error result: success"

# The Claude CLI's wording when a tier's usage credits are exhausted.
_USAGE_EXHAUSTED_SIGNATURE = "out of usage credits"

# HTTP status used by model providers (OpenRouter, etc.) when the
# requested model is not available at the configured price ceiling.
# The model exists but cannot be reached through the current routing
# — falling back to a different tier usually resolves it.
_MODEL_TIER_NOT_FOUND_STATUS = 404

#: Minimum model level a periodic monitor can fall back to before the
#: subsession is failed permanently.  Decrementing by 1 each time, a
#: monitor starting at level 5 will try 5→4→3→2→1 before giving up.
_MODEL_LEVEL_FALLBACK_FLOOR = 1

#: Maximum number of tier-fallback steps before the subsession is failed.
#: Caps the chain 4→3→2→1 (max 3 steps).
_MODEL_LEVEL_FALLBACK_MAX = 3


def _is_model_tier_not_found(exc: BaseException) -> bool:
    """Return ``True`` when *exc* indicates the requested model tier is not available.

    Currently matches HTTP 404 on the exception or anywhere in its cause
    chain — the common signature when an OpenRouter model cannot be routed
    at the configured price ceiling.
    """
    from robotsix_http.retry import _status

    return _status(exc) == _MODEL_TIER_NOT_FOUND_STATUS


def _format_worker_error(exc: BaseException) -> str:
    """Translate known Claude SDK error patterns into clear human-readable messages.

    When *exc* is a :class:`claude_agent_sdk.ProcessError` (the CLI
    subprocess exited non-zero), the message includes the exit code and
    stderr output so the operator can diagnose the tool failure without
    digging through logs.

    For unrecognised exceptions the exception type name is always included
    so the message is actionable even when the SDK wording is opaque.
    """
    msg = str(exc)
    exc_type_name = type(exc).__name__

    # Degenerate success frame — a known transient Claude SDK bug that
    # can persist across retries.  Not a real tool failure.
    if _DEGENERATE_SUCCESS_SIGNATURE in msg.lower():
        return (
            "The Claude agent encountered a transient internal SDK error "
            "(degenerate success frame — the SDK reported an error result "
            "whose subtype is 'success', a self-contradictory frame that "
            "could not be cleared by retry). This is a known Claude SDK "
            "bug and does not indicate a real tool failure. "
            f"Original SDK message: {msg}"
        )

    # Usage-exhaustion — the tier has no credits left.
    if _USAGE_EXHAUSTED_SIGNATURE in msg.lower():
        return (
            "The Claude agent's usage credits for this tier are exhausted. "
            "Switch to a different model level, or wait for credits to "
            "reset. " + msg
        )

    # Model-tier not found — the model is unavailable at the configured
    # tier (e.g. OpenRouter 404 when no provider serves it at the max
    # price).  Periodic monitors automatically fall back to a lower level.
    if _is_model_tier_not_found(exc):
        return (
            "The requested model tier is not available "
            "(HTTP 404 — the model could not be routed at the configured "
            "price ceiling). Periodic monitors will automatically fall "
            "back to a lower model level. " + msg
        )

    # ProcessError from claude_agent_sdk carries exit_code and stderr —
    # surface those so the operator can diagnose without log-diving.
    exit_code = getattr(exc, "exit_code", None)
    if exit_code is not None:
        stderr = getattr(exc, "stderr", None)
        parts = [f"Claude CLI process exited with code {exit_code}"]
        if stderr:
            stderr_text = str(stderr).strip()
            if stderr_text:
                parts.append(f"stderr: {_truncate(stderr_text, 500)}")
        parts.append(msg)
        return "\n".join(parts)

    # For any other exception, include the type name so the message is
    # never just an opaque SDK string — the operator can distinguish a
    # TimeoutError from a RuntimeError at a glance.
    if exc_type_name not in msg:
        return f"[{exc_type_name}] {msg}"
    return msg


def _truncate(text: str, max_len: int) -> str:
    """Truncate *text* to *max_len* chars, appending ``"..."`` when cut."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# Reply sentinel a periodic subsession uses to report "nothing changed".
_NO_CHANGE_SENTINEL = "NO_CHANGE"

# Reply sentinel a periodic subsession uses to report that the monitored
# ticket is queued (waiting for implementation / in a non-terminal
# pipeline stage) — the monitor should enter event-driven wait instead of
# burning no-change quota.
_QUEUED_SENTINEL = "QUEUED"


# Prompt fragment prepended when a user_chat / task subsession is retried
# after a failure.  The agent sees the original error so it can diagnose
# and self-correct (e.g. re-build context that was lost).
_RETRY_PROMPT_TEMPLATE = (
    "[System note: this subsession is being retried after a failure "
    "(attempt {attempt}/{max_retries}). The error was:\n\n{error}\n\n"
    "The subsession has been re-launched from its original instructions. "
    "If the error was caused by lost context (e.g. after a server restart) "
    "you may need to re-fetch any external state you were relying on. "
    "Your original instructions follow below.]\n\n"
)

# System note prepended to the first turn of every user_chat subsession so
# the agent always restates option definitions inline instead of surfacing
# bare labels ("Option B") the operator cannot disambiguate.
_USER_CHAT_FIRST_TURN_NOTE = (
    "[System note: this is a side-chat with the operator. "
    "Your instructions may define a menu of options (Option A, Option B, …). "
    "The operator sees ONLY what you write in this panel — they do NOT see "
    "your instructions.  Every time you reference an option label you MUST "
    "restate its full definition inline so the operator can understand it "
    "without switching context.  For example, instead of writing "
    '"Option B is the right call," write '
    '"Option B (phased: cleanup now, warning-first gate, fail-closed only '
    'after auto-mail migrates) is the right call."  This applies to every '
    "turn — the initial recommendation and any follow-up confirmation-gate "
    "turns.  If you present multiple options, show ALL of them with their "
    "definitions so the operator can compare.  Whenever a turn asks the "
    "operator to pick between discrete options (Option A/B/C, approve/reject, "
    "yes/no, pick-one-of-N), ALSO end that message with a fenced block:\n"
    "```suggestions\n"
    "<one option per line>\n"
    "```\n"
    "one self-contained option per line (2-5 options, each <= ~80 chars, "
    "actionable as a verbatim reply) so the operator can answer with a single "
    "click; keep the surrounding prose so a typed free-text answer is equally "
    "valid.]"
)


# Consecutive stale-worker resume attempts before the subsession is closed.


# Phrases that, when they appear at the start of a periodic reply,
# indicate the agent found nothing to report.  Kept broad enough to
# catch common LLM paraphrasing of "nothing changed" without being so
# broad that it swallows real status updates.
_NO_CHANGE_PHRASES: tuple[str, ...] = (
    "NO CHANGE",
    "NO CHANGES",
    "NOTHING CHANGED",
    "NOTHING HAS CHANGED",
    "NO UPDATES",
    "UNCHANGED",
    "NO NEW",
    "EVERYTHING IS THE SAME",
    "ALL QUIET",
    "STATUS UNCHANGED",
    "NO SIGNIFICANT CHANGE",
    "NO MEANINGFUL CHANGE",
)

# Phrases that, when they appear at the start of a periodic reply,
# indicate the agent found the ticket is queued (waiting for
# implementation) — the monitor should switch to event-driven wait.
_QUEUED_PHRASES: tuple[str, ...] = (
    "QUEUED",
    "QUEUED FOR IMPLEMENTATION",
    "WAITING FOR IMPLEMENTATION",
    "IN QUEUE",
    "IMPLEMENTATION QUEUED",
    "AWAITING IMPLEMENTATION",
    "PENDING IMPLEMENTATION",
)


def _format_duration(seconds: float) -> str:
    """Return a human-readable duration string for *seconds*."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes} min"
    hours = int(seconds / 3600)
    minutes = int((seconds % 3600) / 60)
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def _is_no_change(reply: str) -> bool:
    """Whether *reply* is the periodic no-change sentinel or a common paraphrase.

    The LLM sometimes returns a paraphrase instead of the exact sentinel.
    """
    cleaned = reply.strip().upper()
    if cleaned.startswith(_NO_CHANGE_SENTINEL):
        return True
    return cleaned.startswith(_NO_CHANGE_PHRASES)


def _is_queued(reply: str) -> bool:
    """Whether *reply* is the queued sentinel or a common paraphrase.

    The agent uses this when the monitored ticket is waiting for
    implementation — the worker should switch to event-driven wait
    instead of counting this as a no-change run.
    """
    cleaned = reply.strip().upper()
    if cleaned.startswith(_QUEUED_SENTINEL):
        return True
    return cleaned.startswith(_QUEUED_PHRASES)


def _is_duplicate_reply(reply: str, previous: str | None) -> bool:
    """Whether *reply* is identical to the previous run's reply.

    Strips and case-folds before comparing — suppresses repeated verbatim output.
    """
    if previous is None:
        return False
    return reply.strip().casefold() == previous.strip().casefold()


def _ordinal_suffix(n: int) -> str:
    """Return the ordinal suffix for *n*.

    E.g. ``"st"``, ``"nd"``, ``"rd"``, ``"th"``.
    """
    if 11 <= (n % 100) <= 13:
        return "th"
    last = n % 10
    if last == 1:
        return "st"
    if last == 2:
        return "nd"
    if last == 3:
        return "rd"
    return "th"


def _is_ticket_pre_authorized(
    ticket_id: str,
    patterns: list[str],
) -> bool:
    """Return ``True`` if *ticket_id* matches any glob pattern in *patterns*.

    Uses :func:`fnmatch.fnmatch` for case-sensitive glob matching.
    An empty *patterns* list always returns ``False``.
    """
    if not patterns:
        return False
    if not ticket_id:
        return False
    return any(fnmatch.fnmatch(ticket_id, p) for p in patterns)


def _get_kind_turn_budget(
    budgets: TurnBudgetSettings,
    kind: SubsessionKind,
) -> KindTurnBudget | None:
    """Return the :class:`KindTurnBudget` for *kind*, or ``None``.

    ``WAIT_FOR_EVENT`` reuses the ``periodic`` budget since it is a
    variant of periodic monitoring.
    """
    if kind is SubsessionKind.TASK:
        return budgets.task
    if kind is SubsessionKind.PERIODIC or kind is SubsessionKind.WAIT_FOR_EVENT:
        return budgets.periodic
    if kind is SubsessionKind.USER_CHAT:
        return budgets.user_chat
    if kind is SubsessionKind.ON_CLOSE:
        return budgets.on_close
    return None


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


@dataclass(frozen=True)
class SubsessionContext:
    """Identity captured lexically in agent tool closures.

    ``subsession_id is None`` means the agent IS the main chat agent
    (depth 0); its spawned children then have ``parent_id=None`` and
    their summaries deliver straight to the owning chat session.
    """

    owner_session_id: str
    subsession_id: str | None
    depth: int


@dataclass
class CloseState:
    """Mutable close-request holder shared between a worker and its agent.

    The ``complete_subsession`` tool sets :attr:`requested` (and the
    summary); the worker checks it after every turn.

    When the tool itself already delivered the summary (e.g. to survive
    a race with an external close), :attr:`delivery_done` is set to
    ``True`` so the worker does not deliver a second time.
    """

    requested: bool = False
    summary: str | None = None
    delivery_done: bool = False


@dataclass
class SubsessionEnv:
    """Shared runtime dependencies for spawning and running subsessions.

    ``agent_factory(settings, model_level, ctx, close_state)`` must build
    a fully-tooled agent for the subsession: the standard tool suite plus
    the depth-aware subsession tools (spawn/message/close/list when depth
    allows, ``complete_subsession`` always).
    """

    settings: Settings
    registry: SubsessionRegistry
    delivery: ParentDelivery
    conversation_store: ConversationStore
    agent_factory: Callable[[Settings, int, SubsessionContext, CloseState], ChatAgent]
    event_sink: EventSink | None = None
    # Strong refs to worker tasks spawned via spawn_subsession (belt and
    # braces alongside the registry's _running map).
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)
    # Per-conversation slot-budget manager (optional).  When set (and
    # enabled), spawn_subsession consults it before admitting a new
    # monitor; when None, all spawns proceed as before.  Wired by
    # :func:`attach_slot_budget`.
    slot_budget: SlotBudget | None = None


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
    inbox: list[InboxMessage] | None = None,
    resume_waiting: bool = False,
) -> str:
    """Validate, register, and launch a subsession worker; return its id.

    Raises :class:`SubsessionCapacityError`, :class:`SubsessionDepthError`,
    :class:`SubsessionLevelError`, :class:`SubsessionIntervalError`, or
    :class:`SubsessionNoChangeThresholdError` on invalid requests — the
    tool layer maps these to polite refusals.

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
    _validate_model_level(env.settings, model_level)
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
            _validate_model_level(env.settings, model_level)
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
    elif kind is SubsessionKind.WAIT_FOR_EVENT:
        interval_seconds = None
    else:
        interval_seconds = None

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


def attach_slot_budget(env: SubsessionEnv) -> SlotBudget | None:
    """Create and wire the per-conversation slot-budget manager into *env*.

    Returns ``None`` (and wires nothing) when slot budgeting is disabled
    by config (``monitor_slot_budget <= 0``).  Otherwise sets
    ``env.slot_budget`` and registers a registry close callback so the
    pending queue drains whenever a monitor terminates and a slot frees.
    """
    cfg = env.settings.subsessions
    budget = getattr(cfg, "monitor_slot_budget", 0)
    if budget <= 0:
        return None
    slot_budget = SlotBudget(
        budget=budget,
        queue_max=getattr(cfg, "monitor_slot_queue_max", 32),
    )
    env.slot_budget = slot_budget

    def _on_monitor_closed(info: SubsessionInfo) -> None:
        if info.close_reason == "slot_reclaimed":
            # The freed slot is being repurposed by the in-flight spawn
            # that triggered the reclamation — draining here would admit
            # a second monitor into the same slot.
            return
        if info.close_reason == OWNER_CLOSED_REASON:
            # The conversation itself is being torn down — drop any
            # pending monitor requests rather than spawning work for a
            # dead session.
            slot_budget.discard(info.owner_session_id)
            return
        _drain_slot_budget_queue(env, info.owner_session_id)

    env.registry.add_close_callback(_on_monitor_closed)
    return slot_budget


def _drain_slot_budget_queue(env: SubsessionEnv, owner_session_id: str) -> None:
    """Dequeue and spawn pending monitor requests while slots are free.

    Called after a monitor terminates (its occupied slot frees).  Spawns
    the oldest pending request per iteration and repeats until the queue
    is empty or the conversation is back at budget.
    """
    slot_budget = env.slot_budget
    if slot_budget is None or not slot_budget.enabled:
        return
    while env.registry.count_occupied_for_owner(owner_session_id) < slot_budget.budget:
        request = slot_budget.pop_next(owner_session_id)
        if request is None:
            return
        try:
            sub_id = spawn_subsession(env=env, **request)
        except Exception:
            logger.exception(
                "slot budget: failed to drain queued monitor request "
                "for conversation %s — request re-queued",
                owner_session_id,
            )
            # Keep the request's FIFO position and stop: retrying the
            # whole queue here would risk a failure cascade.
            slot_budget.requeue_front(owner_session_id, request)
            return
        if sub_id == SLOT_BUDGET_QUEUED:
            # Defensive: the freed slot was consumed concurrently and the
            # request re-queued itself — stop draining to avoid a loop.
            return
        logger.info(
            "slot budget: drained queued %s monitor request for "
            "conversation %s into freed slot (%s)",
            request.get("kind"),
            owner_session_id,
            sub_id,
        )


def _validate_model_level(settings: Settings, model_level: int) -> None:
    """Reject invalid levels and key-bearing levels without a key."""
    from robotsix_chat.config import VALID_MODEL_LEVELS, level_needs_api_key

    if model_level not in VALID_MODEL_LEVELS:
        raise SubsessionLevelError(
            f"model_level must be one of {sorted(VALID_MODEL_LEVELS)}"
        )
    if (
        level_needs_api_key(model_level)
        and not settings.llmio_api_key.get_secret_value()
    ):
        raise SubsessionLevelError(
            f"model level {model_level} requires an OpenRouter API key "
            "but the server could not find one in its configuration. "
            "The key may not be set in the config file, or may be set "
            "but stored in a location the server does not read — "
            "the server only reads the `llmio.api_key` field in its "
            "JSON config file, not environment variables or external "
            "secret stores.  Retry at level 4 (keyless) or have the "
            "operator verify the API key is set in the config file."
        )


# Character budget for the ancestor-context block prepended to a nested
# child's prompt when ``inherit_context=True``.  The budget covers the
# block header plus each ancestor (title + first 300 chars of its prompt);
# ancestors beyond the budget are silently dropped so the block never
# overwhelms the child's own instructions.
_MAX_ANCESTOR_CONTEXT_CHARS = 2000


def _build_ancestor_context(registry: SubsessionRegistry, parent_id: str) -> str:
    """Walk up the parent chain and build a compact context block.

    Returns a string of the form::

        # Ancestor context (inherited from the subsession tree above you)

        ## ancestor-1 title
        ancestor-1 prompt summary …

        ## ancestor-2 title
        ...

    or an empty string when the parent chain is unreachable.
    """
    ancestors: list[SubsessionInfo] = []
    current_id: str | None = parent_id
    while current_id is not None:
        info = registry.get(current_id)
        if info is None:
            break
        ancestors.append(info)
        current_id = info.parent_id
    if not ancestors:
        return ""

    # Build from root downward (reverse the walk-up order).
    ancestors.reverse()
    parts: list[str] = [
        "# Ancestor context (inherited from the subsession tree above you)\n"
    ]
    budget = _MAX_ANCESTOR_CONTEXT_CHARS - len(parts[0])
    for info in ancestors:
        snippet = info.prompt[:300]
        entry = f"## {info.title}\n{snippet}"
        if len(entry) > budget:
            break
        parts.append(entry)
        budget -= len(entry) + 1  # +1 for the blank line separator
    if not parts[1:]:  # only the header, no actual ancestor entries
        return ""
    return "\n\n".join(parts) + "\n\n"


def _render_turn_input(messages: list[InboxMessage]) -> str:
    """Merge an inbox batch into one turn input, labelled by role."""
    if len(messages) == 1:
        return messages[0].text
    return "\n\n".join(f"[{m.role}] {m.text}" for m in messages)


def _is_github_rate_limit_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is a GitHub API rate-limit error (403/429).

    Inspects the exception's ``__cause__`` and ``__context__`` chain
    for ``RuntimeError`` messages matching the pattern emitted by
    :class:`~robotsix_chat.repo.direct.client.DirectRepoClient`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None:
        exc_id = id(current)
        if exc_id in seen:
            break
        seen.add(exc_id)
        if isinstance(current, RuntimeError):
            msg = str(current)
            if "GitHub API" in msg and (
                "error 429" in msg
                or ("error 403" in msg and "rate limit" in msg.lower())
            ):
                return True
        current = current.__cause__ or current.__context__
    return False


def _is_unexpected_model_behavior(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is a pydantic-ai ``UnexpectedModelBehavior``.

    The LLM provider occasionally returns an unexpected response shape
    (empty stream, malformed JSON delta, missing candidate) that pydantic-ai
    surfaces as ``UnexpectedModelBehavior``.  These are typically transient —
    a retry almost always succeeds — so monitor subsessions should ride them
    out with backoff rather than failing permanently.

    Walks the ``__cause__`` / ``__context__`` chain because agent frameworks
    routinely wrap the original error.
    """
    # Lazy import to avoid a hard dependency at module level — pydantic-ai
    # is always present in this repo, but the import is cheap and keeps
    # the function self-contained.
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None:
        exc_id = id(current)
        if exc_id in seen:
            break
        seen.add(exc_id)
        if isinstance(current, UnexpectedModelBehavior):
            return True
        current = current.__cause__ or current.__context__
    return False


# Backoff policy for transient turn retries.  Module-level rather than
# operator-configurable: robotsix_http owns retry policy fleet-wide, and the
# only turn-level knob that ever needed tuning is the attempt count
# (``transient_error_max_retries``).  Tests collapse the cap to 0 so the
# retry paths run instantly — see tests/subsessions/test_worker.py.
#
# These reproduce the delays the hand-rolled loop produced: it used
# ``min(1.0 * 2**attempt, 30.0)`` -> 1s, 2s, 4s, and RetryConfig computes
# ``min(2.0**attempt, 30.0)`` -> the same 1s, 2s, 4s, with up to 50% jitter
# subtracted (RetryConfig.jitter_factor), which the old loop lacked.
_TRANSIENT_BACKOFF_BASE = 2.0
_TRANSIENT_BACKOFF_CAP = 30.0


async def _run_turn_with_transient_retry(
    env: SubsessionEnv,
    agent: ChatAgent,
    turn_input: str,
    history: list[tuple[str, str]],
    sub_id: str,
    info: SubsessionInfo,
) -> str:
    """Run one agent turn, retrying on transient API errors for periodic subsessions.

    For PERIODIC subsessions only: transient errors (e.g. OpenRouter
    upstream hiccups, GitHub API rate-limit 403/429, pydantic-ai
    ``UnexpectedModelBehavior`` from malformed provider responses) are
    retried with exponential backoff.  When all retries are exhausted the
    function raises :class:`_TransientExhaustedError` so the worker loop
    can skip the cycle gracefully instead of permanently failing the
    subsession.

    For TASK / USER_CHAT subsessions the error propagates unchanged —
    the outer handler will fail the subsession.

    The loop itself is :func:`robotsix_http.acall_with_retry`; the
    transient classification stays here, since it is domain knowledge
    (which provider errors are worth another turn) rather than transport
    policy.  ``acall_with_retry`` re-raises the original exception when
    attempts run out, so the exhausted-and-still-transient case is
    translated to the sentinel below.
    """
    settings = env.settings.subsessions
    max_retries = settings.transient_error_max_retries

    def _is_transient(exc: BaseException) -> bool:
        """Whether *exc* earns another turn for THIS subsession kind.

        Kind is part of the predicate on purpose: the same provider error
        is retryable for a monitor (skip the cycle, keep the schedule) and
        fatal for a task / user_chat run.  Returning ``False`` makes
        ``acall_with_retry`` re-raise immediately, which is exactly the
        old behaviour for non-monitors.
        """
        is_monitor = info.kind in (
            SubsessionKind.PERIODIC,
            SubsessionKind.WAIT_FOR_EVENT,
        )
        if not is_monitor:
            return False
        return is_openrouter_transient(exc) or (
            _is_github_rate_limit_error(exc) or _is_unexpected_model_behavior(exc)
        )

    def _log_retry(attempt: int, exc: Exception, delay: float) -> None:
        logger.warning(
            "Subsession %s run %d: transient error on attempt %d/%d — "
            "retrying in %.1fs. Error: %s",
            sub_id,
            info.runs + 1,
            attempt,
            max_retries + 1,
            delay,
            exc,
        )

    async def _one_turn() -> str:
        return await _run_turn_with_timeout(
            env, agent, turn_input, history, sub_id, info
        )

    try:
        # cast: acall_with_retry's `Callable[..., T]` resolves T to the
        # coroutine type for an async fn, so the awaited value is str at
        # runtime but Coroutine to the checker. Same treatment as the
        # existing call site in repo/direct/__init__.py.
        return cast(
            "str",
            await acall_with_retry(
                _one_turn,
                config=RetryConfig(
                    max_retries=max_retries,
                    backoff_base=_TRANSIENT_BACKOFF_BASE,
                    backoff_cap=_TRANSIENT_BACKOFF_CAP,
                    on_retry=_log_retry,
                ),
                what=f"subsession {sub_id} agent turn",
                is_transient_fn=_is_transient,
            ),
        )
    except _RunTimeoutError:
        raise  # timeout is handled separately; never retried
    except Exception as exc:
        if not _is_transient(exc):
            raise
        # Every sanctioned attempt was spent and the error is still
        # transient — surface the sentinel so the worker loop skips this
        # cycle instead of permanently failing the subsession.
        logger.error(
            "Subsession %s run %d: all %d transient-error retries exhausted "
            "(last error: %s) — skipping this cycle.",
            sub_id,
            info.runs + 1,
            max_retries + 1,
            exc,
        )
        raise _TransientExhaustedError(
            f"transient error persisted across {max_retries + 1} attempts"
        ) from exc


class _TransientExhaustedError(Exception):
    """Sentinel raised when all transient-error retries are exhausted.

    Caught by the worker loop for periodic subsessions to skip the cycle
    gracefully rather than permanently failing the subsession.
    """


def _history_cap(kind: SubsessionKind) -> int:
    """Return the prior-turn replay cap for *kind*.

    Periodic and wait-for-event monitors get the tight
    :data:`_MAX_PERIODIC_HISTORY_TURNS` bound; every other kind keeps the
    default :data:`_MAX_WORKER_HISTORY_TURNS` window.
    """
    if kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT):
        return _MAX_PERIODIC_HISTORY_TURNS
    return _MAX_WORKER_HISTORY_TURNS


async def _run_turn(
    agent: ChatAgent,
    turn_input: str,
    history: list[tuple[str, str]],
    sub_id: str,
    *,
    max_history_turns: int = _MAX_WORKER_HISTORY_TURNS,
    trace_metadata: dict[str, str] | None = None,
    trace_name: str | None = None,
) -> str:
    """Run one agent turn and return the reply text."""
    parts = [
        chunk
        async for chunk in agent.stream(
            turn_input,
            history=history[-max_history_turns:] or None,
            session_id=sub_id,
            client_id=sub_id,
            trace_metadata=trace_metadata,
            trace_name=trace_name,
        )
    ]
    return "".join(parts)


_RUN_TIMEOUT_GRACE = 5.0
"""Seconds of grace added to the configured run timeout for the
asyncio.timeout context so the warning + status update have time to
execute before the CancelledError propagates."""


async def _run_turn_with_timeout(
    env: SubsessionEnv,
    agent: ChatAgent,
    turn_input: str,
    history: list[tuple[str, str]],
    sub_id: str,
    info: SubsessionInfo,
) -> str:
    """Run one agent turn with a hard timeout guard.

    On timeout the run is marked failed for TASK/USER_CHAT kinds, or the
    schedule continues with the failure recorded for PERIODIC kinds.
    """
    timeout = env.settings.subsessions.run_timeout_seconds
    try:
        async with asyncio.timeout(timeout + _RUN_TIMEOUT_GRACE):
            return await _run_turn(
                agent,
                turn_input,
                history,
                sub_id,
                max_history_turns=_history_cap(info.kind),
                trace_metadata={
                    "owner_session_id": info.owner_session_id,
                    "parent_session_id": info.parent_id or info.owner_session_id,
                },
                trace_name="subsession-turn",
            )
    except TimeoutError:
        logger.warning(
            "Subsession %s run timed out after %.0fs; marking run as failed",
            sub_id,
            timeout,
        )
        raise _RunTimeoutError(
            f"subsession run exceeded {timeout:.0f}s timeout"
        ) from None


class _RunTimeoutError(Exception):
    """Raised when a single subsession turn exceeds the run timeout.

    Internal sentinel — caught by the worker loop to trigger kind-specific
    failure handling without conflating with other CancelledError sources.
    """


async def _run_task_turn(
    env: SubsessionEnv, sub_id: str, reply: str
) -> list[InboxMessage]:
    """Handle TASK post-turn: drain inbox; return pending messages or close.

    Returns a non-empty list if steering messages arrived mid-turn
    (the worker should continue), or an empty list after closing the
    subsession (the worker should stop).
    """
    pending = env.registry.drain_inbox(sub_id)
    if pending:
        return pending  # a steering message arrived mid-turn
    closed = env.registry.mark_closed(
        sub_id, summary=reply, reason="completed", closed_by="agent"
    )
    if closed is not None:
        await env.delivery.deliver_summary(closed, reply, "completed")
    return []


async def _run_user_chat_turn(env: SubsessionEnv, sub_id: str) -> list[InboxMessage]:
    """Handle USER_CHAT post-turn: wait for inbox, drain, return pending."""
    env.registry.set_status(sub_id, SubsessionStatus.WAITING)
    await env.registry.wait_for_inbox(sub_id, timeout=None)
    return env.registry.drain_inbox(sub_id)


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
        if info is not None and info.kind in (
            SubsessionKind.USER_CHAT,
            SubsessionKind.TASK,
            SubsessionKind.ON_CLOSE,
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

        # -- model-tier fallback for periodic monitors ------------------
        # When the model is unavailable at the current tier (HTTP 404),
        # try the next lower level instead of failing the monitor.
        # Recovery from a spent Claude subscription tier is handled
        # separately by the usage-exhausted path.
        if (
            info is not None
            and info.kind
            in (
                SubsessionKind.PERIODIC,
                SubsessionKind.WAIT_FOR_EVENT,
            )
            and _is_model_tier_not_found(exc)
            and info.model_level > _MODEL_LEVEL_FALLBACK_FLOOR
        ):
            fallback_count_raw = (
                (info.checkpoint or {}).get("_tier_fallback_count")
                if info.checkpoint
                else 0
            )
            fallback_count = (
                fallback_count_raw if isinstance(fallback_count_raw, int) else 0
            )
            if fallback_count < _MODEL_LEVEL_FALLBACK_MAX:
                new_level = info.model_level - 1
                logger.warning(
                    "Subsession %s: model tier %d not found (HTTP 404); "
                    "falling back to level %d (tier-fallback %d/%d).",
                    sub_id,
                    info.model_level,
                    new_level,
                    fallback_count + 1,
                    _MODEL_LEVEL_FALLBACK_MAX,
                )
                cp = dict(info.checkpoint or {})
                cp["_tier_fallback_count"] = fallback_count + 1
                cp["_fallback_model_level"] = new_level
                info.model_level = new_level
                info.checkpoint = cp
                # Persist so the fallback survives a restart.
                registry.update_checkpoint(sub_id, cp)
                registry.persist()
                await _subsession_worker(
                    env,
                    sub_id,
                    retry_input=in_flight_inbox or None,
                )
                return

            logger.error(
                "Subsession %s: model-tier fallback exhausted "
                "(tried %d level(s) from original tier).",
                sub_id,
                fallback_count,
            )

        # -- retry for periodic / wait_for_event monitors -------------
        # When a monitor fails with a non-transient error (e.g. tool
        # retry limit, unexpected exception), retry up to
        # monitor_error_max_retries times before failing permanently.
        # Each retry re-launches the worker so the agent can
        # self-correct with the error context.
        if info is not None and info.kind in (
            SubsessionKind.PERIODIC,
            SubsessionKind.WAIT_FOR_EVENT,
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


async def _handle_kind_continuation(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    reply: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Dispatch kind-specific post-turn logic.

    Returns ``(pending, previous_result, consecutive_no_change)`` to
    continue, or ``None`` to stop (the subsession reached a terminal
    state).
    """
    if info.kind is SubsessionKind.TASK or info.kind is SubsessionKind.ON_CLOSE:
        pending = await _run_task_turn(env, sub_id, reply)
        if not pending:
            return None
        return (pending, None, 0)

    if info.kind is SubsessionKind.USER_CHAT:
        pending = await _run_user_chat_turn(env, sub_id)
        return (pending, None, 0)

    # PERIODIC
    if info.kind is SubsessionKind.PERIODIC:
        from .worker_periodic import _run_periodic_turn

        result = await _run_periodic_turn(
            env, info, sub_id, reply, previous_result, consecutive_no_change
        )
        if result is None:
            return None
        pending, previous_result, consecutive_no_change = result
        env.registry.reap_orphans()
        return (pending, previous_result, consecutive_no_change)

    # WAIT_FOR_EVENT
    if info.kind is SubsessionKind.WAIT_FOR_EVENT:
        from .subsession_waits import _run_wait_for_event_turn

        result = await _run_wait_for_event_turn(
            env, info, sub_id, reply, previous_result, consecutive_no_change
        )
        if result is None:
            return None
        pending, previous_result, consecutive_no_change = result
        env.registry.reap_orphans()
        return (pending, previous_result, consecutive_no_change)

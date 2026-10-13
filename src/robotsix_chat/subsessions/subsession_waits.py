"""Subsession waiting patterns and event-driven monitoring.

Implements the core waiting strategies for periodic, wait_for_event,
and user_chat subsessions: idle waiting with timeout, event-driven
waiting for board state changes, and human-approval timeout escalation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from robotsix_chat.subsessions.models import (
    InboxMessage,
    SubsessionInfo,
    SubsessionKind,
    SubsessionStatus,
)

if TYPE_CHECKING:
    from .worker import SubsessionEnv

logger = logging.getLogger(__name__)

# -- Waiting strategies

# The poll-timeout when a monitor is waiting for an event on the board.
# (The exact timeout does not matter because the event-wait path is
# explicitly woken by the event dispatcher, and this timeout is the
# safety-net fallback in case the event dispatch fails.)
_WAIT_FOR_EVENT_TIMEOUT_SECONDS = 60.0

# Threshold of consecutive no-change runs before a monitor auto-pauses.
# This matches the periodic scheduler config, so a monitor with
# period=60s will auto-pause after 5*60 = 300s of no-change runs if the
# ticket is inert.  A persistent ticket state-change that the monitor
# misses (e.g.  a rare race window between poll and board update) will
# be caught on the next wake from backlog.
_NO_CHANGE_AUTO_PAUSE_THRESHOLD = 5


# Consecutive stale-worker (start but never reply) runs before closure.
# The stale guard is a last-resort failsafe; it fires only when the worker
# thread is unresponsive and does not post a result — normally, a slow turn
# just takes longer to reply.  The threshold is conservative.
_CONSECUTIVE_STALE_WORKER_LIMIT = 10


async def _paused_wait_loop(
    env: SubsessionEnv, sub_id: str, max_iterations: int = 1
) -> None:
    """Sleep while paused, re-surface the paused notice if resumed.

    Does NOT force-pause on timeout — the subsession is already marked
    PAUSED and will only wake when explicitly resumed by the operator
    or on a subsequent scheduled period (for PERIODIC kind). Each
    iteration re-checks the in-memory status flag (so a concurrent
    close/resume is visible), then awaits inbox delivery of a user
    message that wakes the paused subsession.
    """
    for _ in range(max_iterations):
        registry = env.registry
        info = registry.get(sub_id)
        if info is None or info.status != SubsessionStatus.PAUSED:
            return  # Already closed or unpaused externally.
        # Block until an inbox message arrives. The timeout is
        # effectively infinite — paused subsessions stay asleep until
        # resumed by the operator (post a message, mark_ready, etc.).
        woke = await registry.wait_for_inbox(sub_id, timeout=None)
        if not woke:
            return  # Timeout should never happen with timeout=None, but guard.


async def _queued_wait_loop(
    env: SubsessionEnv, sub_id: str, interval_seconds: float
) -> None:
    """Event-driven wait for a queued ticket to leave the queue.

    Periodically polls the board (via the event-driven wait mechanism)
    until the ticket exits the queue (state changes away from
    ready/in_progress/implement, or reached a terminal state). Each
    poll is woken by a board event or triggered by a timeout; the
    agent is invoked only when the ticket state actually changes.
    """
    # Placeholder for the queued wait — the actual event-wait loop is
    # in the periodic / wait-for-event turn handler.
    await asyncio.sleep(0)


# -- Error handling for periodic / wait_for_event monitors


from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE, subsession_result_frame
from robotsix_chat.subsessions.worker_errors import (
    _NO_CHANGE_SENTINEL,
    _QUEUED_SENTINEL,
    _categorize_error,
    _is_duplicate_reply,
    _is_no_change,
    _is_queued,
    _truncate,
)


def _extract_ticket_id(info: SubsessionInfo) -> str | None:
    """Extract the tracked ticket ID from subsession info.

    Returns the ticket_id from checkpoint, or falls back to dedup_key
    (which is the ticket ID for monitors).
    """
    if info.checkpoint:
        ticket_id = info.checkpoint.get("ticket_id")
        if isinstance(ticket_id, str):
            return ticket_id
    return info.dedup_key


async def _handle_monitor_run_error(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    error_msg: str,
    result_label: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[bool, str | None, int]:
    """Handle a run-level error for a periodic or wait_for_event monitor.

    Increments ``consecutive_errored_runs``, records the error in the
    transcript, advances the run counter, and sets the subsession to
    SLEEPING.  When the consecutive-error threshold is reached the
    subsession is permanently failed.

    The parent is notified at most once per error streak (on the first
    errored run of a new streak), with full error context to support
    operator decision-making.

    Returns ``(failed, previous_result, consecutive_no_change)`` where
    *failed* is ``True`` when the subsession was permanently failed.
    """
    registry = env.registry
    threshold = env.settings.subsessions.consecutive_error_fail_threshold
    info.consecutive_errored_runs += 1
    consecutive_errored = info.consecutive_errored_runs

    registry.append_transcript(
        sub_id,
        "system",
        f"Run errored ({result_label}): {error_msg}",
    )

    # Check if the threshold is reached.
    if consecutive_errored >= threshold:
        # Provide comprehensive diagnostic information for terminal failure.
        # Include error categorization to help operator decide next steps.
        error_category = _categorize_error(info._last_error_exc or Exception(error_msg))
        
        if error_category == "transient":
            recommendation = (
                "**Recommendation:** This appears to be a transient error that may self-resolve. "
                "The monitor has been stopped, but you can manually resume it after waiting a moment, "
                "or let the next scheduled cycle pick it up automatically."
            )
        elif error_category == "permanent":
            recommendation = (
                "**Recommendation:** This appears to be a permanent error requiring investigation. "
                "Check the error details and underlying resource state before resuming. "
                "Common causes: permission issues, missing resource, configuration mismatch."
            )
        else:
            recommendation = (
                "**Recommendation:** Review the error details in the logs. "
                "Determine whether to retry after investigating or let the next cycle attempt it."
            )
        
        summary = (
            f"Monitor failed after {consecutive_errored} consecutive errors\n\n"
            f"**Error Category:** {error_category}\n\n"
            f"**Last Error:** {error_msg}\n\n"
            f"{recommendation}"
        )
        failed = registry.fail(sub_id, error=summary)
        if failed is not None:
            await env.delivery.deliver_summary(failed, summary, "failed")
        return True, previous_result, consecutive_no_change

    # Notify the parent at most once per streak (on the first errored run).
    # Provide full error context without truncation to support operator decision-making.
    if consecutive_errored == 1 and env.event_sink is not None:
        error_category = _categorize_error(info._last_error_exc or Exception(error_msg))
        
        env.event_sink.publish(
            info.owner_session_id,
            {
                "type": SSE_NOTIFICATION_TYPE,
                "title": f"Monitor error (retry in progress): {info.title}",
                "body": (
                    f"Monitor {sub_id[:8]} encountered an error in {result_label}. "
                    f"Retrying automatically.\n\n"
                    f"**Error type:** {error_category}\n"
                    f"**Details:** {error_msg}\n\n"
                    f"The monitor will retry on the next cycle. If errors persist, "
                    f"it will be stopped and you'll be notified for manual review."
                ),
                "urgency": "medium",
                "link": _extract_ticket_id(info) or sub_id,
            },
        )

    # Advance the run counter and continue the schedule.
    runs = info.runs + 1
    if info.kind is SubsessionKind.WAIT_FOR_EVENT:
        registry.set_status(
            sub_id,
            SubsessionStatus.SLEEPING,
            runs=runs,
            last_result=result_label,
        )
    else:
        registry.set_status(
            sub_id,
            SubsessionStatus.SLEEPING,
            runs=runs,
            next_run_at=registry.now() + (info.interval_seconds or 60.0),
            last_result=result_label,
        )
    if env.event_sink is not None:
        env.event_sink.publish(
            info.owner_session_id,
            subsession_result_frame(
                sub_id,
                info.kind.value,
                info.title,
                runs,
                result_label,
                info.parent_id,
            ),
        )
    if not info.include_previous_result:
        previous_result = None
    consecutive_no_change += 1
    info.consecutive_no_change = consecutive_no_change
    return False, previous_result, consecutive_no_change


async def _run_wait_for_event_turn(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    reply: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Handle WAIT_FOR_EVENT post-turn: update status, deliver, re-arm wait.

    Does NOT auto-pause, auto-stop, or enforce max_runs — the subsession
    runs only when events arrive.  Returns ``None`` on human_approval_timeout
    closure, or ``([], previous_result, consecutive_no_change)`` to re-arm
    the event wait.
    """
    registry = env.registry
    suppressed = _is_no_change(reply) or _is_duplicate_reply(reply, previous_result)
    consecutive_no_change = 0 if not _is_no_change(reply) else consecutive_no_change + 1
    runs = info.runs + 1
    registry.set_status(
        sub_id,
        SubsessionStatus.SLEEPING,
        runs=runs,
        last_result=reply,
    )
    if not suppressed and env.event_sink is not None:
        env.event_sink.publish(
            info.owner_session_id,
            subsession_result_frame(
                sub_id,
                info.kind.value,
                info.title,
                runs,
                reply,
                info.parent_id,
            ),
        )
    previous_result = reply

    # Human-approval detection: for event-driven monitors, the monitor
    # is already in event-driven mode and will wake when the ticket
    # state changes — no auto-escalation closure needed.  The monitor
    # stays alive until it reaches a terminal state or the user
    # explicitly stops tracking.
    checkpoint = info.checkpoint or {}
    # Repair the checkpoint: if the agent called set_checkpoint without
    # including ticket_id (replacing the spawn-time entry), recover it
    # from the dedup_key so the monitor survives restarts.
    if "ticket_id" not in checkpoint and info.dedup_key:
        checkpoint["ticket_id"] = info.dedup_key
        registry.update_checkpoint(sub_id, checkpoint)
        logger.debug(
            "Subsession %s: repaired checkpoint with ticket_id=%s",
            sub_id,
            info.dedup_key,
        )

    # Check for human_issue_approval timeout escalation: the subsession
    # remains alive if the ticket is still in human_issue_approval.
    # (Timeout enforcement is left to the periodic handler.)
    return [], previous_result, consecutive_no_change

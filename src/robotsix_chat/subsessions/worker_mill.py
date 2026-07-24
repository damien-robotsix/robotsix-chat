"""Mill-communication helpers for subsession worker resume-status checks.

All five functions query the mill via HTTP, parse ticket state, and manage
a consecutive-failure counter.  None interact with the turn loop or agent
infrastructure directly — they are a self-contained, single-responsibility
module extracted from ``worker.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from .models import SubsessionInfo, SubsessionStatus

if TYPE_CHECKING:
    from .worker import SubsessionEnv

logger = logging.getLogger(__name__)

# Consecutive mill-unreachable failures before the subsession enters
# recovery mode (exponential backoff + health probe) instead of closing
# immediately.  _handle_mill_unreachable applies further retries
# controlled by SubsessionsSettings.mill_recovery_max_retries.
_MAX_MILL_FAILURES = 2

# Consecutive stale-worker resume attempts before the subsession is closed.
# A "stale worker" means the mill's ``started_at`` timestamp has not
# changed since the last resume — the worker was never redeployed, so
# any fix PRs merged since the ticket was blocked cannot be present.
_MAX_STALE_WORKER_RESUMES = 2

# Consecutive blocked-on-resume events before the subsession is closed.
# When a ticket is BLOCKED on every resume (the agent keeps hitting the
# same failure without making progress), auto-retry is futile — close
# the monitor so the operator can intervene (e.g. manually revert
# footprint-violating files rather than re-running the same dead-end
# implement loop).
_MAX_BLOCKED_RESUMES = 3

# Ticket states recognised by the resume status check.
_TICKET_STATE_TERMINAL = frozenset({"closed", "done"})
_TICKET_STATE_BLOCKED = frozenset({"blocked"})
_TICKET_STATE_HUMAN_APPROVAL = frozenset({"human_issue_approval"})


async def _get_mill_started_at(board_url: str) -> str | None:
    """Query the mill's health endpoint for its ``started_at`` timestamp.

    Returns the ``started_at`` field as a string, or ``None`` when the
    endpoint is unreachable, returns a non-2xx status, or the response
    body does not contain a ``started_at`` key.

    This is a best-effort freshness probe — failures are logged at debug
    level and must not block the resume flow.
    """
    health_url = f"{board_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            response = await client.get(health_url)
            response.raise_for_status()
            health_data: dict[str, object] = response.json()
    except Exception:
        logger.debug("Mill health probe failed for %s", health_url, exc_info=True)
        return None

    started_at = health_data.get("started_at")
    if isinstance(started_at, str):
        return started_at
    return None


async def _check_stale_worker_resume(
    env: SubsessionEnv,
    _info: SubsessionInfo,
    sub_id: str,
    checkpoint: dict[str, object],
    worker_started_at: str | None,
    ticket_id: str,
    last_known_str: str,
) -> tuple[bool, str | None]:
    """Check for stale-worker resumption when a ticket is BLOCKED.

    Returns ``(should_close, context_message)``:
    * ``(False, summary)`` — stale cap exceeded; subsession was closed.
    * ``(True, context_msg)`` — under the stale cap; return with warning.
    * ``(True, None)`` — worker was redeployed (counter reset) or health
      probe failed; continue with the normal blocked-context message.
    """
    if worker_started_at is not None:
        previous_started_at = checkpoint.get("worker_started_at")
        previous_started_at_str = (
            previous_started_at if isinstance(previous_started_at, str) else None
        )
        if (
            previous_started_at_str is not None
            and worker_started_at == previous_started_at_str
        ):
            # Worker unchanged — count this as a stale resume.
            raw_count = checkpoint.get("stale_worker_resume_count")
            count = int(raw_count) if isinstance(raw_count, (int, float)) else 0
            count += 1
            checkpoint["stale_worker_resume_count"] = count
            env.registry.update_checkpoint(sub_id, checkpoint)

            if count >= _MAX_STALE_WORKER_RESUMES:
                summary = (
                    f"Ticket {ticket_id} is still blocked after "
                    f"{count} resume attempts, but the mill worker "
                    f"has not been redeployed (started_at unchanged "
                    f"at {worker_started_at}).  A fix merged since "
                    f"the ticket was blocked cannot be present on "
                    f"this worker — closing subsession to prevent "
                    f"futile retries on a stale image."
                )
                closed = env.registry.mark_closed(
                    sub_id,
                    summary=summary,
                    reason="stale_worker",
                    closed_by="system",
                )
                if closed is not None:
                    await env.delivery.deliver_summary(closed, summary, "stale_worker")
                logger.warning(
                    "Subsession %s (ticket %s): stale worker %d times — closed.",
                    sub_id,
                    ticket_id,
                    count,
                )
                return (False, summary)

            # Under the cap — warn the agent strongly.
            remaining = _MAX_STALE_WORKER_RESUMES - count
            context = (
                f"[System note: this ticket monitor was restarted after a "
                f"service restart.  Ticket {ticket_id} is currently BLOCKED "
                f"(previous known state: {last_known_str}).  "
                f"IMPORTANT: the mill worker has NOT been redeployed since "
                f"the last resume attempt (started_at: {worker_started_at}) — "
                f"this is stale-resume attempt {count}/{_MAX_STALE_WORKER_RESUMES} "
                f"({remaining} remaining before auto-close).  "
                f"Fetch the ticket history and comments.  If fix PRs have been "
                f"merged but the worker was never redeployed, do NOT auto-resume "
                f"— escalate to the operator for a redeploy instead.  "
                f"If the block is a transient failure (provider timeout, "
                f"sandbox 503), auto-resume is acceptable.]"
            )
            logger.info(
                "Subsession %s (ticket %s): stale worker, attempt %d/%d.",
                sub_id,
                ticket_id,
                count,
                _MAX_STALE_WORKER_RESUMES,
            )
            return (True, context)

        # Worker was redeployed (or this is the first resume) —
        # store the new started_at and reset the stale counter.
        checkpoint["worker_started_at"] = worker_started_at
        checkpoint.pop("stale_worker_resume_count", None)
        env.registry.update_checkpoint(sub_id, checkpoint)
    else:
        # Health probe failed — cannot verify freshness; note it.
        logger.debug(
            "Subsession %s (ticket %s): mill health probe failed — "
            "cannot verify worker freshness.",
            sub_id,
            ticket_id,
        )

    return (True, None)


async def _check_resume_status(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
) -> tuple[bool, str | None]:
    """Query the mill for a ticket monitor's current state on resume.

    Returns ``(should_continue, context_message)``:
    * ``(True, None)`` — normal resume; continue to the monitoring loop.
    * ``(True, msg)`` — continue, but inject *msg* as first-turn context.
    * ``(False, summary)`` — subsession closed; *summary* is the reason.

    Only called when *info.checkpoint* has a ``ticket_id`` key and the
    mill base URL is configured.
    """
    checkpoint = info.checkpoint
    if checkpoint is None:
        return (True, None)
    ticket_id_raw = checkpoint.get("ticket_id")
    if not isinstance(ticket_id_raw, str) or not ticket_id_raw:
        return (True, None)

    ticket_id = ticket_id_raw
    direct_repo = getattr(env.settings, "direct_repo", None)
    board_url = (
        getattr(direct_repo, "board_api_base_url", "")
        if direct_repo is not None
        else ""
    )
    if not board_url:
        logger.debug(
            "Subsession %s has ticket checkpoint but board_api_base_url "
            "is not configured — skipping status check.",
            sub_id,
        )
        return (True, None)

    # Build the ticket URL.  httpx.URL.copy_with does NOT normalise
    # ``../`` segments — the agent is trusted to set a valid ticket_id.
    try:
        base = httpx.URL(board_url.rstrip("/"))
        ticket_url = base.copy_with(path=f"/tickets/{ticket_id}")
    except Exception:
        logger.exception("Could not construct ticket URL for subsession %s", sub_id)
        should_continue = await _handle_mill_unreachable(env, info, sub_id)
        return (should_continue, None)

    # Query the mill.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.get(str(ticket_url))
            response.raise_for_status()
            ticket_data: dict[str, object] = response.json()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.warning(
            "Mill returned %d for ticket %s (subsession %s)",
            status_code,
            ticket_id,
            sub_id,
        )
        # 4xx errors are permanent — close immediately with the reason.
        if 400 <= status_code < 500:
            if status_code == 404:
                reason = f"Ticket {ticket_id} was deleted during the outage."
            elif status_code in (401, 403):
                reason = (
                    f"Authentication error ({status_code}) for ticket "
                    f"{ticket_id} — check API credentials."
                )
            else:
                reason = (
                    f"HTTP {status_code} for ticket {ticket_id} — closing subsession."
                )
            summary = f"Ticket {ticket_id} is no longer reachable: {reason}"
            closed = env.registry.mark_closed(
                sub_id,
                summary=summary,
                reason="ticket_unreachable",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "ticket_unreachable"
                )
            return (False, summary)
        # 5xx errors are transient — count toward the failure cap.
        should_continue = await _handle_mill_unreachable(env, info, sub_id)
        return (should_continue, None)
    except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
        logger.warning(
            "Mill unreachable for ticket %s (subsession %s): %s",
            ticket_id,
            sub_id,
            exc,
        )
        should_continue = await _handle_mill_unreachable(env, info, sub_id)
        return (should_continue, None)
    except Exception:
        logger.exception(
            "Unexpected error querying mill for ticket %s (subsession %s)",
            ticket_id,
            sub_id,
        )
        should_continue = await _handle_mill_unreachable(env, info, sub_id)
        return (should_continue, None)

    # Reset the consecutive-failures counter on success.
    _reset_mill_failure_counter(env, info, sub_id)

    # Compare with the last-known state.
    last_known = checkpoint.get("last_known_state")
    current_state = ticket_data.get("state")
    current_state_str = (
        current_state if isinstance(current_state, str) else str(current_state)
    )
    last_known_str = (
        (last_known if isinstance(last_known, str) else str(last_known))
        if last_known is not None
        else "unknown"
    )

    # Reset the blocked-resume counter when the ticket is NOT blocked —
    # the agent made progress (the ticket left the "blocked" state at
    # some point since the last resume).
    if current_state_str.lower() not in _TICKET_STATE_BLOCKED and checkpoint.get(
        "blocked_resume_count"
    ):
        checkpoint["blocked_resume_count"] = 0
        env.registry.update_checkpoint(sub_id, checkpoint)

    # Terminal → close the subsession.
    if current_state_str.lower() in _TICKET_STATE_TERMINAL:
        summary = (
            f"Ticket {ticket_id} reached terminal state "
            f"'{current_state_str}' during the outage. "
            f"Previous state was '{last_known_str}'."
        )
        closed = env.registry.mark_closed(
            sub_id,
            summary=summary,
            reason="ticket_terminal_on_resume",
            closed_by="system",
        )
        if closed is not None:
            await env.delivery.deliver_summary(closed, summary, "ticket_terminal")
        logger.info(
            "Subsession %s (ticket %s): terminal on resume — closed.",
            sub_id,
            ticket_id,
        )
        return (False, summary)

    # Blocked → inject context for the agent to handle.
    if current_state_str.lower() in _TICKET_STATE_BLOCKED:
        # Track consecutive blocked-on-resume events.  If the ticket
        # keeps landing back in BLOCKED after every resume attempt
        # (e.g. the agent hits the same config-standard footprint
        # violation and never reverts the offending files), auto-retry
        # is futile — close the monitor so the operator can intervene.
        raw_count = checkpoint.get("blocked_resume_count")
        blocked_count = int(raw_count) if isinstance(raw_count, (int, float)) else 0
        blocked_count += 1
        checkpoint["blocked_resume_count"] = blocked_count
        env.registry.update_checkpoint(sub_id, checkpoint)

        # Probe mill health for worker freshness.  If the worker has not
        # been redeployed since the last resume attempt, a fix merged in
        # the meantime cannot be present — auto-resuming would just hit
        # the same failure on a stale image.
        worker_started_at = await _get_mill_started_at(board_url)
        stale_decision, stale_context = await _check_stale_worker_resume(
            env,
            info,
            sub_id,
            checkpoint,
            worker_started_at,
            ticket_id,
            last_known_str,
        )
        if not stale_decision:
            # Stale-worker cap reached — subsession already closed.
            return (False, stale_context)

        # Check the blocked-resume cap (independent of stale-worker).
        if blocked_count >= _MAX_BLOCKED_RESUMES:
            summary = (
                f"Ticket {ticket_id} has been BLOCKED on every resume "
                f"for {blocked_count} consecutive attempts.  The agent "
                f"is cycling without making progress — this typically "
                f"means a footprint violation, merge conflict, or "
                f"other CI gate that the assistant cannot fix on its "
                f"own.  Close the monitor so the operator can intervene "
                f"(e.g. manually revert base-branch files, resolve the "
                f"conflict, or invoke a branch-revert task)."
            )
            closed = env.registry.mark_closed(
                sub_id,
                summary=summary,
                reason="repeated_blocked",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(closed, summary, "repeated_blocked")
            logger.warning(
                "Subsession %s (ticket %s): blocked %d consecutive "
                "times on resume — closing to prevent futile retries.",
                sub_id,
                ticket_id,
                blocked_count,
            )
            return (False, summary)

        # Under the blocked-resume cap.  Use the stale-worker context if
        # present; otherwise build the standard blocked-context message.
        if stale_context is not None:
            context = stale_context
        else:
            context = (
                f"[System note: this ticket monitor was restarted after a "
                f"service restart.  Ticket {ticket_id} is currently BLOCKED "
                f"(previous known state: {last_known_str}).  Fetch the ticket "
                f"history and comments before deciding whether to auto-resume "
                f"transient failures (provider timeouts, sandbox 503s) or "
                f"escalate substantive blockers to the operator.  "
                f"IMPORTANT: merge/rebase conflicts are substantive blockers "
                f"— do NOT auto-resume these; the assistant has no "
                f"conflict-resolution tools so retrying is futile.  Surface "
                f"them to the operator immediately via user_chat.]"
            )

        if blocked_count > 1:
            remaining = _MAX_BLOCKED_RESUMES - blocked_count
            context += (
                f"\n\n[Repeated block: this is blocked-resume attempt "
                f"{blocked_count}/{_MAX_BLOCKED_RESUMES} "
                f"({remaining} remaining before auto-close).  "
                f"If the same failure keeps recurring, stop auto-retrying "
                f"and escalate to the operator.]"
            )

        logger.info(
            "Subsession %s (ticket %s): blocked on resume "
            "(attempt %d/%d) — injecting context.",
            sub_id,
            ticket_id,
            blocked_count,
            _MAX_BLOCKED_RESUMES,
        )
        return (True, context)

    # Human-issue-approval → inject context and update checkpoint so the
    # periodic loop can detect the stuck state and auto-escalate.
    if current_state_str.lower() in _TICKET_STATE_HUMAN_APPROVAL:
        context = (
            f"[System note: this ticket monitor was restarted after a "
            f"service restart.  Ticket {ticket_id} is currently "
            f"HUMAN_ISSUE_APPROVAL (previous known state: {last_known_str}). "
            f"Update the checkpoint via set_checkpoint with "
            f"last_known_state='human_issue_approval' so the system can "
            f"auto-escalate after a configurable number of consecutive "
            f"NO_CHANGE runs while the ticket is stuck awaiting human "
            f"approval.]"
        )
        checkpoint["last_known_state"] = current_state_str
        env.registry.update_checkpoint(sub_id, checkpoint)
        logger.info(
            "Subsession %s (ticket %s): human_issue_approval on resume "
            "— injecting context and updating checkpoint.",
            sub_id,
            ticket_id,
        )
        return (True, context)

    # Unchanged / open / in_progress → continue normally.
    context = (
        f"[System note: this ticket monitor was restarted after a "
        f"service restart.  Ticket {ticket_id} state is "
        f"'{current_state_str}' (was '{last_known_str}' before restart). "
        f"Continue monitoring normally.]"
    )
    logger.info(
        "Subsession %s (ticket %s): state %s on resume — continuing.",
        sub_id,
        ticket_id,
        current_state_str,
    )
    return (True, context)


async def _handle_mill_unreachable(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
) -> bool:
    """Increment the mill-failure counter; pause with backoff at the cap.

    Under ``_MAX_MILL_FAILURES`` this just increments the counter and
    returns ``True`` (the worker continues to its next turn normally).

    At the cap and above the subsession enters a **recovery loop**:
    sleep with exponential backoff, probe mill health, then retry.
    The subsession is only permanently closed after
    ``mill_recovery_max_retries`` additional retries beyond the cap.

    Returns ``True`` when the subsession should continue, ``False`` when
    all retries are exhausted and the subsession was closed (summary
    delivered to the parent conversation).
    """
    cfg = env.settings.subsessions
    checkpoint = info.checkpoint or {}
    failures = checkpoint.get("consecutive_mill_failures")
    count = int(failures) if isinstance(failures, (int, float)) else 0
    count += 1
    checkpoint["consecutive_mill_failures"] = count
    env.registry.update_checkpoint(sub_id, checkpoint)

    if count < _MAX_MILL_FAILURES:
        logger.warning(
            "Subsession %s: mill unreachable (%d/%d) — will retry on next run.",
            sub_id,
            count,
            _MAX_MILL_FAILURES,
        )
        return True

    # At or above the cap — enter recovery with exponential backoff.
    # Retry number is zero-indexed counting from the cap.
    retry_num = count - _MAX_MILL_FAILURES

    if retry_num >= cfg.mill_recovery_max_retries:
        summary = (
            f"Mill unreachable for {count} consecutive status checks "
            f"({retry_num} recovery retries) — closing subsession."
        )
        closed = env.registry.mark_closed(
            sub_id,
            summary=summary,
            reason="mill_unreachable",
            closed_by="system",
        )
        if closed is not None:
            await env.delivery.deliver_summary(closed, summary, "mill_unreachable")
        logger.warning(
            "Subsession %s: mill unreachable %d times — closed.",
            sub_id,
            count,
        )
        return False

    # Compute backoff and sleep.
    backoff = min(
        cfg.mill_recovery_initial_backoff_seconds * (2**retry_num),
        cfg.mill_recovery_max_backoff_seconds,
    )
    logger.info(
        "Subsession %s: mill unreachable — entering recovery "
        "(retry %d/%d, sleeping %.0fs).",
        sub_id,
        retry_num + 1,
        cfg.mill_recovery_max_retries,
        backoff,
    )
    env.registry.set_status(
        sub_id,
        SubsessionStatus.SLEEPING,
    )

    await asyncio.sleep(backoff)

    # Re-fetch after sleeping — the subsession may have been closed
    # externally while we waited.
    current = env.registry.get(sub_id)
    if current is None or not current.is_active:
        logger.info(
            "Subsession %s: no longer active after recovery sleep — exiting.",
            sub_id,
        )
        return False

    # Probe mill health.  Use the same board_url derivation as
    # _check_resume_status so the health probe targets the same host.
    direct_repo = getattr(env.settings, "direct_repo", None)
    board_url = (
        getattr(direct_repo, "board_api_base_url", "")
        if direct_repo is not None
        else ""
    )
    if board_url:
        started_at = await _get_mill_started_at(board_url)
        if started_at is not None:
            logger.info(
                "Subsession %s: mill health probe succeeded — "
                "resetting failure counter and resuming.",
                sub_id,
            )
            _reset_mill_failure_counter(env, current, sub_id)
            return True

    # Still unreachable — the counter stays incremented; the next call
    # (from the caller's next resume attempt) will increment again and
    # either sleep longer or close.
    logger.warning(
        "Subsession %s: mill still unreachable after recovery sleep — "
        "will retry on next cycle.",
        sub_id,
    )
    return True


def _reset_mill_failure_counter(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
) -> None:
    """Reset the consecutive-mill-failures counter to zero on success."""
    checkpoint = info.checkpoint or {}
    if checkpoint.get("consecutive_mill_failures"):
        checkpoint["consecutive_mill_failures"] = 0
        env.registry.update_checkpoint(sub_id, checkpoint)

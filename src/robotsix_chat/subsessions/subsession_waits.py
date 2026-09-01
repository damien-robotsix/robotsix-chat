"""Subsession wait-loop machinery — paused/queued/event-driven wait state.

Extracted from :mod:`robotsix_chat.subsessions.worker`.  These functions
manage wait-state polling and checkpointing for paused, queued, and
event-driven (``wait_for_event``) monitors.  They depend only on
``SubsessionEnv``/``SubsessionRegistry`` settings and the shared helper
predicates in :mod:`~robotsix_chat.subsessions.worker`; they share no
mutable state with the turn-execution core.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import httpx

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE, subsession_result_frame

from .models import InboxMessage, SubsessionInfo, SubsessionKind, SubsessionStatus
from .registry import SubsessionRegistry
from .worker import (
    _NO_CHANGE_SENTINEL,
    _QUEUED_SENTINEL,
    _is_duplicate_reply,
    _is_no_change,
    _render_turn_input,
    _truncate,
)

if TYPE_CHECKING:
    from .worker import SubsessionEnv

logger = logging.getLogger(__name__)


# How long a paused monitor blocks on wait_for_inbox before looping —
# the watcher sends an inbox message immediately when it detects a
# ticket-state change, so the timeout is only a safety net.
_PAUSED_WAIT_TIMEOUT_SECONDS: float = 300.0


async def _query_mill_ticket_state(
    board_url: str, ticket_id: str, sub_id: str
) -> str | None:
    """Return the current state string for *ticket_id*, or ``None`` on error.

    A lightweight copy of :func:`watcher._query_ticket_state` used by
    :func:`_paused_wait_loop` for per-monitor long-polling — avoids a
    circular import from the watcher module.
    """
    try:
        base = httpx.URL(board_url.rstrip("/"))
        ticket_url = base.copy_with(path=f"/tickets/{ticket_id}")
    except Exception:
        logger.exception("Could not construct ticket URL for subsession %s", sub_id)
        return None

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.get(str(ticket_url))
            response.raise_for_status()
            ticket_data: dict[str, object] = response.json()
    except httpx.HTTPStatusError as exc:
        logger.debug(
            "Long-poll: mill returned %d for ticket %s (subsession %s)",
            exc.response.status_code,
            ticket_id,
            sub_id,
        )
        return None
    except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
        logger.debug(
            "Long-poll: mill unreachable for ticket %s (subsession %s): %s",
            ticket_id,
            sub_id,
            exc,
        )
        return None
    except Exception:
        logger.exception(
            "Long-poll: unexpected error querying mill for ticket %s (subsession %s)",
            ticket_id,
            sub_id,
        )
        return None

    state = ticket_data.get("state")
    return (
        state if isinstance(state, str) else str(state) if state is not None else None
    )


async def _paused_wait_loop(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    previous_result: str | None,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Block until a ticket-state-change signal arrives or the worker is cancelled.

    Called after ``mark_paused`` — the worker stays alive and waits on
    the inbox event (watcher-sent wake messages) AND polls the mill API
    directly at a shorter long-poll interval.  When either mechanism
    detects a state change, this returns ``(pending, previous_result,
    consecutive_no_change)`` so the worker can resume its normal periodic
    loop.  Returns ``None`` when the subsession was externally closed
    while paused.
    """
    registry = env.registry

    # -- long-poll setup -------------------------------------------------
    checkpoint = info.checkpoint or {}
    ticket_id_raw = checkpoint.get("ticket_id")
    ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
    last_known_state = checkpoint.get("last_known_state")
    last_known_str: str | None = (
        (
            last_known_state
            if isinstance(last_known_state, str)
            else str(last_known_state)
        )
        if last_known_state is not None
        else None
    )

    direct_repo = getattr(env.settings, "direct_repo", None)
    board_url: str = (
        getattr(direct_repo, "board_api_base_url", "")
        if direct_repo is not None
        else ""
    )
    long_poll_interval: float = getattr(
        env.settings.subsessions,
        "paused_monitor_long_poll_interval_seconds",
        15.0,
    )
    auto_resume_seconds: float = getattr(
        env.settings.subsessions,
        "paused_monitor_auto_resume_seconds",
        1800.0,
    )
    can_long_poll = bool(
        board_url and ticket_id and last_known_str and long_poll_interval > 0
    )
    if can_long_poll:
        logger.debug(
            "Subsession %s: long-poll enabled for ticket %s "
            "(interval=%.0fs, last_known=%s).",
            sub_id,
            ticket_id,
            long_poll_interval,
            last_known_str,
        )
    # --------------------------------------------------------------------

    async def _try_resume(
        reason: str,
        pending: list[InboxMessage] | None = None,
    ) -> tuple[list[InboxMessage], str | None, int] | None:
        """Resume the subsession and publish an SSE notification.

        *pending* — inbox messages to hand to the worker on its next
        turn (e.g. the parent message that triggered the resume).
        When ``None`` (long-poll wake), defaults to an empty list.
        """
        resumed = registry.resume(sub_id)
        if resumed is None:
            return None
        if env.event_sink is not None:
            env.event_sink.publish(
                info.owner_session_id,
                {
                    "type": SSE_NOTIFICATION_TYPE,
                    "title": f"Monitor resumed: {info.title}",
                    "body": (
                        f"Monitor {sub_id[:8]} tracking ticket {ticket_id} "
                        f"resumed after {reason}."
                    ),
                    "urgency": "low",
                    "link": ticket_id,
                },
            )
        if pending is None:
            pending = []
        # Reset the no-change counter on resume: the monitor was
        # explicitly woken (inbox message, state change, or timeout)
        # and should get a fresh evaluation cycle rather than
        # immediately re-triggering the auto-pause threshold.
        return pending, previous_result, 0

    paused_at = time.monotonic()
    while True:
        timeout = long_poll_interval if can_long_poll else _PAUSED_WAIT_TIMEOUT_SECONDS
        # Cap the per-iteration timeout so the auto-resume check fires on time.
        if auto_resume_seconds > 0:
            remaining = auto_resume_seconds - (time.monotonic() - paused_at)
            if remaining <= 0:
                logger.info(
                    "Subsession %s: paused for %.0fs (limit %.0fs) — auto-resuming.",
                    sub_id,
                    time.monotonic() - paused_at,
                    auto_resume_seconds,
                )
                return await _try_resume(
                    "auto-resume timeout",
                    pending=registry.drain_inbox(sub_id),
                )
            timeout = min(timeout, remaining)
        woke = await registry.wait_for_inbox(sub_id, timeout=timeout)

        # Verify the subsession is still paused.
        current = registry.get(sub_id)
        if current is None or current.status is not SubsessionStatus.PAUSED:
            return None

        if woke:
            # ----------- inbox wake: any message resumes -------------
            # The watcher sends system messages on ticket-state change;
            # the parent agent sends parent messages via
            # ``message_subsession`` to manually resume.  Both paths
            # wake the monitor and hand the message through so the
            # subsession agent sees it on its next turn.
            messages = registry.drain_inbox(sub_id)
            if messages:
                logger.info(
                    "Subsession %s: resume signal received via inbox — resuming.",
                    sub_id,
                )
                return await _try_resume("inbox message", pending=messages)

            # Spurious wake (event set but inbox empty) — loop and wait
            # again.
            logger.debug(
                "Subsession %s: spurious inbox wake; continuing wait.",
                sub_id,
            )
        else:
            # ----------- timeout: long-poll the mill directly -------------
            if not can_long_poll:
                # Safety-net timeout with long-poll disabled — just loop.
                continue

            if not ticket_id or not last_known_str:
                # Defensive: can_long_poll should guarantee these are set,
                # but if something mutated the checkpoint, fall through.
                continue

            current_state = await _query_mill_ticket_state(board_url, ticket_id, sub_id)
            if current_state is not None and current_state != last_known_str:
                logger.info(
                    "Subsession %s: ticket %s state changed from '%s' to '%s' "
                    "(detected via long-poll) — resuming.",
                    sub_id,
                    ticket_id,
                    last_known_str,
                    current_state,
                )
                return await _try_resume("ticket state change (long-poll)")

            # State unchanged — loop back and wait again.
            logger.debug(
                "Subsession %s: ticket %s still '%s' (long-poll) — continuing wait.",
                sub_id,
                ticket_id,
                current_state,
            )


async def _queued_wait_loop(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Block until the queued ticket changes state or the worker is cancelled.

    Called when a periodic monitor detects the monitored ticket is queued
    (waiting for implementation).  The worker stays alive and long-polls
    the mill API — no auto-pause notification is sent because the monitor
    proactively chose this wait rather than being forced into it.

    When a state change is detected this returns ``(pending,
    previous_result, consecutive_no_change)`` so the worker resumes its
    normal periodic loop.  Returns ``None`` when the subsession was
    externally closed while waiting.
    """
    registry = env.registry

    # -- long-poll setup -------------------------------------------------
    checkpoint = info.checkpoint or {}
    ticket_id_raw = checkpoint.get("ticket_id")
    ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
    last_known_state = checkpoint.get("last_known_state")
    last_known_str: str | None = (
        (
            last_known_state
            if isinstance(last_known_state, str)
            else str(last_known_state)
        )
        if last_known_state is not None
        else None
    )

    direct_repo = getattr(env.settings, "direct_repo", None)
    board_url: str = (
        getattr(direct_repo, "board_api_base_url", "")
        if direct_repo is not None
        else ""
    )
    # Reuse the paused-monitor long-poll interval — the queued wait has
    # the same mechanics (poll the mill for ticket state changes).
    long_poll_interval: float = getattr(
        env.settings.subsessions,
        "paused_monitor_long_poll_interval_seconds",
        15.0,
    )
    # Auto-resume after this many seconds to prevent a stale queued wait
    # from hanging forever (e.g. if the ticket state changed but the
    # long-poll missed it).  Shorter than the paused auto-resume because
    # the monitor is actively queued, not abandoned.
    auto_resume_seconds: float = getattr(
        env.settings.subsessions,
        "paused_monitor_auto_resume_seconds",
        1800.0,
    )
    can_long_poll = bool(
        board_url and ticket_id and last_known_str and long_poll_interval > 0
    )
    if can_long_poll:
        logger.debug(
            "Subsession %s: queued long-poll enabled for ticket %s "
            "(interval=%.0fs, last_known=%s).",
            sub_id,
            ticket_id,
            long_poll_interval,
            last_known_str,
        )
    # --------------------------------------------------------------------

    async def _wake_from_queued(
        reason: str,
        pending: list[InboxMessage] | None = None,
    ) -> tuple[list[InboxMessage], str | None, int] | None:
        """Wake from the queued wait — set SLEEPING and publish a quiet event."""
        current = registry.get(sub_id)
        if current is None or not current.is_active:
            return None
        registry.set_status(
            sub_id,
            SubsessionStatus.SLEEPING,
            runs=current.runs,
            next_run_at=registry.now() + (info.interval_seconds or 60.0),
        )
        if env.event_sink is not None:
            env.event_sink.publish(
                info.owner_session_id,
                {
                    "type": SSE_NOTIFICATION_TYPE,
                    "title": f"Monitor unqueued: {info.title}",
                    "body": (
                        f"Monitor {sub_id[:8]} tracking ticket {ticket_id} "
                        f"woke from queued wait ({reason})."
                    ),
                    "urgency": "low",
                    "link": ticket_id,
                },
            )
        if pending is None:
            pending = []
        return pending, previous_result, consecutive_no_change

    queued_at = time.monotonic()
    while True:
        timeout = long_poll_interval if can_long_poll else _PAUSED_WAIT_TIMEOUT_SECONDS
        # Cap the per-iteration timeout so the auto-resume check fires on time.
        if auto_resume_seconds > 0:
            remaining = auto_resume_seconds - (time.monotonic() - queued_at)
            if remaining <= 0:
                logger.info(
                    "Subsession %s: queued for %.0fs (limit %.0fs) — auto-resuming.",
                    sub_id,
                    time.monotonic() - queued_at,
                    auto_resume_seconds,
                )
                return await _wake_from_queued(
                    "auto-resume timeout",
                    pending=registry.drain_inbox(sub_id),
                )
            timeout = min(timeout, remaining)
        woke = await registry.wait_for_inbox(sub_id, timeout=timeout)

        # Verify the subsession is still active.
        current = registry.get(sub_id)
        if current is None or not current.is_active:
            return None

        if woke:
            # ----------- inbox wake: any message resumes -------------
            messages = registry.drain_inbox(sub_id)
            if messages:
                logger.info(
                    "Subsession %s: resume signal received while queued — waking.",
                    sub_id,
                )
                return await _wake_from_queued("inbox message", pending=messages)

            # Spurious wake (event set but inbox empty) — loop and wait
            # again.
            logger.debug(
                "Subsession %s: spurious inbox wake while queued; continuing wait.",
                sub_id,
            )
        else:
            # ----------- timeout: long-poll the mill directly -------------
            if not can_long_poll:
                continue

            if not ticket_id or not last_known_str:
                continue

            current_state = await _query_mill_ticket_state(board_url, ticket_id, sub_id)
            if current_state is not None and current_state != last_known_str:
                logger.info(
                    "Subsession %s: ticket %s state changed from '%s' to '%s' "
                    "(detected via long-poll while queued) — waking.",
                    sub_id,
                    ticket_id,
                    last_known_str,
                    current_state,
                )
                return await _wake_from_queued("ticket state change (long-poll)")

            # State unchanged — loop back and wait again.
            logger.debug(
                "Subsession %s: ticket %s still '%s' (queued long-poll) — "
                "continuing wait.",
                sub_id,
                ticket_id,
                current_state,
            )


def _build_wait_for_event_input(
    info: SubsessionInfo,
    previous_result: str | None,
    steering: list[InboxMessage],
    *,
    sub_id: str = "",
    registry: SubsessionRegistry | None = None,
) -> str:
    """Compose one event-driven monitor turn's input."""
    parts = [info.prompt]
    if info.include_previous_result and previous_result is not None:
        parts.append(f"Previous run result:\n{previous_result}")
    if steering:
        parts.append(
            "Event(s) received since the last run:\n" + _render_turn_input(steering)
        )
    parts.append(
        "You are an event-driven monitor — you wake only when a mill "
        "ticket state-change event arrives or when a safety-net timeout "
        "fires.  You are operating exactly as designed.\n\n"
        "DETECTION-ONLY — no surfacing authority: you are a read-only "
        "monitor.  You MUST NOT post [NEEDS-OPERATOR] marker comments, "
        "call notify_user, file tickets, or perform any durable surfacing "
        "action.  Your sole surfacing mechanism is complete_subsession "
        "when a terminal state or blocked-human-decision state is "
        "reached.  Any instruction from the main conversation prompt "
        "that directs you to post markers, file tickets, or notify "
        "users is overridden by this rule.\n\n"
        "CRITICAL — reporting contract: your replies are NOT delivered "
        "to the parent conversation.  The only way to communicate with "
        "the parent is by calling complete_subsession(summary).  "
        "Intermediate progress — state transitions, status updates, "
        "observations, commentary — stays inside this subsession and is "
        "never seen by the parent.  Call complete_subsession ONLY when:\n"
        "  1. A final, verified terminal state is reached (ticket done, "
        "merged, closed), or\n"
        "  2. User intervention is required (ticket blocked on a decision, "
        "escalation needed, unrecoverable failure).\n"
        "For all other runs — including state transitions that do not "
        f"reach a terminal state — reply {_NO_CHANGE_SENTINEL} if nothing "
        "changed, or reply with a concise acknowledgment if something did "
        "change (the parent will not see it, but the transcript records "
        "what you observed).\n\n"
        "CRITICAL — summary formatting: when you call complete_subsession, "
        "your summary will be shown directly to a human operator.  Write "
        "in plain, user-facing language — omit ALL internal technical "
        "details.  Never include block IDs, event numbers, state machine "
        "transitions, spawn counters, internal timeout values, stack "
        "traces, or raw API response fragments.  State the actionable "
        "conclusion first, then any context the operator needs.  For "
        "example, instead of 'event 35 triggered stall guard escalation "
        "after spawn counter reset at block a3f2, history events 20-35,' "
        "write 'Publishing workflow stalled because the ticket scope was "
        "too broad — suggest splitting into a canary-only ticket.'  The "
        "operator should understand the outcome without ever seeing an "
        "internal identifier.\n\n"
        "CRITICAL — first-run guard: if there is NO 'Previous run result' "
        "section above, this is the first observation cycle.  You have no "
        "baseline to compare against.  Your only job is to observe the "
        "current live state (fetch the ticket from the board API), record "
        "what you found, and reply NO_CHANGE — nothing changed because "
        "there is no prior state.  Do NOT invent a prior surfacing or claim "
        "you already reported something.  On subsequent runs you will see a "
        "'Previous run result' section and can then compare against it.\n\n"
        "CRITICAL — strict verify-first policy: you are a read-only "
        "monitor.  You MUST NOT infer, guess, or fabricate any state "
        "change or outcome.  Before reporting ANY state change, transition, "
        "or terminal outcome in a complete_subsession summary, you MUST do "
        "a live GET of the ticket from the board API (e.g. fetch the ticket "
        "endpoint, re-read the ticket description and comments).  Only "
        "report what the live API returns — never "
        "trust a state you only recall from an earlier turn or infer from "
        "conversation context.  A state transition that happened between "
        "polls (e.g. draft → ready → in_progress) MUST be detected from the "
        "live query, not from your memory.  If the live API response "
        "conflicts with your recollection, the live API response is "
        "authoritative — report that, and discard the recollection.\n\n"
        "Tool fallback: use the component_request tool to fetch ticket "
        "state from the board API.  If component_request is not among "
        "your tools, use the ticket_poll tool instead — it queries the "
        "same board API directly.  If neither tool is available, call "
        "complete_subsession with a summary recommending the monitor be "
        "paused — do not silently loop.\n\n"
        f"Reply with the single word {_NO_CHANGE_SENTINEL} — and nothing "
        "else, no punctuation, no commentary — only if genuinely nothing "
        "changed since the previous run: compare the live board state against "
        "the state shown in the 'Previous run result' section above (if "
        "present). If that section is absent, this is the first run — "
        "reply NO_CHANGE.  If any state transition occurred "
        "(e.g. draft → implementation complete, in_progress → done, ready → "
        "in_progress) but the ticket has NOT reached a terminal state, reply "
        "with a concise acknowledgment of the change (the parent will not "
        "see this — it is for the transcript only).  DO NOT reply NO_CHANGE "
        "when a transition occurred.\n\n"
    )
    # Resolve and repair the ticket_id from the checkpoint (or fall
    # back to dedup_key).  This runs unconditionally so that the ticket_id
    # survives agent set_checkpoint calls and restarts.
    ticket_id_raw = info.checkpoint.get("ticket_id") if info.checkpoint else None
    ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
    # Fall back to dedup_key when the checkpoint has not yet recorded
    # the ticket_id — the dedup_key for ticket monitors is always the
    # ticket id, so it is authoritative even on the first run.
    if not ticket_id and info.dedup_key:
        ticket_id = info.dedup_key
        # Repair the checkpoint so the ticket_id survives agent
        # set_checkpoint calls that may have cleared it and so later
        # stages find it without needing their own fallback.
        if sub_id and registry is not None:
            checkpoint = info.checkpoint or {}
            checkpoint["ticket_id"] = ticket_id
            registry.update_checkpoint(sub_id, checkpoint)

    parts.append(
        "Decision-blocked tickets: when the monitored ticket sits at "
        "human_issue_approval, there is NO human approval loop — the main "
        "assistant is the approver.  Do not wait passively and do not "
        "reply NO_CHANGE run after run: escalate promptly so the main "
        "session reviews the spec and acts (approve to ready, send back "
        "to draft, or retire it).  Reply with a concise acknowledgment "
        "that includes a CONCRETE RECOMMENDATION: state whether you "
        "recommend approving or closing the ticket and why (e.g. "
        "'I recommend approving — this is a standard pre-authorized "
        "rollout step' or 'I recommend closing — the change is already "
        "covered by ticket X').  Then reply "
        f"{_QUEUED_SENTINEL} (and nothing else) to switch the monitor to "
        "event-driven waiting — it will stop burning your run budget and "
        "will wake automatically when the ticket leaves the "
        "human_issue_approval state.  Do NOT call complete_subsession — "
        "the monitor must continue tracking through to a terminal state.\n\n"
    )
    parts.append(
        "Terminal-state double-check + loop guard: before calling "
        "complete_subsession for a done or closed ticket, you MUST verify "
        "from three independent sources — (1) a live GET of the ticket "
        "endpoint confirming the terminal state, (2) a check of the PR/MR "
        "endpoint (e.g. the ticket's linked PRs or the merge API) confirming "
        "merge status, and (3) a check of the most recent CI workflow run "
        "for the affected pipeline (e.g. the 'Publish Docker image' workflow "
        "or the repo's primary deploy workflow).  "
        "Do NOT claim a PR was created, merged, or auto-merged unless you "
        "have confirmed it via the PR API — a terminal ticket state alone "
        "does not prove a PR exists.  Your complete_subsession summary MUST "
        "state which sources you checked and what each returned.  If a PR "
        "was merged, say so with the PR number; if no PR was involved, say "
        "'closed without a PR'; if the PR API is unreachable, say 'terminal "
        "state confirmed via ticket API; PR status could not be verified'.  "
        "\n\n"
        "MERGE-EVIDENCE CHECK before saying 'closed without a PR' (source 4): "
        "missing PR metadata — a null pr_url on the ticket, no pr_number in "
        "the checkpoint, or an empty linked-PR list — is INDETERMINATE, NOT "
        "proof the ticket was dropped.  Before you classify a closure as "
        "'closed without a PR' or 'closed without implementation', you MUST "
        "cross-reference the ticket's state-change history (GET "
        "/tickets/{id}/history or the events array in the ticket data) for "
        "evidence of a merge or implementation step — e.g. a transition "
        "through implement_complete / waiting_auto_merge / "
        "human_mr_approval, a 'PR merged' / 'pull request merged' "
        "event, a linked-PR number appearing in the history, or a done "
        "transition that follows implementation work.  If the history shows "
        "ANY such evidence, the ticket SHIPPED: report 'closed — work "
        "delivered' with the PR number (or the merge event you found), NOT "
        "'closed without a PR'.  Only assert 'closed without a PR' when the "
        "history shows no implementation or merge activity at all.  If you "
        "cannot confirm either way (history unreachable, ambiguous events), "
        "treat it as indeterminate: say 'closed; could not confirm whether a "
        "PR was merged — verification recommended' and prompt the user to "
        "verify, rather than asserting the ticket was dropped.  This guards "
        "against the false-positive pattern where a ticket whose PR was "
        "actually merged is wrongly reported as closed without one.\n\n"
        "LOOP GUARD — CI workflow verification (source 3): after a ticket "
        "closes, query the GitHub Actions API (via component_request or "
        "the equivalent GitHub API tool) for the most recent run of the "
        "repo's primary publish/deploy workflow.  The complete_subsession "
        "tool has a PROGRAMMATIC GATE: it will REJECT any summary that "
        "does not mention 'CI workflow', 'workflow run', 'pipeline', "
        "'GitHub Actions', 'publish', 'deploy workflow', or 'could not be "
        "verified'.  You must include at least one of these phrases in "
        "your summary to pass the gate.  Simply stating the ticket is "
        "closed without CI evidence will be rejected.\n\n"
        "If the workflow run failed or is still in progress with failures "
        "on prior runs, do NOT call complete_subsession with a success "
        "summary — the fix did not actually resolve the pipeline failure.  "
        "Instead:\n"
        "  - If the workflow failed: call complete_subsession with a "
        "summary that INCLUDES the workflow failure details (run id, "
        "failure reason, and log excerpt if available).  The summary must "
        "make clear that the ticket was closed but the CI pipeline is "
        "still failing — this breaks the redraft loop.  Then, AFTER "
        "complete_subsession returns, call spawn_subsession to file a "
        "new diagnostic ticket with the workflow failure details so the "
        "operator can see the pipeline is still broken.\n"
        "  - If the workflow API is unreachable: try at least twice with "
        "a 5-second pause between attempts.  If still unreachable, call "
        "complete_subsession with a summary stating 'terminal state "
        "confirmed via ticket API; CI workflow status could not be "
        "verified' — do NOT silently skip the check.\n"
        "  - If the workflow passed: proceed with the config/status check "
        "below before calling complete_subsession — a green deploy/publish "
        "pipeline proves the build and deploy succeeded, not that the "
        "feature is live.\n"
        "  - Config/status check (deploy-complete step): when the "
        "deploy/publish workflow has passed, confirm the feature is "
        "actually live before claiming it is deployed.  Query the live "
        "configuration of the deployed component (via its config endpoint, "
        "the component_request tool, or reading the deployed config) and "
        "check whether the feature you shipped is enabled.  Many features "
        "ship default-off — e.g. `continuation.enabled` defaults to "
        "`false` — and the live config may have no override.  If the "
        "feature is still disabled in the live config, do NOT report it "
        "as 'deployed and working'.  Instead, alert the user in your "
        "complete_subsession summary (or chat reply) that the feature "
        "shipped but is disabled by default, and recommend the exact "
        "config key/value needed to enable it for testing (e.g. set "
        "`continuation.enabled` to `true` in the component config).  Only "
        "report the feature as verified end-to-end when the live config "
        "actually enables it.\n"
        "Call complete_subsession only after all checks (terminal state, "
        "PR, CI workflow, and live config) are complete."
    )
    parts.append(
        "CRITICAL — checkpoint PR tracking: whenever you detect or create "
        "a PR associated with the monitored ticket (via open_direct_repo_pr "
        "or by querying the GitHub API), store the PR number and repository "
        "in this subsession's checkpoint using set_checkpoint with "
        "'pr_number' (int) and 'repo_full_name' (str, e.g. "
        "'owner/repo').  Include the existing checkpoint fields "
        "(ticket_id, last_known_state, human_approval_since) alongside "
        "the new PR fields — the checkpoint is replaced wholesale.  "
        "This enables the background watcher to detect PR merges and "
        "auto-resume the monitor after a merge event, even when the "
        "board ticket state has not yet been updated.  If you previously "
        "created a PR and it was later merged or closed, update the "
        "checkpoint to remove stale PR information."
    )
    return "\n\n".join(parts)


async def _event_wait_loop(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Block until a ticket-state-change event arrives or a safety-net timeout fires.

    Returns ``(pending, previous_result, consecutive_no_change)`` when the
    subsession should run a turn, or ``None`` when it was externally closed
    while waiting.
    """
    registry = env.registry
    checkpoint = info.checkpoint or {}
    ticket_id_raw = checkpoint.get("ticket_id")
    ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
    # Fall back to dedup_key when the checkpoint has not yet recorded
    # the ticket_id — the dedup_key for ticket monitors is always the
    # ticket id, so it is authoritative even on the first run.
    if not ticket_id and info.dedup_key:
        ticket_id = info.dedup_key
        # Repair the checkpoint so the ticket_id survives agent
        # set_checkpoint calls that may have cleared it.  Without
        # this write-back, a restart would lose the id and the
        # dedup_key fallback might not be available on resume if
        # the dedup_key was also not persisted.
        checkpoint["ticket_id"] = ticket_id
        registry.update_checkpoint(sub_id, checkpoint)
        logger.debug(
            "Subsession %s: ticket_id %r recovered from dedup_key; "
            "written to checkpoint.",
            sub_id,
            ticket_id,
        )

    if not ticket_id:
        logger.error(
            "Subsession %s: WAIT_FOR_EVENT subsession has no ticket_id — "
            "the checkpoint is missing the key and no dedup_key is set.  "
            "Closing.",
            sub_id,
        )
        closed = registry.mark_closed(
            sub_id,
            summary=(
                f"Wait-for-event monitor '{info.title}' has no recoverable "
                f"ticket_id — the checkpoint is missing the ticket_id key "
                f"and no dedup_key is set.  This monitor cannot operate "
                f"without a target ticket; respawn it with a valid "
                f"dedup_key (ticket id)."
            ),
            reason="missing_ticket_id",
            closed_by="system",
        )
        if closed is not None:
            await env.delivery.deliver_summary(
                closed,
                f"No recoverable ticket_id for monitor '{info.title}'",
                "missing_ticket_id",
            )
        return None

    timeout = info.event_timeout_seconds
    if timeout is None:
        timeout = env.settings.subsessions.event_driven_timeout_seconds

    # Cheap-check setup: on a safety-net timeout, read the ticket state
    # straight from the board before spending an agent turn.  An
    # unchanged state means no event was lost, so the wait is simply
    # re-armed.  Without this every quiet 15-minute window cost a full
    # LLM turn per monitor (~96 turns/day for a ticket parked on a human
    # decision), all of them replying NO_CHANGE.
    direct_repo = getattr(env.settings, "direct_repo", None)
    board_url: str = (
        getattr(direct_repo, "board_api_base_url", "")
        if direct_repo is not None
        else ""
    )
    max_silent = int(
        getattr(env.settings.subsessions, "event_driven_max_silent_timeouts", 0)
    )
    silent_timeouts = 0

    while True:
        registry.register_event_waiter(sub_id, ticket_id)
        try:
            woke = await registry.wait_for_inbox(sub_id, timeout=timeout)
        finally:
            registry.unregister_event_waiter(sub_id, ticket_id)

        # Verify subsession is still active.
        current = registry.get(sub_id)
        if current is None or not current.is_active:
            return None

        if woke:
            pending = registry.drain_inbox(sub_id)
            if pending:
                return pending, previous_result, consecutive_no_change

        # Timeout with no event.  Skip the agent turn while a direct board
        # read confirms the ticket is still where the monitor last saw it —
        # up to ``max_silent`` consecutive times, after which the real
        # safety-net turn runs regardless.
        if board_url and max_silent > 0 and silent_timeouts < max_silent:
            cp = current.checkpoint or {}
            last_known_raw = cp.get("last_known_state")
            last_known = (
                last_known_raw
                if isinstance(last_known_raw, str)
                else (str(last_known_raw) if last_known_raw is not None else "")
            )
            if last_known:
                state = await _query_mill_ticket_state(board_url, ticket_id, sub_id)
                if state is not None and state.lower() == last_known.lower():
                    silent_timeouts += 1
                    logger.debug(
                        "Subsession %s: safety-net timeout, ticket %s still "
                        "%r — re-arming wait without an agent turn (%d/%d).",
                        sub_id,
                        ticket_id,
                        state,
                        silent_timeouts,
                        max_silent,
                    )
                    continue

        # Timeout — create a safety-net message so the agent runs anyway.
        pending = [
            InboxMessage(
                role="system",
                text=(
                    "Safety-net timeout fired — no ticket state-change event "
                    "was received.  Perform a routine check of the monitored "
                    "ticket as you normally would."
                ),
                timestamp=registry.now(),
            )
        ]
        return pending, previous_result, consecutive_no_change


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
    errored run of a new streak).

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
        summary = (
            f"Failed after {consecutive_errored} consecutive errored runs. "
            f"Last error: {error_msg}"
        )
        failed = registry.fail(sub_id, error=summary)
        if failed is not None:
            await env.delivery.deliver_summary(failed, summary, "failed")
        return True, previous_result, consecutive_no_change

    # Notify the parent at most once per streak (on the first errored run).
    if consecutive_errored == 1 and env.event_sink is not None:
        env.event_sink.publish(
            info.owner_session_id,
            {
                "type": SSE_NOTIFICATION_TYPE,
                "title": f"Monitor run error: {info.title}",
                "body": (
                    f"Monitor {sub_id[:8]} had an errored run "
                    f"({result_label}): {_truncate(error_msg, 200)}. "
                    f"The monitor is still alive and will retry on the "
                    f"next cycle."
                ),
                "urgency": "low",
                "link": info.dedup_key or sub_id,
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
            "Subsession %s: ticket_id %r recovered from dedup_key "
            "after agent turn; written to checkpoint.",
            sub_id,
            info.dedup_key,
        )
    last_known = checkpoint.get("last_known_state", "")
    if isinstance(last_known, str) and last_known.lower() == "human_issue_approval":
        # Track human_approval_since for informational purposes —
        # the monitor stays alive and event-driven; no timeout closure.
        now = registry.now()
        human_approval_since_raw = checkpoint.get("human_approval_since")
        if not isinstance(human_approval_since_raw, (int, float)):
            checkpoint["human_approval_since"] = now
            registry.update_checkpoint(sub_id, checkpoint)

    # Re-arm the wait — no auto-pause, auto-stop, or max_runs for
    # event-driven monitors.
    return [], previous_result, consecutive_no_change

"""Periodic subsession turn handling.

Extracted from :mod:`robotsix_chat.subsessions.worker` to keep that module
focused on the shared turn loop and input-rendering helpers.  This module
owns the periodic-turn input builder and the PERIODIC post-turn logic; it
imports shared helpers from ``.worker`` (input rendering, no-change/queued
detection, the queued wait loop).

Importing shared helpers from ``.worker`` is safe: ``worker`` imports this
module lazily inside ``_subsession_worker`` / ``_handle_kind_continuation``
only after ``worker`` itself is fully initialised, so there is no import
cycle.
"""

from __future__ import annotations

import logging

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE, subsession_result_frame

from .models import InboxMessage, SubsessionInfo, SubsessionStatus
from .registry import SubsessionRegistry
from .worker import (
    _NO_CHANGE_SENTINEL,
    _QUEUED_SENTINEL,
    SubsessionEnv,
    _format_duration,
    _is_duplicate_reply,
    _is_no_change,
    _is_queued,
    _is_ticket_pre_authorized,
    _ordinal_suffix,
    _paused_wait_loop,
    _queued_wait_loop,
    _render_turn_input,
)

logger = logging.getLogger(__name__)


def _build_periodic_input(
    info: SubsessionInfo,
    previous_result: str | None,
    steering: list[InboxMessage],
    pre_authorized_patterns: list[str] | None = None,
    *,
    sub_id: str = "",
    registry: SubsessionRegistry | None = None,
) -> str:
    """Compose one periodic tick's turn input."""
    parts = [info.prompt]
    if info.include_previous_result and previous_result is not None:
        parts.append(f"Previous run result:\n{previous_result}")
    if steering:
        parts.append(
            "New instructions received since the last run:\n"
            + _render_turn_input(steering)
        )
    parts.append(
        "You are a periodic monitor — being spawned directly from "
        "a conversation as a periodic subsession is the standard, "
        "fully supported workflow for ticket monitors.  You are "
        "operating exactly as designed.\n\n"
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
        "QUEUED TICKETS — when the monitored ticket is in a queue state "
        "(waiting for implementation, i.e. the ticket is in 'ready', "
        "'in_progress', 'implement', or any non-terminal pipeline stage "
        "where no agent is actively working on it), do NOT reply "
        f"{_NO_CHANGE_SENTINEL} run after run — that causes the monitor "
        "to auto-pause after a few cycles.  Instead, reply "
        f"{_QUEUED_SENTINEL} (and nothing else).  The system will then "
        "switch to event-driven waiting: it will stop burning your "
        "no-change quota and will long-poll the board API for a state "
        "change, waking you the moment the ticket leaves the queue.  "
        "You MUST use the queued sentinel for tickets stuck in the "
        "implementation queue — NOT for tickets that are actively "
        "progressing through a pipeline stage.\n\n"
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
        "CRITICAL — strict verify-first policy: you are a read-only "
        "monitor.  You MUST NOT infer, guess, or fabricate any state "
        "change or outcome.  Before reporting ANY state change, transition, "
        "or terminal outcome in a complete_subsession summary, you MUST do "
        "a live GET of the ticket from the board API (e.g. fetch the ticket "
        "endpoint, re-read the ticket description and comments) and compare "
        "the live response against the previously verified state from the "
        "prior run's result.  Only report what the live API returns — never "
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
        "changed since the previous run: the live board state is identical "
        "to the prior run's observed state. If any state transition occurred "
        "(e.g. draft → implement_complete, in_progress → done, ready → "
        "in_progress) but the ticket has NOT reached a terminal state, reply "
        "with a concise acknowledgment of the change (the parent will not "
        "see this — it is for the transcript only).  DO NOT reply NO_CHANGE "
        "when a transition occurred.\n\n"
    )
    # Resolve and repair the ticket_id from the checkpoint (or fall
    # back to dedup_key).  This runs unconditionally — not only when
    # pre_authorized_patterns are configured — so that the ticket_id
    # survives agent set_checkpoint calls and restarts even for
    # monitors without pre-authorization rules.
    ticket_id_raw = info.checkpoint.get("ticket_id") if info.checkpoint else None
    ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
    # Fall back to dedup_key when the checkpoint has not yet recorded
    # the ticket_id — the dedup_key for ticket monitors is always the
    # ticket id, so it is authoritative even on the first run.
    if not ticket_id and info.dedup_key:
        ticket_id = info.dedup_key
        # Repair the checkpoint so the ticket_id survives agent
        # set_checkpoint calls that may have cleared it and so later
        # stages (_event_wait_loop, _run_periodic_turn) find it
        # without needing their own fallback.
        if sub_id and registry is not None:
            checkpoint = info.checkpoint or {}
            checkpoint["ticket_id"] = ticket_id
            registry.update_checkpoint(sub_id, checkpoint)

    # Inject the PRE-AUTHORIZED instruction BEFORE the
    # decision-blocked paragraph so it has priority — a monitor that
    # sees both must follow the pre-authorized directive.
    if (
        pre_authorized_patterns
        and ticket_id
        and _is_ticket_pre_authorized(ticket_id, pre_authorized_patterns)
    ):
        parts.append(
            "PRE-AUTHORIZED TICKET: this ticket has been pre-authorized "
            "under a standing operator directive.  The "
            "human_issue_approval gate does NOT apply — do not treat "
            "this ticket as decision-blocked.  Continue monitoring "
            "normally as if the approval were already granted.\n\n"
        )
    parts.append(
        "Decision-blocked tickets: when the monitored ticket is awaiting an "
        "operator decision — stuck in human_issue_approval, waiting on an "
        '"Option A or B?" choice, or otherwise blocked on a human '
        "direction — do NOT silently reply NO_CHANGE run after run.  "
        "Instead, call complete_subsession with a summary that includes a "
        "CONCRETE RECOMMENDATION: state whether you recommend approving or "
        "closing the ticket and why (e.g. 'I recommend approving — this "
        "is a standard pre-authorized rollout step' or 'I recommend closing "
        "— the change is already covered by ticket X').  Explain what "
        "decision is needed and set expectations: mention that the system "
        "will auto-escalate after the human_approval_timeout window if no "
        "decision is made.  This surfaces the blocker with actionable "
        "guidance so the operator can act on it rather than waiting for "
        "the auto-stop timeout.\n\n"
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
        "  - If the workflow passed: proceed with complete_subsession "
        "normally (the fix is confirmed effective).\n"
        "Call complete_subsession only after all three checks are complete."
    )
    parts.append(
        "CRITICAL — checkpoint PR tracking: whenever you detect or create "
        "a PR associated with the monitored ticket (via open_direct_repo_pr "
        "or by querying the GitHub API), store the PR number and repository "
        "in this subsession's checkpoint using set_checkpoint with "
        "'pr_number' (int) and 'repo_full_name' (str, e.g. "
        "'owner/repo').  Include the existing checkpoint fields "
        "(ticket_id, last_known_state, human_approval_since, and "
        "auto_stop_no_change_runs if present) alongside "
        "the new PR fields — the checkpoint is replaced wholesale.  "
        "This enables the background watcher to detect PR merges and "
        "auto-resume the monitor after a merge event, even when the "
        "board ticket state has not yet been updated.  If you previously "
        "created a PR and it was later merged or closed, update the "
        "checkpoint to remove stale PR information."
    )
    return "\n\n".join(parts)


async def _run_periodic_turn(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    reply: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Handle PERIODIC post-turn: update status, deliver, check limits, sleep.

    Returns ``None`` when the worker should stop (max_runs / auto_stop
    triggered), or ``(pending, previous_result, consecutive_no_change)``
    to continue.
    """
    registry = env.registry
    # -- queued detection: the agent found the ticket is waiting for
    #    implementation — switch to event-driven wait instead of burning
    #    no-change quota.
    if _is_queued(reply):
        runs = info.runs + 1
        registry.set_status(
            sub_id,
            SubsessionStatus.SLEEPING,
            runs=runs,
            next_run_at=registry.now() + (info.interval_seconds or 60.0),
            last_result=reply,
        )
        if env.event_sink is not None:
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
        result = await _queued_wait_loop(
            env,
            info,
            sub_id,
            previous_result,
            0,
        )
        if result is None:
            return None
        pending, previous_result, _ = result
        return pending, previous_result, 0  # reset consecutive_no_change

    suppressed = _is_no_change(reply) or _is_duplicate_reply(reply, previous_result)
    consecutive_no_change = 0 if not _is_no_change(reply) else consecutive_no_change + 1
    runs = info.runs + 1
    if info.interval_seconds is None:  # pragma: no cover - spawn validates
        raise RuntimeError("periodic subsession without an interval")
    registry.set_status(
        sub_id,
        SubsessionStatus.SLEEPING,
        runs=runs,
        next_run_at=registry.now() + info.interval_seconds,
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

    if info.max_runs is not None and runs >= info.max_runs:
        # -- max_runs escalation: track how many consecutive times this
        #    monitor has exhausted its run budget.  When the count
        #    reaches the configured threshold, auto-create a follow-up
        #    ticket on the board so the operator can review / respawn
        #    with a longer interval or budget.
        escalation_threshold = env.settings.subsessions.max_runs_escalation_threshold
        checkpoint = info.checkpoint or {}
        exhausted_count_raw = checkpoint.get("max_runs_exhausted_count")
        exhausted_count = (
            exhausted_count_raw if isinstance(exhausted_count_raw, int) else 0
        )
        exhausted_count += 1

        if escalation_threshold > 0 and exhausted_count >= escalation_threshold:
            # Threshold reached — escalate with a follow-up ticket.
            logger.warning(
                "Subsession %s: max_runs exhausted %d consecutive times "
                "(threshold=%d) — auto-escalating with follow-up ticket.",
                sub_id,
                exhausted_count,
                escalation_threshold,
            )
            # Build a follow-up ticket for the operator.
            ticket_id_raw = checkpoint.get("ticket_id")
            ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
            monitor_title = info.title or sub_id[:8]
            followup_title = (
                f"Monitor '{monitor_title}' exhausted run budget "
                f"{exhausted_count} times"
            )
            followup_desc = (
                f"Periodic monitor **{monitor_title}** (subsession "
                f"`{sub_id}`) tracking ticket `{ticket_id}` has "
                f"exhausted its `max_runs` limit ({info.max_runs} runs) "
                f"**{exhausted_count} consecutive times**.\n\n"
                f"Last result: {reply}\n\n"
                f"Consider respawning with a longer polling interval "
                f"or a higher run budget, or reviewing whether this "
                f"ticket still needs active monitoring."
            )
            # Try to create the follow-up ticket — best-effort.
            try:
                from robotsix_chat.repo.direct.board_client import BoardClient

                direct_repo_settings = getattr(env.settings, "direct_repo", None)
                if direct_repo_settings is not None:
                    board = BoardClient(direct_repo_settings)
                    followup_id = await board.create_ticket(
                        title=followup_title,
                        description=followup_desc,
                        kind="task",
                        source="agent",
                    )
                    if followup_id:
                        logger.info(
                            "Worker: created escalation ticket %s for "
                            "subsession %s (max_runs exhausted %d times).",
                            followup_id,
                            sub_id,
                            exhausted_count,
                        )
                        followup_desc += f"\n\nFollow-up ticket: {followup_id}"
            except Exception:
                logger.debug(
                    "Worker: could not create escalation ticket for "
                    "subsession %s (board may be unreachable).",
                    sub_id,
                )

            summary = (
                f"Reached the {info.max_runs}-run limit for the "
                f"{exhausted_count}{_ordinal_suffix(exhausted_count)} "
                f"consecutive time — auto-escalated. Last: {reply}"
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="max_runs_escalated",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "max_runs_escalated"
                )
            return None

        # Below threshold — persist the updated count and close normally.
        checkpoint["max_runs_exhausted_count"] = exhausted_count
        registry.update_checkpoint(sub_id, checkpoint)
        summary = f"Reached the {info.max_runs}-run limit. Last: {reply}"
        closed = registry.mark_closed(
            sub_id, summary=summary, reason="max_runs", closed_by="system"
        )
        if closed is not None:
            await env.delivery.deliver_summary(closed, summary, "max_runs")
        return None

    # Human-approval timeout: when the checkpoint's last_known_state is
    # human_issue_approval and the subsession has produced enough
    # consecutive NO_CHANGE runs, auto-escalate by closing with a
    # distinct reason so the parent agent can act on it.
    #
    # Pre-authorized fast-path: when the monitored ticket matches a
    # pre_authorized_ticket_patterns entry, escalate immediately on the
    # first NO_CHANGE run instead of waiting for the full timeout.
    checkpoint = info.checkpoint or {}
    last_known = checkpoint.get("last_known_state", "")
    if isinstance(last_known, str) and last_known.lower() == "human_issue_approval":
        patterns = env.settings.subsessions.pre_authorized_ticket_patterns
        ticket_id_raw = checkpoint.get("ticket_id")
        ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
        pre_authorized = _is_ticket_pre_authorized(ticket_id, patterns)

        if pre_authorized and consecutive_no_change >= 1:
            logger.info(
                "Subsession %s: pre-authorized ticket %s in "
                "human_issue_approval — auto-escalating immediately.",
                sub_id,
                ticket_id,
            )
            elapsed = _format_duration(registry.now() - info.created_at)
            summary = (
                f"Pre-authorized ticket {ticket_id} entered "
                f"human_issue_approval — auto-escalating immediately "
                f"under standing operator directive "
                f"({elapsed} elapsed)."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="pre_authorized_approval",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "pre_authorized_approval"
                )
            return None

        # Track how long the checkpoint has carried human_issue_approval
        # so we can auto-escalate on wall-clock time even when the agent
        # never emits NO_CHANGE (e.g. it follows the system prompt and
        # calls complete_subsession instead, but the call fails).
        now = registry.now()
        human_approval_since_raw = checkpoint.get("human_approval_since")
        if isinstance(human_approval_since_raw, (int, float)):
            human_approval_since = float(human_approval_since_raw)
        else:
            human_approval_since = now
            checkpoint["human_approval_since"] = now
            registry.update_checkpoint(sub_id, checkpoint)

        human_approval_timeout_s = (
            env.settings.subsessions.human_approval_timeout_seconds
        )
        if now - human_approval_since >= human_approval_timeout_s:
            logger.warning(
                "Subsession %s: auto-escalating after %.0f s in "
                "human_issue_approval state (%.0f s total elapsed).",
                sub_id,
                now - human_approval_since,
                now - info.created_at,
            )
            elapsed = _format_duration(now - info.created_at)
            stuck_for = _format_duration(now - human_approval_since)
            summary = (
                f"Ticket has been stuck at human_issue_approval for "
                f"{stuck_for} ({elapsed} total elapsed) — "
                f"auto-escalating (wall-clock timeout)."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="human_approval_timeout",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "human_approval_timeout"
                )
            return None

        human_approval_cap = env.settings.subsessions.human_approval_timeout_runs
        if consecutive_no_change >= human_approval_cap:
            logger.warning(
                "Subsession %s: auto-escalating after %d consecutive "
                "no-change runs in human_issue_approval state.",
                sub_id,
                consecutive_no_change,
            )
            elapsed = _format_duration(registry.now() - info.created_at)
            summary = (
                f"Ticket has been stuck at human_issue_approval for "
                f"{human_approval_cap} consecutive no-change runs "
                f"({elapsed} elapsed) — auto-escalating."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="human_approval_timeout",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "human_approval_timeout"
                )
            return None

    idle_cap = env.settings.subsessions.max_idle_runs
    if idle_cap > 0 and consecutive_no_change >= idle_cap:
        logger.info(
            "Subsession %s: auto-pausing after %d consecutive no-change runs.",
            sub_id,
            consecutive_no_change,
        )
        summary = (
            f"Auto-paused after {idle_cap} consecutive no-change runs. "
            f"The monitor will resume when the ticket's state changes, "
            f"or you can resume it now by sending a message to this "
            f"subsession via message_subsession."
        )
        paused = registry.mark_paused(
            sub_id,
            summary=summary,
            reason="paused",
        )
        if paused is not None:
            await env.delivery.deliver_summary(paused, summary, "paused")
            if env.event_sink is not None:
                ticket_id_raw = checkpoint.get("ticket_id")
                ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
                last_known = checkpoint.get("last_known_state", "")
                env.event_sink.publish(
                    info.owner_session_id,
                    {
                        "type": SSE_NOTIFICATION_TYPE,
                        "title": f"Monitor auto-paused: {info.title}",
                        "body": (
                            f"Tracked ticket {ticket_id} ({last_known}) — {summary}"
                        ),
                        "urgency": "low",
                        "link": ticket_id,
                    },
                )
            # -- paused wait loop: block on inbox, wake on resume signal --
            return await _paused_wait_loop(env, info, sub_id, previous_result)
        return None

    no_change_cap = env.settings.subsessions.auto_stop_no_change_runs
    no_change_cap_override = checkpoint.get("auto_stop_no_change_runs")
    if (
        not isinstance(no_change_cap_override, bool)
        and isinstance(no_change_cap_override, int)
        and no_change_cap_override >= 1
    ):
        no_change_cap = no_change_cap_override
    if consecutive_no_change >= no_change_cap:
        logger.warning(
            "Subsession %s: auto-stopping after %d consecutive no-change runs. "
            "The monitor will no longer watch for changes — restart it if "
            "continued monitoring is needed.",
            sub_id,
            consecutive_no_change,
        )
        elapsed = _format_duration(registry.now() - info.created_at)
        state_context = ""
        last_known_state = checkpoint.get("last_known_state", "")
        if isinstance(last_known_state, str) and last_known_state:
            state_context = (
                f" Still '{last_known_state}' after {elapsed} — "
                f"if this is not expected, consider checking step-level "
                f"logs or the ticket timeline."
            )
        else:
            state_context = (
                f" No changes detected over {elapsed}. "
                f"Restart the monitor if continued watching is needed."
            )
        summary = (
            f"Auto-stopped after {no_change_cap} consecutive no-change runs."
            f"{state_context}"
        )
        closed = registry.mark_closed(
            sub_id,
            summary=summary,
            reason="no_change_auto_stop",
            closed_by="system",
        )
        if closed is not None:
            await env.delivery.deliver_summary(closed, summary, "no_change_auto_stop")
        if env.event_sink is not None:
            ticket_id_raw = checkpoint.get("ticket_id")
            ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
            last_known = checkpoint.get("last_known_state", "")
            env.event_sink.publish(
                info.owner_session_id,
                {
                    "type": SSE_NOTIFICATION_TYPE,
                    "title": f"Monitor auto-stopped: {info.title}",
                    "body": (f"Tracked ticket {ticket_id} ({last_known}) — {summary}"),
                    "urgency": "low",
                    "link": ticket_id,
                },
            )
        return None

    # Sleep until the next tick, waking early on a steering message.
    woke = await registry.wait_for_inbox(sub_id, timeout=info.interval_seconds)
    pending = registry.drain_inbox(sub_id) if woke else []
    return pending, previous_result, consecutive_no_change

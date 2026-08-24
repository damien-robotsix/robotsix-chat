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
    auto_drive_promote_ready_drafts: bool = False,
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
    pre_authorized = bool(
        pre_authorized_patterns
        and ticket_id
        and _is_ticket_pre_authorized(ticket_id, pre_authorized_patterns)
    )
    if pre_authorized:
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
    # Promotable-draft branch: the auto-drive monitor must not loop
    # silently on a draft ticket that already has a complete,
    # refine-passed spec and no blocking review.  Two outcomes, decided
    # by the opt-in gate + pre-authorization:
    #   - gate ON + pre-authorized  -> auto-promote into the ready queue
    #   - otherwise                 -> post exactly one operator-decision
    #                                  comment, then wait event-driven
    promotable_draft_definition = (
        "A PROMOTABLE DRAFT is a ticket where ALL of the following "
        "hold: (1) its state is 'draft'; (2) it carries a refine-passed "
        "spec — spec_markdown is present and contains the sections "
        "'## Problem', '## Scope', '## Acceptance criteria', and "
        "'## Out of scope / constraints' (or the ticket is otherwise "
        "marked refine-complete by the mill's status metadata); "
        "(3) it has no open blocking review thread.  Drafts with an "
        "incomplete spec, a failed refine, or an open blocking review "
        "are NOT promotable — never promote them, never comment on "
        "them, and follow the normal rules above instead."
    )
    if auto_drive_promote_ready_drafts and pre_authorized:
        parts.append(
            "DRAFT TICKETS — AUTO-PROMOTE BRANCH (the "
            "auto_drive_promote_ready_drafts gate is ON and this ticket "
            "matches a pre_authorized_ticket_patterns entry):\n"
            + promotable_draft_definition
            + "\n"
            f"When the monitored ticket ({ticket_id}) is a promotable "
            "draft, call mark_ticket_ready(ticket_id, justification="
            "'auto-drive: refine-passed spec, no blocking review, "
            "pre-authorized promotion') to transition it out of draft "
            "into the ready queue.  This ticket is pre-authorized under "
            "a standing operator directive, so no per-ticket approval "
            "is required.  Do NOT post an operator-decision comment in "
            "this branch.  After the transition succeeds, reply "
            f"{_QUEUED_SENTINEL} (and nothing else) so the monitor "
            "switches to event-driven waiting while the implement stage "
            "picks the ticket up — do not burn the run budget "
            "re-driving a ticket that has left draft.  If "
            "mark_ticket_ready fails with a permanent error (4xx), do "
            "NOT retry it run after run — fall back to the "
            "operator-decision comment below.\n\n"
        )
    else:
        parts.append(
            "DRAFT TICKETS — OPERATOR-DECISION BRANCH (the "
            "auto_drive_promote_ready_drafts gate is OFF or this ticket "
            "does not match a pre_authorized_ticket_patterns entry):\n"
            + promotable_draft_definition
            + "\n"
            "When the monitored ticket is a promotable draft:\n"
            "  - If the checkpoint already carries "
            "'auto_drive_comment_posted' set to true, do NOT repost "
            f"any comment.  Reply {_QUEUED_SENTINEL} (and nothing else) "
            "and let the system wait event-driven for the operator's "
            "decision.\n"
            "  - Otherwise, post EXACTLY ONE operator-decision comment "
            "on the ticket: call component_request('mill', 'POST', "
            f"'/tickets/{ticket_id}/comments', json_body={{'body': "
            "'[AUTO_DRIVE] This draft has a complete, refine-passed "
            "spec and no open blocking review.  Awaiting an operator "
            "decision: promote it to ready (mark_ticket_ready) or "
            "close it with a reason.'}}).  Then call set_checkpoint "
            "with the existing checkpoint fields (ticket_id, "
            "last_known_state, human_approval_since if present) PLUS "
            "'auto_drive_comment_posted': true — the checkpoint is "
            "replaced wholesale, so re-include every existing field.  "
            "Then reply "
            f"{_QUEUED_SENTINEL} (and nothing else) so the monitor "
            "stops consuming its run budget while the ticket waits for "
            "the operator.  Never post a second comment — one comment "
            "per ticket is the hard limit, enforced by the checkpoint "
            "flag.\n"
            "If the comment POST fails (component_request error, "
            "non-2xx), do NOT fabricate success — leave the checkpoint "
            "flag unset so the next run retries, and reply "
            f"{_NO_CHANGE_SENTINEL}.\n\n"
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
        "have confirmed it via the PR API (GitHub) — a terminal ticket state "
        "alone does not prove a PR exists.  The board API's ``pr_url`` field "
        "can be null or stale even when a PR was merged; never treat a null "
        "``pr_url`` as proof that no PR exists.  Always cross-verify PR "
        "status directly from the GitHub API (e.g. search for PRs "
        "referencing the ticket ID, or use the direct_repo tools to list "
        "open PRs and check their merge status).  "
        "Your complete_subsession summary MUST "
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
        "PR, CI workflow, and live config) are complete.\n\n"
        "REDUNDANT FIX TICKET DETECTION — when you are monitoring a fix "
        "ticket (a ticket created to resolve a specific bug, failure, or "
        "issue), check whether the underlying issue has already been "
        "resolved through an alternative path before continuing to poll.  "
        "Signs that a fix ticket is redundant include:\n"
        "  - The baseline ticket (the original issue report) was directly "
        "fixed, closed, or merged — making the dedicated fix ticket "
        "unnecessary.\n"
        "  - Another ticket or PR addressing the same root cause was "
        "merged or deployed.\n"
        "  - The monitored ticket's block reason or dependency was "
        "resolved externally (e.g. an upstream fix landed, an "
        "infrastructure issue was remediated by another team).\n"
        "When you detect that a fix ticket is redundant, do NOT continue "
        "polling — call complete_subsession with a summary that:\n"
        "  1. States the ticket is redundant and explains WHY (name the "
        "alternative resolution path — e.g. 'baseline ticket X was "
        "directly fixed').\n"
        "  2. Recommends the operator close the redundant ticket with a "
        "brief rationale.\n"
        "  3. Includes the CI workflow verification phrases required by "
        "the loop guard (above) if a deploy/publish workflow was "
        "involved in the alternative fix.\n"
        "This prevents the monitor from burning its run budget on a "
        "ticket whose purpose is already fulfilled."
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
    if not _is_no_change(reply):
        # Progress observed (a non-NO_CHANGE reply) — clear the no-change
        # pause counter so a later idle stretch starts a fresh budget.
        cp = info.checkpoint or {}
        if cp.get("no_change_pause_count"):
            cp["no_change_pause_count"] = 0
            registry.update_checkpoint(sub_id, cp)
    runs = info.runs + 1
    # -- adaptive run budget: record whether this run observed progress
    #    (a non-suppressed reply, i.e. the agent acknowledged a state
    #    transition or reported activity rather than emitting NO_CHANGE).
    #    The max_runs gate below uses this rolling window to extend the
    #    budget while the ticket is actively moving through non-terminal
    #    states instead of cutting the monitor off at a hard cap.
    progress_window = env.settings.subsessions.max_runs_progress_window
    if info.max_runs is not None and progress_window > 0:
        checkpoint = info.checkpoint
        if checkpoint is None:
            checkpoint = {}
            info.checkpoint = checkpoint
        flags_raw = checkpoint.get("recent_progress_flags")
        flags = (
            [bool(flag) for flag in flags_raw] if isinstance(flags_raw, list) else []
        )
        flags.append(not suppressed)
        if len(flags) > progress_window:
            flags = flags[-progress_window:]
        checkpoint["recent_progress_flags"] = flags
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
        # -- adaptive run-budget extension: when the monitored ticket has
        #    made progress recently (a non-suppressed reply within the
        #    configured window), extend the run budget instead of closing
        #    hard at max_runs.  This keeps a monitor alive through
        #    long-running, actively-progressing pipeline stages (e.g. a
        #    PR sitting in code_review) rather than cutting tracking short.
        checkpoint = info.checkpoint or {}
        extension = env.settings.subsessions.max_runs_progress_extension
        if extension > 0:
            flags_raw = checkpoint.get("recent_progress_flags")
            flags = (
                [bool(flag) for flag in flags_raw]
                if isinstance(flags_raw, list)
                else []
            )
            if any(flags):
                ceiling = env.settings.subsessions.periodic_max_total_runs
                new_cap = min(info.max_runs + extension, ceiling)
                if new_cap > info.max_runs:
                    logger.info(
                        "Subsession %s: extending max_runs from %d to %d "
                        "after observing progress within the last %d runs.",
                        sub_id,
                        info.max_runs,
                        new_cap,
                        env.settings.subsessions.max_runs_progress_window,
                    )
                    info.max_runs = new_cap
                    checkpoint["recent_progress_flags"] = []
                    registry.update_checkpoint(sub_id, checkpoint)

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

    # -- promotable-draft wait: when the operator-decision comment for
    #    an unchanged draft is already posted (checkpoint flag set and
    #    last_known_state still 'draft'), route into the queued wait
    #    loop.  This saves the run budget: the ticket is waiting on the
    #    operator, not progressing, and no further agent turns are
    #    useful until the ticket leaves draft.
    checkpoint = info.checkpoint or {}
    last_known = checkpoint.get("last_known_state", "")
    if (
        isinstance(last_known, str)
        and last_known.lower() == "draft"
        and bool(checkpoint.get("auto_drive_comment_posted"))
    ):
        ticket_id_raw = checkpoint.get("ticket_id")
        ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
        logger.info(
            "Subsession %s: ticket %s still draft with the "
            "operator-decision comment posted — entering event-driven "
            "wait.",
            sub_id,
            ticket_id,
        )
        registry.set_status(
            sub_id,
            SubsessionStatus.SLEEPING,
            runs=runs,
            next_run_at=registry.now() + (info.interval_seconds or 60.0),
            last_result=reply,
        )
        result = await _queued_wait_loop(
            env,
            info,
            sub_id,
            previous_result,
            consecutive_no_change,
        )
        if result is None:
            return None
        pending, previous_result, _ = result
        return pending, previous_result, 0

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
        # -- no-change pause escalation -----------------------------------
        # A monitor that auto-pauses, gets auto-resumed by the paused-wait
        # loop's timeout, and auto-pauses again without the ticket ever
        # changing state would otherwise repeat the same pause message
        # forever.  Track how many consecutive no-change pauses this
        # monitor has entered and close it with a reassessment
        # recommendation once the limit is reached.
        pause_cap = env.settings.subsessions.max_no_change_pauses
        pause_count_raw = checkpoint.get("no_change_pause_count")
        pause_count = (
            pause_count_raw
            if (
                isinstance(pause_count_raw, int)
                and not isinstance(pause_count_raw, bool)
            )
            else 0
        )
        pause_count += 1

        if pause_cap > 0 and pause_count >= pause_cap:
            logger.warning(
                "Subsession %s: auto-paused %d consecutive times without "
                "ticket progress (limit=%d) — closing with a reassessment "
                "recommendation.",
                sub_id,
                pause_count,
                pause_cap,
            )
            elapsed = _format_duration(registry.now() - info.created_at)
            ticket_id_raw = checkpoint.get("ticket_id")
            ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
            last_known = checkpoint.get("last_known_state", "")
            summary = (
                f"Monitor auto-closed after {pause_count} consecutive "
                f"pauses with no change to the tracked ticket."
            )
            if isinstance(last_known, str) and last_known:
                summary += f" The ticket has remained '{last_known}' for {elapsed}."
            else:
                summary += f" No changes were detected over {elapsed}."
            summary += (
                " Recommend reassessing whether this ticket still needs "
                "active monitoring, or respawning the monitor with a "
                "longer polling interval."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="no_change_pause_limit",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "no_change_pause_limit"
                )
            if env.event_sink is not None:
                env.event_sink.publish(
                    info.owner_session_id,
                    {
                        "type": SSE_NOTIFICATION_TYPE,
                        "title": f"Monitor auto-closed: {info.title}",
                        "body": (
                            f"Tracked ticket {ticket_id} ({last_known}) — {summary}"
                        ),
                        "urgency": "low",
                        "link": ticket_id,
                    },
                )
            return None

        checkpoint["no_change_pause_count"] = pause_count
        registry.update_checkpoint(sub_id, checkpoint)

        if pause_cap > 0:
            logger.info(
                "Subsession %s: auto-pausing after %d consecutive no-change runs "
                "(pause %d/%d).",
                sub_id,
                consecutive_no_change,
                pause_count,
                pause_cap,
            )
        else:
            logger.info(
                "Subsession %s: auto-pausing after %d consecutive no-change runs.",
                sub_id,
                consecutive_no_change,
            )
        if pause_cap > 0:
            summary = (
                f"Auto-paused after {idle_cap} consecutive no-change runs "
                f"(no-change pause {pause_count}/{pause_cap}; the monitor "
                f"will auto-close after {pause_cap} such pauses if the "
                f"ticket never changes). The monitor will resume when the "
                f"ticket's state changes, or you can resume it now by "
                f"sending a message to this subsession via message_subsession."
            )
        else:
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

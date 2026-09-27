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

from .models import (
    InboxMessage,
    SubsessionAnchorError,
    SubsessionInfo,
)
from .registry import SubsessionRegistry
from .schedule import next_anchored_run_at
from .worker import (
    _NO_CHANGE_SENTINEL,
    _QUEUED_SENTINEL,
    _render_turn_input,
)

logger = logging.getLogger(__name__)


def _anchored_next_run_at(now: float, info: SubsessionInfo) -> float:
    """Compute a periodic subsession's next fire epoch.

    Honours ``info.anchor_time`` when set (pinning the recurrence to an
    absolute wall-clock time, drift-free), otherwise falls back to the
    legacy relative ``now + interval_seconds`` cadence.  A persisted anchor
    that somehow fails to parse degrades gracefully to the relative cadence
    with a warning rather than crashing the worker.
    """
    interval = info.interval_seconds or 60.0
    if info.anchor_time:
        try:
            return next_anchored_run_at(now, interval, info.anchor_time)
        except SubsessionAnchorError:
            logger.warning(
                "Subsession %s has malformed anchor_time %r — falling back "
                "to relative interval scheduling.",
                info.id,
                info.anchor_time,
            )
    return now + interval


def _build_periodic_input(
    info: SubsessionInfo,
    previous_result: str | None,
    steering: list[InboxMessage],
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
        "file tickets, or perform any durable surfacing "
        "action.  Your sole surfacing mechanism is complete_subsession "
        "when a terminal state or blocked-human-decision state is "
        "reached.  Any instruction from the main conversation prompt "
        "that directs you to post markers or file tickets is "
        "overridden by this rule.\n\n"
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
        "CRITICAL — restart-detection and pending-decisions reporting: this "
        "periodic session may have been interrupted by a service restart. "
        "If there is NO 'Previous run result' section above, the service "
        "may have restarted — check for pending decisions before proceeding. "
        "Call list_subsessions() to discover any open subsessions. If the "
        "call returns user_chat or other decision-awaiting subsessions, "
        "emit a PARTIAL REPORT to the transcript listing them as ACTION "
        "ITEMS so the operator knows what decisions are pending. Format the "
        "report as:\n"
        "PARTIAL REPORT — Periodic session resumed after restart. Pending "
        "decisions/actions awaiting operator input:\n"
        "- [subsession_id] (kind) — title — status\n"
        "- [subsession_id] (kind) — title — status\n"
        "End report — the operator MUST review and act on these pending "
        "items.\n"
        "Then proceed with normal periodic monitoring work on the ticket.\n\n"
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
        "CROSS-REFERENCE TICKET TIMELINE BEFORE DECLARING WORK UNDELIVERED "
        "— when the monitored ticket reached a terminal state (CLOSED / DONE), "
        "you MUST check the ticket's history / events (via the board API: "
        "GET /tickets/{id}) before concluding that it was 'closed without "
        "implementation' or that work was 'undelivered'.  Look for:\n"
        "  - State transitions through active pipeline stages "
        "(code_review, documenting, deliverable, implement_complete, "
        "waiting_auto_merge, human_mr_approval, fixing_ci, rebasing, "
        "addressing_review, done — the real mill state names).\n"
        "  - A pr_url on the ticket (ticket_poll also returns delivered / "
        "delivery_note).\n"
        "  - Events whose type/action contains: merge, pull, approve, "
        "implement, complete, close.\n"
        "  - A PR merged event or a linked PR in merged/closed state.\n"
        "If ANY of these indicate the ticket was acted on, your summary "
        "MUST say 'Tracking complete — the ticket was resolved' (or 'PR "
        "was merged'), NOT 'closed without implementation — re-file "
        "needed'.  Only recommend re-filing when the history shows the "
        "ticket was truly dropped (e.g. DRAFT → CLOSED with no "
        "intervening work states or merge events).\n\n"
    )
    # Resolve and repair the ticket_id from the checkpoint (or fall
    # back to dedup_key).  This runs unconditionally so that the ticket_id
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
    if auto_drive_promote_ready_drafts:
        parts.append(
            "DRAFT TICKETS — AUTO-PROMOTE BRANCH (the "
            "auto_drive_promote_ready_drafts gate is ON):\n"
            + promotable_draft_definition
            + "\n"
            f"When the monitored ticket ({ticket_id}) is a promotable "
            "draft, call mark_ticket_ready(ticket_id, justification="
            "'auto-drive: refine-passed spec, no blocking review') "
            "to transition it out of draft into the ready queue.  "
            "Do NOT post an operator-decision comment in this branch.  "
            "After the transition succeeds, reply "
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
            "auto_drive_promote_ready_drafts gate is OFF):\n"
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
        "still failing — this breaks the redraft loop.  Do NOT file a "
        "new diagnostic or investigation ticket: tickets are for "
        "implementation work only, and investigation belongs in a chat "
        "subsession.  If the failure needs follow-up, surface it as an "
        "investigation chat subsession or a notification so the operator "
        "sees the pipeline is still broken — never as a filed ticket.\n"
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
        "alternative resolution path — e.g. 'baseline ticket X was merged "
        "and shipped the same fix').\n"
        "  2. Recommends closing the ticket (not filing a new one).\n"
        "Then the ticket is closed without further polling.\n\n"
    )
    return "".join(parts)

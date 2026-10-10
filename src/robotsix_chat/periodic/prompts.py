"""Shared preamble for periodic session initial prompts.

The preamble is prepended by the scheduler to every preset's
``initial_prompt`` — presets state only their task. It rides in the USER
message of the session's first turn, not in the system prompt: periodic
sessions run the exact same agent, instruction, and code path as an operator
session.
"""

from __future__ import annotations

from datetime import UTC, datetime

from robotsix_chat.subsessions.prompts import USER_CHAT_FIRST_TURN_NOTE

CURRENT_DATETIME_HEADER = (
    "=== CURRENT DATE/TIME (system context — extract 'today' from here) ==="
)
CURRENT_DATETIME_FOOTER = "=== END CURRENT DATE/TIME ==="

PERIODIC_PREAMBLE = (
    "You are running a scheduled periodic session. Complete the task below "
    "in this turn: do the work now, then finish with a report of your "
    "findings and any actions you took. Do not schedule continuations, "
    "do not wait for future events, and do not leave the task half-done for "
    "a later run — the next scheduled run starts from a fresh session with "
    "no memory of this one beyond what your report says.\n\n"
    "IMPORTANT: When your task requires generating a structured output "
    "(numbered action-list, decision table, enumerated items, or similar), "
    "**complete that enumeration in full** — do not truncate items for "
    "brevity. Completeness of structured outputs is mandatory. Brevity "
    "applies to prose explanation and analysis, not to the enumerated "
    "deliverables themselves.\n\n"
    "════════════════════════════════════════════════════════════\n"
    "CRITICAL — FIRST OPERATION (MANDATORY, BEFORE ANY TASK WORK)\n"
    "════════════════════════════════════════════════════════════\n"
    "The VERY FIRST action of this session — before enumerating "
    "tickets, processing queues, running gate checks, or ANY other task "
    "work — is to call list_subsessions and check for any "
    "OPEN user_chat subsessions still awaiting an operator reply. "
    "Perform this check even right after a chat-service restart: the "
    "operator's desktop alert may not persist across restarts, so the "
    "panel may be the only surviving signal.\n"
    "Surface every such open panel IMMEDIATELY as a first-class ACTION "
    "ITEM in your first output — name the subsession/panel and its full "
    "ID, the decision it needs, the relevant status details, and that "
    "it is awaiting an operator reply. Do not bury these under 'Held "
    "for next run' — they are live decisions needing operator input, "
    "not deferred work. If none are open, state that explicitly so the "
    "operator knows there is no pending decision.\n"
    "════════════════════════════════════════════════════════════\n\n"
    "ALWAYS end the session with a report — this is non-negotiable. The "
    "report is the ONLY thing the next scheduled run inherits; a session "
    "that ends without one leaves the next run blind to what was attempted, "
    "done, escalated, or blocked. Treat the report as the first thing you "
    "owe, not the last.\n\n"
    "You CANNOT introspect your own token consumption or context-window "
    "usage — there is no budget field or tool that tells you how close you "
    "are to the limit, and the limit can be reached without warning "
    "mid-operation. Do NOT rely on 'sensing' exhaustion; instead, make the "
    "report resilient to sudden termination by DISTRIBUTING report output "
    "throughout the session, not concentrating it at the end. This "
    "distribution IS the truncation-resilience mechanism: the last complete "
    "report the transcript contains is what survives, so the more evenly you "
    "spread report sections across the turn, the less any single unexpected "
    "cut-off can destroy. Reporting AS YOU GO is therefore mandatory:\n"
    "  - MANDATORY, NOT OPTIONAL: pause work and emit an inline 'PARTIAL "
    "REPORT' section after every 2-3 major operations. Do NOT accumulate a "
    "long run of work and report only once at the end — interleave report "
    "sections BETWEEN batches of operations. A 'major operation' is one "
    "atomic unit of work at a natural breakpoint: each PR merged, each ticket "
    "processed or drained, each subsession opened, each investigation "
    "closed.\n"
    "  - Concrete examples of when to stop and output a PARTIAL REPORT: after "
    "merging 2-3 PRs; after draining 2-3 tickets; after opening an escalation "
    "subsession; after closing an investigation; after an enumeration attempt "
    "fails (board-list API timeout, missing component registry, gate-query "
    "error, or any query that returns nothing / error / no response); and "
    "immediately before starting any expensive operation that could be cut off "
    "before you report it. When unsure whether to report now or do one more "
    "operation, REPORT NOW — the report you emitted always outranks the operation "
    "you skipped. **CRITICAL:** When enumeration fails, emit a PARTIAL REPORT "
    "checkpoint BEFORE proceeding to troubleshoot the blocker. Document what "
    "enumeration was attempted (e.g., 'enumerate board tickets in blocked state'), "
    "what failed (e.g., 'API timed out after 30 seconds'), and any error details. "
    "Then proceed to root-cause troubleshooting if needed. This ensures enumeration "
    "failure evidence survives even if troubleshooting is interrupted.\n"
    "  - Each PARTIAL REPORT must be COMPLETE and self-contained: "
    "overwrite/restate the full report each time (everything done so far) "
    "rather than emitting deltas — the last complete report the transcript "
    "contains is what survives.\n"
    "  - Distribution beats size. A smaller batch that is fully reported "
    "beats a larger batch that terminates unreported. Reserve headroom for "
    "the report; do not pack the turn end-to-end with operational work. Once "
    "you have completed a batch of 2-3 major operations, prefer stopping and "
    "emitting a PARTIAL REPORT over starting another expensive operation.\n"
    "  - ERROR HANDLING — CRITICAL: wrap all task work in a try-finally structure "
    "(either explicit in your planning, or implicit in your control flow) to GUARANTEE "
    "PARTIAL REPORT output even if an error occurs. If you encounter ANY error "
    "mid-execution — tool failure, API error, or unhandled exception — stop the "
    "remaining work immediately, catch the error, document it in your report, "
    "and emit the PARTIAL REPORT right away while you still can. Do NOT let an "
    "error silence the session — every session MUST end with a report.\n\n"
    "ESCALATION OPENED — IMMEDIATE NOTIFICATION (MANDATORY): When you open "
    "a user_chat escalation subsession, you MUST emit a plain-text "
    "ESCALATION OPENED line in the main conversation immediately, so the "
    "escalation survives session truncation (restart, token exhaustion, or "
    "timeout) before the final PARTIAL REPORT. The line format is:\n"
    "  ESCALATION OPENED — <subsession_id>: <title> (<detail>)\n"
    "Worked example (order matters):\n"
    "  1. FIRST call spawn_subsession(...) to open the escalation subsession "
    "and capture the subsession id it returns — you cannot know the id "
    "before this call.\n"
    "  2. IMMEDIATELY emit, in the main conversation, the line "
    "'ESCALATION OPENED — <returned id>: <title> (<detail>)' using the id "
    "RETURNED by spawn_subsession, before any further work.\n"
    "This ESCALATION OPENED line is independent of the subsession panel "
    "itself: the panel may be lost across a restart, but the line in the "
    "main conversation survives truncation and tells the next run an "
    "escalation was opened.\n\n"
    "PARTIAL REPORT is the EXPECTED outcome for any interrupted or "
    "incomplete periodic session, not an emergency fallback. Whenever you "
    "have not finished everything — and at every breakpoint above — output a "
    "report titled 'PARTIAL REPORT'. To make the report resilient to "
    "truncation at any point, use the skeleton-first strategy:\n"
    "\n"
    "  CRITICAL — skeleton-first reporting strategy:\n"
    "  1. BEFORE DOING ANY WORK, enumerate the items you will process and emit\n"
    "     a minimal PARTIAL REPORT skeleton containing only counts and full\n"
    "     ticket IDs — one ID per line, no prose, no descriptions. This is\n"
    "     your checkpoint: if token exhaustion strikes immediately, at least\n"
    "     the next run knows what items exist.\n"
    "     Example skeleton:\n"
    "     PARTIAL REPORT\n"
    "     Done: 0 items\n"
    "     Escalations: 5 decisions\n"
    "       20260918T134429Z-add-http-basic-a-4b89\n"
    "       20260918T140815Z-update-docs-pa-5c2d\n"
    "       20260918T142103Z-fix-auth-flow-b-8e1f\n"
    "       20260918T144530Z-review-security-c-3a2b\n"
    "       20260918T150247Z-verify-deployment-d-6f7e\n"
    "     Held for next run: 0 items\n"
    "     Errors encountered: 0\n"
    "  2. After EACH BATCH of work (e.g. after processing 5 tickets, or after\n"
    "     opening subsessions), RE-EMIT the full skeleton with updated counts.\n"
    "     Update the Done/Escalations/Held/Errors sections with their new counts.\n"
    "     This re-emitted checkpoint is what survives if you hit token exhaustion\n"
    "     mid-batch.\n"
    "  3. ONLY AFTER the skeleton is safely in the transcript, optionally add\n"
    "     prose descriptions for completed items (one line per item):\n"
    "     'ticket 20260918T134429Z-add-http-basic-a-4b89: escalated to user for\n"
    "     approval decision'. Prose is helpful for the operator's understanding\n"
    "     but is NOT critical — it is allowed to be truncated. The skeleton\n"
    "     counts and IDs are what matter.\n"
    "  4. If you run short on tokens MID-PROSE or mid-report, STOP immediately\n"
    "     after the skeleton. The next run can resume from the skeleton's counts\n"
    "     and IDs. A truncated skeleton is valid and actionable; a truncated ID\n"
    "     list is not.\n"
    "\n"
    "  Then list, in this order:\n"
    "  - Done: items completed and the ROUTINE actions taken (e.g. tickets "
    "drained, PRs merged), with full ticket IDs.\n"
    "  - Escalations: subsession IDs opened and the decision each needs "
    "from the operator, with full ticket IDs.\n"
    "  - Held for next run: items not reached or deliberately deferred, each "
    "with a one-line reason and full ticket IDs.\n"
    "  - Errors encountered (if any): brief description of what failed and why, "
    "so the next run understands what to investigate or retry.\n"
    "  FULL TICKET IDS — every ticket you name in the Done, Escalations, "
    "Held, or Errors sections (or any awaiting-operator panel / escalation subsession) "
    "MUST carry its full mill ticket ID exactly as returned by the board API "
    "(e.g. 20260922T124232Z-...-4b89), never a truncated suffix. The next run "
    "relies on these IDs to match the report to live tickets and to apply "
    "escalation/dedup rules; a truncated-only reference forces it to re-derive "
    "the ID from scratch.\n"
    "\n"
    "A PARTIAL REPORT with these four sections is always better than "
    "silence — an interrupted session that reported what it got to gives "
    "the next run the context to resume; one that reported nothing does not. "
    "An error-aware report (including the Errors section) tells the next run "
    "what went wrong so it can avoid the same failure or investigate further.\n\n"
    "The operator may reply in this session after your report. Such a reply "
    "is a live instruction that supersedes the task's constraints below "
    "(including any read-only framing): resolve it against this session's "
    "own turns first, and act on it as you would in a normal chat.\n\n"
    "---\n\n"
)


def _current_datetime_context(now: datetime) -> str:
    """Return the fenced system-context block stating the current instant.

    Periodic sessions run headless with no operator to supply "today", so the
    scheduler injects the firing instant here. The agent must derive today's
    date and any date ranges from this block rather than from memory or
    training-cutoff assumptions — the failure that motivated this
    (2026-09-07 calendar-agenda returned events for 2026-09-05).
    """
    now = now.astimezone(UTC)
    return (
        f"{CURRENT_DATETIME_HEADER}\n"
        f"The current date and time is {now:%Y-%m-%d %H:%M} UTC ({now:%A}).\n"
        "Treat this instant as 'now'/'today' for any date-relative task below. "
        "Derive today's date and any date ranges (e.g. start-of-day "
        "00:00:00 to end-of-day 23:59:59 UTC) from this line — never from "
        "memory or assumptions. Before reporting date-scoped results, confirm "
        "the returned items fall within the range you derived from this date.\n"
        f"{CURRENT_DATETIME_FOOTER}\n\n"
    )


def build_initial_message(initial_prompt: str, *, now: datetime | None = None) -> str:
    """Return the first user message for a periodic session.

    ``now`` is the firing instant (defaults to the current UTC time); it is
    rendered into a fenced system-context block so the agent extracts "today"
    from context instead of guessing.
    """
    if now is None:
        now = datetime.now(UTC)
    return PERIODIC_PREAMBLE + _current_datetime_context(now) + initial_prompt.strip()


def strip_periodic_scaffolding(message: str) -> str:
    """Return *message* with the scheduler scaffolding removed.

    Strips the fixed preamble and the injected current-date/time block so
    memory recall queries the preset's actual task, not the scaffolding.
    Messages without the scaffolding are returned unchanged.
    """
    body = message.removeprefix(PERIODIC_PREAMBLE)
    marker = f"{CURRENT_DATETIME_FOOTER}\n\n"
    idx = body.find(marker)
    if idx != -1:
        body = body[idx + len(marker) :]
    return body


def strip_recall_scaffolding(message: str) -> str:
    """Return *message* with every fixed first-turn preamble removed.

    This is the scrubber for the memory recall query: it strips the periodic
    scheduler scaffolding (see :func:`strip_periodic_scaffolding`) and the
    ``user_chat`` side-chat system note the subsession worker prepends to
    its first turn.  Recalling on either preamble retrieves junk — on
    2026-09-07 three consecutive recalls ran on ``'[System note: this is a
    side-chat with the operator...'`` and paid a 10-30 s rerank for nothing.
    Messages without either preamble are returned unchanged; a message that
    merely mentions the note mid-text is left alone.
    """
    note = USER_CHAT_FIRST_TURN_NOTE + "\n\n"
    body = message.removeprefix(note)
    body = strip_periodic_scaffolding(body)
    return body.removeprefix(note)
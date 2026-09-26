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
    "in this turn: do the work now, then finish with a concise report of "
    "your findings and any actions you took. Do not schedule continuations, "
    "do not wait for future events, and do not leave the task half-done for "
    "a later run — the next scheduled run starts from a fresh session with "
    "no memory of this one beyond what your report says.\n\n"
    "═══════════════════════════════════════════════════════════════════════════\n"
    "CRITICAL — FIRST OPERATION (MANDATORY, BEFORE ANY TASK WORK)\n"
    "═══════════════════════════════════════════════════════════════════════════\n"
    "The VERY FIRST thing you must do in this session — before enumerating \n"
    "tickets, processing queues, performing gate checks, or starting ANY other\n"
    "task work — is to call list_subsessions and check for any OPEN \n"
    "user_chat subsessions that are still awaiting an operator reply.\n"
    "\n"
    "Surface every such open panel IMMEDIATELY as an ACTION ITEM in your \n"
    "first output (before task details):\n"
    "  - Subsession/panel name and ID\n"
    "  - The decision it needs from the operator\n"
    "  - Relevant decision/status details so the operator can respond\n"
    "  - That it is awaiting the operator's reply\n"
    "\n"
    "Include this check even if a chat-service restart just occurred — the \n"
    "operator's desktop alert may not persist across restarts, so you must \n"
    "proactively surface awaiting-operator panels. If none are open, state \n"
    "that explicitly so the operator knows there is no pending decision.\n"
    "═══════════════════════════════════════════════════════════════════════════\n\n"
    "ALWAYS end the session with a report — this is non-negotiable. The "
    "report is the ONLY thing the next scheduled run inherits; a session "
    "that ends without one leaves the next run blind to what was attempted, "
    "done, escalated, or blocked. Treat the report as the first thing you "
    "owe, not the last.\n\n"
    "You CANNOT introspect your own token consumption or context-window "
    "usage — there is no budget field or tool that tells you how close you "
    "are to the limit, and the limit can be reached without warning "
    "mid-operation. Do NOT rely on 'sensing' exhaustion; instead, make the "
    "report resilient to sudden termination by reporting AS YOU GO:\n"
    "  - Report at natural operational breakpoints, not just at the end. "
    "After each atomic unit of work (each PR merged, each ticket processed "
    "or drained, each subsession opened, each investigation closed), output "
    "an updated running 'PARTIAL REPORT' capturing everything done so far. "
    "Overwrite/restate the full report each time rather than emitting deltas "
    "— the last complete report the transcript contains is what survives.\n"
    "  - Reserve headroom for the report. Do not pack the turn end-to-end "
    "with operational work; once you have completed a batch of major "
    "operations, prefer stopping and emitting a final PARTIAL REPORT over "
    "starting another expensive operation that could be cut off before you "
    "report it. A smaller batch that is fully reported beats a larger batch "
    "that terminates unreported.\n"
    "  - If you hit an error mid-execution, STOP the remaining work and "
    "output the report IMMEDIATELY while you still can.\n\n"
    "PARTIAL REPORT is the EXPECTED outcome for any interrupted or "
    "incomplete periodic session, not an emergency fallback. Whenever you "
    "have not finished everything — and at every breakpoint above — output a "
    "report titled 'PARTIAL REPORT' that lists, in this order:\n"
    "\n"
    "  CRITICAL — make the report truncation-resilient:\n"
    "  1. Output section HEADERS AND SUMMARY COUNTS first, before detailed items.\n"
    "     Example: 'Done: 5 items (PR merged, ticket closed)' before listing them;\n"
    "     'Escalations: 2' before listing the IDs.\n"
    "  2. Format detailed items as compact, self-contained LINE-BY-LINE entries\n"
    "     (prose lines or key=value pairs), NOT multi-line tables or structures.\n"
    "     Each completed line survives truncation independently.\n"
    "  3. If you run short on tokens MID-REPORT, STOP and output what you have\n"
    "     so far. Every line you complete survives — partial reports are valid.\n"
    "\n"
    "  Then list, in this order:\n"
    "  - Done: items completed and the ROUTINE actions taken (e.g. tickets "
    "drained, PRs merged), each named.\n"
    "  - Escalations: subsession IDs opened and the decision each needs "
    "from the operator.\n"
    "  - Held for next run: items not reached or deliberately deferred, each "
    "with a one-line reason.\n"
    "  FULL TICKET IDS — every ticket you name in the Done, Escalations, or "
    "Held sections (or any awaiting-operator panel / escalation subsession) "
    "MUST carry its full mill ticket ID exactly as returned by the board API "
    "(e.g. 20260922T124232Z-...-4b89), never a truncated suffix. The next run "
    "relies on these IDs to match the report to live tickets and to apply "
    "escalation/dedup rules; a truncated-only reference forces it to re-derive "
    "the ID from scratch.\n"
    "\n"
    "A PARTIAL REPORT with these three sections is always better than "
    "silence — an interrupted session that reported what it got to gives "
    "the next run the context to resume; one that reported nothing does not.\n\n"
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

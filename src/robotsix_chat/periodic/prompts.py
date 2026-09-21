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
    "in this turn: do the work now, emitting progress reports as you go, "
    "so that if context exhaustion or a timeout interrupts you, the next "
    "scheduled run inherits your progress. Do not schedule continuations, "
    "do not wait for future events, and do not leave the task half-done — "
    "the next scheduled run starts from a fresh session with "
    "no memory of this one beyond what your report says.\n\n"
    "CRITICAL — Report as the FIRST deliverable, not the last: The next "
    "scheduled run depends entirely on your report. If you exhaust your "
    "available context or time before completing all work, the session will "
    "terminate WITHOUT WARNING. To guarantee the next run has something to "
    "work from, you MUST emit a report (full or partial) BEFORE running out "
    "of tokens. Strategy:\n"
    "  1. After each major operational batch (e.g., after processing 3-5 "
    "items, or after merging a PR, or after a major decision), check your "
    "progress: have you completed ~50% of the expected scope? If yes, emit "
    "a progress report NOW (not at the end). This is NOT premature — it "
    "ensures the next run knows what you finished.\n"
    "  2. As you approach the end of the task, watch for signs you are "
    "running low on context: your last few turns take longer to generate, "
    "your outputs become shorter or less detailed, or you see fewer tokens "
    "available in your outputs. IMMEDIATELY STOP operational work and emit "
    "a FINAL or PARTIAL REPORT with the three sections below.\n"
    "  3. NEVER reach the hard limit. Your output may be cut off mid-sentence "
    "if the context window fills completely. A report that is cut off is "
    "worse than no report — incomplete data confuses the next run. Stop early "
    "to ensure your report completes cleanly.\n\n"
    "ALWAYS end the session with a report — this is non-negotiable. The "
    "report is the ONLY thing the next scheduled run inherits; a session "
    "that ends without one leaves the next run blind to what was attempted, "
    "done, escalated, or blocked.\n\n"
    "When work is complete (all items processed), output a report titled "
    "'FINAL REPORT' or simply 'Report'. When you cannot finish everything "
    "because context exhaustion interrupts you, output a report titled "
    "'PARTIAL REPORT'. Both use the same format — list the following "
    "sections in this order:\n"
    "  - Done: items completed and the ROUTINE actions taken (e.g. tickets "
    "drained, PRs merged), each named. Include ticket IDs, PR numbers, or "
    "subsession IDs (e.g., 'Merged PR #42 (safe dependency bump)', 'Filed "
    "ticket #2026xyz (migration needed)'). The next run uses this list to "
    "avoid re-processing.\n"
    "  - Escalations: subsession IDs opened and the decision each needs "
    "from the operator. Include the subsession ID and a one-line summary "
    "(e.g., 'Subsession #sub-123: awaiting operator approval for PR #99').\n"
    "  - Held for next run: items not reached or deliberately deferred, each "
    "with a one-line reason. Use this to tell the next run what to resume "
    "with (e.g., 'PR #50-60 (10 PRs) not yet reviewed — review queue full; "
    "defer to next run').\n\n"
    "For both FINAL and PARTIAL reports, keep entries explicit and countable: "
    "the next run will scan your report to understand what was done and what "
    "remains. Vague summaries ('completed several tasks', 'many PRs merged') "
    "waste the next run's time re-discovering what actually happened. "
    "Use specific names and counts.\n\n"
    "A PARTIAL REPORT with these three sections is always better than "
    "silence — an interrupted session that reported what it completed gives "
    "the next run the context to resume from where you left off; one that "
    "reported nothing leaves the next run blind and forces it to re-discover "
    "what was already done. Incomplete context is better than none.\n\n"
    "Examples:\n"
    "  - FINAL REPORT: 'All 15 Dependabot PRs reviewed. Merged 8 (green CI, "
    "non-breaking). Filed 4 migration tickets (breaking changes). Skipped 3 "
    "(already auto-merging).'\n"
    "  - PARTIAL REPORT (interrupted): 'Done: Merged PR #456, filed ticket "
    "#xyz-789. Escalations: subsession #sub-012 awaiting operator approval. "
    "Held for next run: 12 remaining Dependabot PRs (context budget "
    "exhausted).'\n\n"
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

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
    side-chat with the operato...'`` and paid a 10-30 s rerank for nothing.
    Messages without either preamble are returned unchanged; a message that
    merely mentions the note mid-text is left alone.
    """
    note = USER_CHAT_FIRST_TURN_NOTE + "\n\n"
    body = message.removeprefix(note)
    body = strip_periodic_scaffolding(body)
    return body.removeprefix(note)

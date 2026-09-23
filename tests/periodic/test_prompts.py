"""Tests for the shared periodic-session preamble."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from robotsix_chat.periodic.prompts import (
    CURRENT_DATETIME_FOOTER,
    CURRENT_DATETIME_HEADER,
    PERIODIC_PREAMBLE,
    build_initial_message,
    strip_periodic_scaffolding,
    strip_recall_scaffolding,
)
from robotsix_chat.subsessions.prompts import USER_CHAT_FIRST_TURN_NOTE


def test_initial_message_prepends_the_preamble():
    msg = build_initial_message("Review the mail queue.")
    assert msg.startswith(PERIODIC_PREAMBLE)
    assert msg.endswith("Review the mail queue.")


def test_initial_message_injects_the_current_datetime():
    """The firing instant rides in a fenced system-context block.

    Regression: 2026-09-07 the calendar-agenda job reported events for
    2026-09-05 because the agent had no authoritative "today" in context.
    """
    now = datetime(2026, 9, 7, 6, 0, tzinfo=UTC)
    msg = build_initial_message("Produce today's calendar agenda.", now=now)
    assert CURRENT_DATETIME_HEADER in msg
    assert CURRENT_DATETIME_FOOTER in msg
    assert "2026-09-07 06:00 UTC" in msg
    assert "Monday" in msg  # 2026-09-07 is a Monday
    # The date block sits between the preamble and the task.
    assert msg.startswith(PERIODIC_PREAMBLE)
    assert msg.endswith("Produce today's calendar agenda.")
    assert msg.index(CURRENT_DATETIME_HEADER) < msg.index(
        "Produce today's calendar agenda."
    )


def test_initial_message_normalises_now_to_utc():
    """A non-UTC firing instant is rendered in UTC."""
    now = datetime(2026, 9, 7, 2, 0, tzinfo=timezone(-timedelta(hours=4)))
    msg = build_initial_message("Task.", now=now)
    assert "2026-09-07 06:00 UTC" in msg


def test_strip_periodic_scaffolding_removes_preamble_and_date_block():
    now = datetime(2026, 9, 7, 6, 0, tzinfo=UTC)
    msg = build_initial_message("Produce today's calendar agenda.", now=now)
    assert strip_periodic_scaffolding(msg) == "Produce today's calendar agenda."


def test_strip_periodic_scaffolding_without_date_block():
    """A message carrying only the preamble still strips cleanly."""
    msg = PERIODIC_PREAMBLE + "Review today's unread inbox."
    assert strip_periodic_scaffolding(msg) == "Review today's unread inbox."


def test_strip_periodic_scaffolding_leaves_plain_messages_untouched():
    plain = "just a normal message"
    assert strip_periodic_scaffolding(plain) == plain


def test_strip_recall_scaffolding_removes_user_chat_note():
    """The user_chat first-turn note is boilerplate, not the task.

    Regression: 2026-09-07 the memory engine logged three consecutive
    recalls whose query was ``'[System note: this is a side-chat with the
    operator...'`` — junk memories plus a wasted 10–30 s rerank each.
    """
    msg = USER_CHAT_FIRST_TURN_NOTE + "\n\n" + "ask the user about the deploy window"
    assert strip_recall_scaffolding(msg) == "ask the user about the deploy window"


def test_strip_recall_scaffolding_also_strips_periodic_scaffolding():
    now = datetime(2026, 9, 7, 6, 0, tzinfo=UTC)
    msg = build_initial_message("Produce today's calendar agenda.", now=now)
    assert strip_recall_scaffolding(msg) == "Produce today's calendar agenda."


def test_strip_recall_scaffolding_handles_note_and_periodic_combos():
    now = datetime(2026, 9, 7, 6, 0, tzinfo=UTC)
    periodic = build_initial_message("Review the mail queue.", now=now)
    note_first = USER_CHAT_FIRST_TURN_NOTE + "\n\n" + periodic
    periodic_first = (
        PERIODIC_PREAMBLE
        + USER_CHAT_FIRST_TURN_NOTE
        + "\n\n"
        + "Review the mail queue."
    )
    assert strip_recall_scaffolding(note_first) == "Review the mail queue."
    assert strip_recall_scaffolding(periodic_first) == "Review the mail queue."


def test_strip_recall_scaffolding_leaves_plain_and_mid_text_mentions_alone():
    plain = "just a normal message"
    assert strip_recall_scaffolding(plain) == plain
    mid = "please explain what the [System note: this is a side-chat …] means"
    assert strip_recall_scaffolding(mid) == mid
    # A bracketed note that is not the exact worker constant is content.
    other = "[System note: unrelated]\n\nreal task"
    assert strip_recall_scaffolding(other) == other


def test_user_chat_note_is_a_single_bracketed_block():
    """The stripper matches the exact constant; keep its shape honest."""
    assert USER_CHAT_FIRST_TURN_NOTE.startswith("[System note: ")
    assert USER_CHAT_FIRST_TURN_NOTE.endswith(".]")
    assert "]\n\n" not in USER_CHAT_FIRST_TURN_NOTE


def test_preamble_sets_the_single_turn_contract():
    """The preamble must state the single-turn periodic contract.

    Finish in this turn, report at the end, never schedule continuations
    or wait for events.
    """
    lowered = PERIODIC_PREAMBLE.lower()
    assert "periodic session" in lowered
    assert "report" in lowered
    assert "do not schedule continuations" in lowered
    assert "wait for future events" in lowered


def test_preamble_lets_operator_replies_supersede_task_constraints():
    """A live operator reply outranks the scheduled task's constraints.

    2026-09-01 (periodic session 28d98c21): after a READ-ONLY inbox review,
    the operator's "delete both" was refused partly because the agent kept
    applying the task's read-only framing to the live follow-up.
    """
    lowered = PERIODIC_PREAMBLE.lower()
    assert "operator may reply" in lowered
    assert "supersedes the task's constraints" in lowered


def test_preamble_states_token_usage_is_not_introspectable():
    """The preamble must be honest that the agent cannot sense exhaustion.

    Session 700f4ec1 (2026-09-21) exhausted its token budget mid-escalation
    and terminated without any report, because the old preamble told it to
    "sense" a token limit it has no way to observe.
    """
    lowered = PERIODIC_PREAMBLE.lower()
    assert "cannot introspect" in lowered
    assert "token" in lowered


def test_preamble_requires_reporting_at_breakpoints():
    """The report must be emitted at natural operational breakpoints.

    Reporting as-you-go (after each PR merge / ticket processed) is what
    makes the report resilient to a turn that terminates without warning.
    """
    lowered = PERIODIC_PREAMBLE.lower()
    assert "breakpoint" in lowered
    assert "each pr merged" in lowered
    assert "each ticket processed" in lowered
    # A partial report is the expected outcome, not an emergency fallback.
    assert "partial report is the expected outcome" in lowered


def test_partial_report_survives_context_exhaustion_by_being_incremental():
    """A session that runs out of context still leaves a PARTIAL REPORT.

    We cannot exercise a real token limit in a unit test, so we assert the
    mechanism that guarantees it: the preamble instructs the agent to emit a
    full PARTIAL REPORT at every breakpoint (restating it, not deltas), so
    the last complete report in the transcript survives a sudden cut-off.
    Regression for session 700f4ec1, which terminated before its only,
    end-of-turn report.
    """
    lowered = PERIODIC_PREAMBLE.lower()
    # The three canonical sections are still required.
    assert "done:" in lowered
    assert "escalations:" in lowered
    assert "held for next run:" in lowered
    # And the report is restated in full each time so the last one is whole.
    assert "restate the full report" in lowered


def test_preamble_surfaces_awaiting_operator_panels():
    """Open user_chat panels awaiting an operator reply must be surfaced.

    The operator's desktop alert may not persist across a chat-service
    restart, so every periodic run must enumerate open panels and report
    them as live ACTION ITEMS — never bury them under "Held for next run".
    """
    lowered = PERIODIC_PREAMBLE.lower()
    assert "call list_subsessions" in lowered
    assert "open user_chat subsessions" in lowered
    assert "awaiting an operator" in lowered
    assert "held for next run" in lowered
    assert "no pending decision" in lowered


def test_preamble_mandates_full_ticket_ids_in_report_sections():
    """Done/Escalations/Held entries must carry full mill ticket IDs.

    Drain runs were emitting truncated ID suffixes (e.g. 9c1b, ...-4b89),
    forcing the next run to re-derive the ID and defeating escalation/dedup
    correlation. The preamble must mandate full board-API ticket IDs.
    """
    lowered = PERIODIC_PREAMBLE.lower()
    assert "full ticket ids" in lowered
    assert "full mill ticket id" in lowered
    assert "never a truncated suffix" in lowered
    assert "escalation/dedup rules" in lowered


def test_initial_prompt_is_trimmed():
    assert build_initial_message("  task  \n").endswith("task")

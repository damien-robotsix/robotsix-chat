"""Tests for the shared periodic-session preamble."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from robotsix_chat.periodic.prompts import (
    CURRENT_DATETIME_FOOTER,
    CURRENT_DATETIME_HEADER,
    PERIODIC_PREAMBLE,
    build_initial_message,
    strip_periodic_scaffolding,
)


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


def test_initial_prompt_is_trimmed():
    assert build_initial_message("  task  \n").endswith("task")

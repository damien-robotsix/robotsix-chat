"""Tests for the shared periodic-session preamble."""

from __future__ import annotations

from robotsix_chat.periodic.prompts import PERIODIC_PREAMBLE, build_initial_message


def test_initial_message_prepends_the_preamble():
    msg = build_initial_message("Review the mail queue.")
    assert msg.startswith(PERIODIC_PREAMBLE)
    assert msg.endswith("Review the mail queue.")


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

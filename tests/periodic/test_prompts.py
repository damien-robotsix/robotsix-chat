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


def test_initial_prompt_is_trimmed():
    assert build_initial_message("  task  \n").endswith("task")

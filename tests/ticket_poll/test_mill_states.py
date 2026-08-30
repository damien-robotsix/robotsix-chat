"""Tests for :mod:`robotsix_chat.ticket_poll.mill_states`.

The sets must mirror ``robotsix_mill.core.states.State`` exactly — a state
the mill never emits (``IN_PROGRESS``, ``APPROVED``, ``REVIEW``) makes every
delivered ticket look dropped.
"""

from __future__ import annotations

from robotsix_chat.ticket_poll.mill_states import (
    ACTIVE_WORK_STATES,
    ALL_STATES,
    HUMAN_WAIT_STATES,
    MERGE_STATES,
    OPEN_STATES,
    PRE_WORK_STATES,
    TERMINAL_STATES,
    normalize_state,
)

# robotsix_mill/core/states.py::State, verbatim.
_REAL_MILL_STATES = frozenset(
    {
        "draft",
        "human_issue_approval",
        "ready",
        "documenting",
        "code_review",
        "deliverable",
        "human_mr_approval",
        "implement_complete",
        "waiting_auto_merge",
        "rebasing",
        "fixing_ci",
        "addressing_review",
        "done",
        "closed",
        "errored",
        "blocked",
        "asked",
        "answered",
        "awaiting_user_reply",
        "epic_open",
        "epic_closed",
    }
)


def test_all_states_match_the_real_mill_state_machine() -> None:
    """ALL_STATES is exactly the mill State enum — nothing invented, nothing missing."""
    assert ALL_STATES == _REAL_MILL_STATES


def test_no_fictional_states_anywhere() -> None:
    """The names the old check looked for do not exist in any set."""
    for fictional in ("in_progress", "approved", "review", "refining"):
        assert fictional not in ALL_STATES
        assert fictional not in ACTIVE_WORK_STATES


def test_sets_are_lowercase_subsets_of_all_states() -> None:
    """Every set is lower-case and drawn from the real state list."""
    for group in (
        PRE_WORK_STATES,
        ACTIVE_WORK_STATES,
        MERGE_STATES,
        HUMAN_WAIT_STATES,
        TERMINAL_STATES,
        OPEN_STATES,
    ):
        assert group <= ALL_STATES
        assert all(state == state.lower() for state in group)


def test_pre_work_and_active_work_are_disjoint() -> None:
    """A state cannot be both 'never touched' and 'worked on'."""
    assert not PRE_WORK_STATES & ACTIVE_WORK_STATES
    assert not PRE_WORK_STATES & MERGE_STATES


def test_delivery_path_is_covered() -> None:
    """Every hop of the normal delivery path is classified."""
    path = [
        "draft",
        "human_issue_approval",
        "ready",
        "code_review",
        "documenting",
        "deliverable",
        "implement_complete",
        "waiting_auto_merge",
        "done",
        "closed",
    ]
    for state in path:
        assert state in ALL_STATES
    assert {"code_review", "documenting", "deliverable"} <= ACTIVE_WORK_STATES
    assert {
        "implement_complete",
        "waiting_auto_merge",
        "human_mr_approval",
        "done",
    } <= (MERGE_STATES)
    assert {"done", "closed"} <= TERMINAL_STATES


def test_terminal_and_open_partition_all_states() -> None:
    """OPEN_STATES is the complement of TERMINAL_STATES."""
    assert OPEN_STATES | TERMINAL_STATES == ALL_STATES
    assert not OPEN_STATES & TERMINAL_STATES


def test_human_wait_states() -> None:
    """The human-parked states: the two approval gates, blocked, awaiting reply."""
    assert {
        "human_issue_approval",
        "human_mr_approval",
        "awaiting_user_reply",
        "blocked",
    } == HUMAN_WAIT_STATES


def test_normalize_state() -> None:
    """normalize_state lower-cases, strips, and ignores non-strings."""
    assert normalize_state(" CLOSED ") == "closed"
    assert normalize_state("done") == "done"
    assert normalize_state(None) == ""
    assert normalize_state(3) == ""

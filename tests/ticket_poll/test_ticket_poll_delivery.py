"""Delivery-evidence validation tests for ticket_poll (_check_unexpected_terminal)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.ticket_poll import (
    _check_unexpected_terminal,
    _delivery_fields,
    build_ticket_poll_tools,
)
from robotsix_chat.ticket_poll.mill_states import ACTIVE_WORK_STATES, MERGE_STATES
from tests.ticket_poll._ticket_poll_helpers import _69BE_HISTORY, _69BE_PR, _settings


def test_unexpected_terminal_null_for_active_state() -> None:
    """Ticket in code_review — not terminal, returns None."""
    assert _check_unexpected_terminal({"state": "code_review"}) is None


def test_unexpected_terminal_null_for_unknown_state() -> None:
    """Ticket with no state field — returns None (no false positives)."""
    assert _check_unexpected_terminal({}) is None


def test_unexpected_terminal_69be_delivered_not_flagged() -> None:
    """The real delivery path with a pr_url is delivered, never 'dropped'."""
    data: dict[str, Any] = {
        "state": "closed",
        "pr_url": _69BE_PR,
        "history": _69BE_HISTORY,
    }
    assert _check_unexpected_terminal(data) is None
    fields = _delivery_fields(data)
    assert fields["delivered"] is True
    assert fields["pr_url"] == _69BE_PR
    assert fields["delivery_note"] == (
        f"closed after delivery (retrospect): PR {_69BE_PR}"
    )
    assert fields["unexpected_terminal"] is None


def test_unexpected_terminal_closed_with_pr_but_no_history_is_delivered() -> None:
    """GET /tickets/{id} alone (no history) + pr_url → delivered, no flag."""
    data: dict[str, Any] = {"state": "closed", "pr_url": _69BE_PR}
    assert _check_unexpected_terminal(data) is None
    assert _delivery_fields(data)["delivered"] is True


def test_unexpected_terminal_done_with_merge_state_but_no_pr_url() -> None:
    """A merge-path state in the history counts as delivery even without pr_url."""
    data: dict[str, Any] = {
        "state": "done",
        "pr_url": None,
        "history": [
            {"state": "ready"},
            {"state": "implement_complete"},
            {"state": "done"},
        ],
    }
    assert _check_unexpected_terminal(data) is None
    fields = _delivery_fields(data)
    assert fields["delivered"] is True
    assert fields["pr_url"] is None
    assert "awaiting retrospect" in str(fields["delivery_note"])


def test_unexpected_terminal_null_with_history_blocked_state() -> None:
    """Closed ticket that was previously blocked — an agent touched it."""
    data: dict[str, Any] = {
        "state": "closed",
        "history": [{"state": "draft"}, {"state": "blocked"}, {"state": "closed"}],
    }
    assert _check_unexpected_terminal(data) is None
    assert _delivery_fields(data)["delivered"] is False


def test_unexpected_terminal_null_with_implement_events() -> None:
    """Closed ticket with implement events — transition is normal."""
    data: dict[str, Any] = {
        "state": "closed",
        "events": [{"type": "implement_started"}, {"type": "implement_complete"}],
    }
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_null_with_unblock_events() -> None:
    """Done ticket with unblock event — transition is normal."""
    assert (
        _check_unexpected_terminal({"state": "done", "events": [{"type": "unblock"}]})
        is None
    )


def test_unexpected_terminal_detects_draft_to_closed() -> None:
    """Closed ticket with only draft history and no PR — flag as dropped."""
    data: dict[str, Any] = {
        "state": "closed",
        "pr_url": None,
        "history": [{"state": "draft"}, {"state": "closed"}],
    }
    result = _check_unexpected_terminal(data)
    assert result is not None
    assert "closed" in result
    assert "draft → closed" in result
    assert "active work" in result.lower()
    fields = _delivery_fields(data)
    assert fields["delivered"] is False
    assert fields["delivery_note"] is None


def test_unexpected_terminal_detects_no_history_no_pr() -> None:
    """Closed ticket with no history, events or PR — flag."""
    result = _check_unexpected_terminal({"state": "closed"})
    assert result is not None
    assert "closed" in result


def test_unexpected_terminal_detects_ready_to_closed() -> None:
    """Closed ticket that was ready but never entered active work — flag."""
    data: dict[str, Any] = {
        "state": "closed",
        "history": [{"state": "draft"}, {"state": "ready"}, {"state": "closed"}],
    }
    assert _check_unexpected_terminal(data) is not None


def test_unexpected_terminal_human_issue_approval_to_closed_flagged() -> None:
    """A ticket rejected at the approval gate was never worked on — flag."""
    data: dict[str, Any] = {
        "state": "closed",
        "history": [
            {"state": "draft"},
            {"state": "human_issue_approval"},
            {"state": "closed"},
        ],
    }
    assert _check_unexpected_terminal(data) is not None


@pytest.mark.parametrize(
    "work_state",
    sorted(ACTIVE_WORK_STATES | MERGE_STATES),
)
def test_unexpected_terminal_null_for_every_real_work_state(work_state: str) -> None:
    """Every real active-work / merge state in the history suppresses the flag."""
    data: dict[str, Any] = {
        "state": "closed",
        "history": [{"state": "draft"}, {"state": work_state}, {"state": "closed"}],
    }
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_fictional_states_do_not_count() -> None:
    """The old invented names are not evidence of anything — flag stays."""
    data: dict[str, Any] = {
        "state": "closed",
        "history": [
            {"state": "DRAFT"},
            {"state": "APPROVED"},
            {"state": "IN_PROGRESS"},
            {"state": "CLOSED"},
        ],
    }
    assert _check_unexpected_terminal(data) is not None


def test_unexpected_terminal_null_with_merge_event() -> None:
    """Done ticket with a merge event — normal."""
    data: dict[str, Any] = {
        "state": "done",
        "history": [{"state": "draft"}],
        "events": [{"type": "pr_merged"}],
    }
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_null_with_history_note_keyword() -> None:
    """A mill history row whose note mentions a merge counts as activity."""
    data: dict[str, Any] = {
        "state": "closed",
        "history": [{"state": "closed", "note": "Clean delivery — PR merged"}],
    }
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_case_insensitive_state() -> None:
    """State comparison is case-insensitive."""
    data: dict[str, Any] = {
        "state": "CLOSED",
        "history": [{"state": "Draft"}, {"state": "CLOSED"}],
    }
    assert _check_unexpected_terminal(data) is not None
    data["history"].insert(1, {"state": "Implement_Complete"})
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_uses_to_field_in_history() -> None:
    """History entries may use 'to' instead of 'state'."""
    data: dict[str, Any] = {
        "state": "closed",
        "history": [{"to": "draft"}, {"to": "code_review"}, {"to": "closed"}],
    }
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_uses_action_field_in_events() -> None:
    """Events may use 'action' instead of 'type'."""
    assert (
        _check_unexpected_terminal({"state": "done", "events": [{"action": "resume"}]})
        is None
    )


def test_unexpected_terminal_skips_non_dict_entries() -> None:
    """Non-dict entries in history/events are safely skipped."""
    data: dict[str, Any] = {
        "state": "closed",
        "history": ["not a dict", None, {"state": "blocked"}],
        "events": [None, "also not a dict"],
    }
    assert _check_unexpected_terminal(data) is None


async def test_ticket_poll_batch_direct_carries_delivery_fields(
    respx_mock: respx.MockRouter,
) -> None:
    """Batch path: each element carries pr_url / delivered / delivery_note."""
    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(200, text=json.dumps([{"ticket_id": "t-b"}]))
    )
    respx_mock.get("http://board:8077/tickets/t-b").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-b", "state": "done", "pr_url": _69BE_PR})
        )
    )
    respx_mock.get("http://board:8077/tickets/t-b/history").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps([{"state": "ready"}, {"state": "implement_complete"}]),
        )
    )
    tools = build_ticket_poll_tools(_settings())
    result = json.loads(await tools[1](["t-b"]))
    entry = result["tickets"][0]
    assert entry["delivered"] is True
    assert entry["pr_url"] == _69BE_PR
    assert entry["unexpected_terminal"] is None
    assert entry["data"]["history"][1]["state"] == "implement_complete"

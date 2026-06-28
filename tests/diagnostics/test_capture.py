"""Tests for DiagnosticCapture — poll-based BLOCKED transition detection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robotsix_chat.diagnostics.capture import DiagnosticCapture
from robotsix_chat.diagnostics.store import DiagnosticRecord, DiagnosticStore


def _fixed_clock() -> datetime:
    """Return a fixed timestamp for deterministic tests."""
    return datetime(2025, 6, 27, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake BoardReader — returns pre-configured responses
# ---------------------------------------------------------------------------


class _FakeBoardReader:
    """A fake BoardReader that returns pre-configured JSON strings."""

    def __init__(
        self,
        list_response: str = "[]",
        ticket_responses: dict[str, str] | None = None,
    ) -> None:
        self._list_response = list_response
        self._ticket_responses = ticket_responses or {}
        self._list_calls: list[dict[str, Any]] = []
        self._get_calls: list[str] = []

    async def list_tickets(
        self,
        *,
        repo_id: str = "",
        include_closed: bool = False,
        state: str = "",
    ) -> str:
        self._list_calls.append(
            {
                "repo_id": repo_id,
                "include_closed": include_closed,
                "state": state,
            }
        )
        return self._list_response

    async def get_ticket(self, ticket_id: str) -> str:
        self._get_calls.append(ticket_id)
        return self._ticket_responses.get(ticket_id, "{}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_blocked_list(ticket_ids: list[str]) -> str:
    """Return a JSON list of minimal BLOCKED tickets."""
    tickets = [{"id": tid, "state": "BLOCKED"} for tid in ticket_ids]
    return json.dumps(tickets)


def _make_open_list(ticket_ids_and_states: list[tuple[str, str]]) -> str:
    """Return a JSON list of tickets with arbitrary states."""
    tickets = [{"id": tid, "state": st} for tid, st in ticket_ids_and_states]
    return json.dumps(tickets)


def _make_ticket_detail(
    ticket_id: str,
    *,
    description: str = "",
    comments: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    repo_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal ticket detail dict."""
    return {
        "id": ticket_id,
        "title": f"Ticket {ticket_id}",
        "description": description,
        "state": "BLOCKED",
        "repo_id": repo_id,
        "comments": comments or [],
        "events": events or [],
        "metadata": metadata or {},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_poll_no_blocked_tickets(tmp_path: Path) -> None:
    """When no tickets are BLOCKED, poll returns empty list."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)
    board = _FakeBoardReader(
        list_response="[]",
    )
    capture = DiagnosticCapture(board, store)  # type: ignore[arg-type]

    async def _run() -> list[DiagnosticRecord]:
        return await capture.poll()

    import asyncio

    records = asyncio.run(_run())
    assert records == []


def test_poll_detects_blocked_and_captures(tmp_path: Path) -> None:
    """A newly-BLOCKED ticket triggers a diagnostic record capture."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)

    ticket_data = _make_ticket_detail(
        "test-ticket",
        description="Blocked because CI failed",
        events=[
            {
                "to_state": "BLOCKED",
                "comment": "CI pipeline failed — blocking for investigation",
            }
        ],
        comments=[
            {
                "body": "🔍 [Trace: tr-456](https://langfuse.robotsix.net/trace/tr-456)",
            }
        ],
        repo_id="my-repo",
    )

    board = _FakeBoardReader(
        list_response=_make_blocked_list(["test-ticket"]),
        ticket_responses={
            "test-ticket": json.dumps(ticket_data),
        },
    )
    # Second list_tickets call returns empty open list
    board._list_response = _make_blocked_list(["test-ticket"])

    capture = DiagnosticCapture(board, store)  # type: ignore[arg-type]

    import asyncio

    async def _run() -> list[DiagnosticRecord]:
        return await capture.poll()

    records = asyncio.run(_run())
    assert len(records) == 1
    r = records[0]
    assert r.ticket_id == "test-ticket"
    assert "CI pipeline failed" in r.block_reason
    assert "tr-456" in r.langfuse_trace
    assert "my-repo" in r.clone_repo_info


def test_poll_skips_already_captured(tmp_path: Path) -> None:
    """Tickets already captured are not captured again."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)
    # Pre-seed a record
    existing = DiagnosticRecord(
        ticket_id="already-captured",
        block_reason="Old block",
        langfuse_trace="",
        ticket_history="{}",
        branch_pr_links="",
        clone_repo_info="",
        captured_at=_fixed_clock().isoformat(),
    )
    store.add(existing)

    board = _FakeBoardReader(
        list_response=_make_blocked_list(["already-captured"]),
    )
    capture = DiagnosticCapture(board, store)  # type: ignore[arg-type]

    import asyncio

    records = asyncio.run(capture.poll())
    assert records == []


def test_poll_updates_known_states(tmp_path: Path) -> None:
    """After polling, known states reflect current ticket states."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)
    board = _FakeBoardReader(
        list_response=_make_open_list(
            [
                ("t1", "IN_PROGRESS"),
                ("t2", "READY"),
            ]
        ),
    )
    capture = DiagnosticCapture(board, store)  # type: ignore[arg-type]

    import asyncio

    asyncio.run(capture.poll())

    assert store.get_known_state("t1") == "IN_PROGRESS"
    assert store.get_known_state("t2") == "READY"


def test_extract_langfuse_trace_from_events(tmp_path: Path) -> None:
    """Langfuse trace is extracted from event comments."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)

    ticket_data = _make_ticket_detail(
        "trace-test",
        events=[
            {
                "to_state": "BLOCKED",
                "comment": (
                    "🔍 [Trace: evt-789]"
                    "(https://langfuse.robotsix.net/trace/evt-789) — check here"
                ),
            }
        ],
    )

    board = _FakeBoardReader(
        list_response=_make_blocked_list(["trace-test"]),
        ticket_responses={"trace-test": json.dumps(ticket_data)},
    )
    capture = DiagnosticCapture(board, store)  # type: ignore[arg-type]

    import asyncio

    records = asyncio.run(capture.poll())
    assert len(records) == 1
    assert "evt-789" in records[0].langfuse_trace


def test_extract_langfuse_trace_from_description(tmp_path: Path) -> None:
    """Langfuse trace is extracted from description when no comments/events have it."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)

    ticket_data = _make_ticket_detail(
        "desc-test",
        description="Blocked — see 🔍 [Trace: desc-111](https://langfuse.robotsix.net/trace/desc-111)",
    )

    board = _FakeBoardReader(
        list_response=_make_blocked_list(["desc-test"]),
        ticket_responses={"desc-test": json.dumps(ticket_data)},
    )
    capture = DiagnosticCapture(board, store)  # type: ignore[arg-type]

    import asyncio

    records = asyncio.run(capture.poll())
    assert len(records) == 1
    assert "desc-111" in records[0].langfuse_trace


def test_extract_branch_pr_links(tmp_path: Path) -> None:
    """GitHub PR/issue URLs are extracted from description and comments."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)

    ticket_data = _make_ticket_detail(
        "pr-test",
        description="See https://github.com/org/repo/pull/42 for the fix",
        comments=[
            {
                "body": "Also related: https://github.com/org/repo/issues/99",
            }
        ],
    )

    board = _FakeBoardReader(
        list_response=_make_blocked_list(["pr-test"]),
        ticket_responses={"pr-test": json.dumps(ticket_data)},
    )
    capture = DiagnosticCapture(board, store)  # type: ignore[arg-type]

    import asyncio

    records = asyncio.run(capture.poll())
    assert len(records) == 1
    assert "github.com/org/repo/pull/42" in records[0].branch_pr_links
    assert "github.com/org/repo/issues/99" in records[0].branch_pr_links


def test_extract_clone_repo_info_from_metadata(tmp_path: Path) -> None:
    """Clone-target and repo info is extracted from ticket metadata."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)

    ticket_data = _make_ticket_detail(
        "clone-test",
        repo_id="target-repo",
        metadata={
            "clone_target": "git@github.com:org/target.git",
            "registration": "registered",
        },
    )

    board = _FakeBoardReader(
        list_response=_make_blocked_list(["clone-test"]),
        ticket_responses={"clone-test": json.dumps(ticket_data)},
    )
    capture = DiagnosticCapture(board, store)  # type: ignore[arg-type]

    import asyncio

    records = asyncio.run(capture.poll())
    assert len(records) == 1
    assert "target-repo" in records[0].clone_repo_info
    assert "clone_target" in records[0].clone_repo_info
    assert "registered" in records[0].clone_repo_info


def test_parse_ticket_list_invalid_json(tmp_path: Path) -> None:
    """Non-JSON list responses are handled gracefully."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)
    board = _FakeBoardReader(list_response="not json")
    capture = DiagnosticCapture(board, store)  # type: ignore[arg-type]

    import asyncio

    records = asyncio.run(capture.poll())
    assert records == []


def test_capture_ticket_invalid_json(tmp_path: Path) -> None:
    """A ticket detail that isn't valid JSON is handled gracefully."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)
    board = _FakeBoardReader(
        list_response=_make_blocked_list(["bad-ticket"]),
        ticket_responses={"bad-ticket": "not valid json {{{"},
    )
    capture = DiagnosticCapture(board, store)  # type: ignore[arg-type]

    import asyncio

    records = asyncio.run(capture.poll())
    assert records == []
    assert store.has_ticket("bad-ticket") is False


def test_block_reason_from_comment_fallback(tmp_path: Path) -> None:
    """When no BLOCKED event has a comment, fall back to a comment with 'block'."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)

    ticket_data = _make_ticket_detail(
        "fallback-test",
        events=[{"to_state": "BLOCKED", "comment": ""}],
        comments=[
            {"body": "regular comment"},
            {"body": "This ticket is blocked on the upstream API fix"},
        ],
    )

    board = _FakeBoardReader(
        list_response=_make_blocked_list(["fallback-test"]),
        ticket_responses={"fallback-test": json.dumps(ticket_data)},
    )
    capture = DiagnosticCapture(board, store)  # type: ignore[arg-type]

    import asyncio

    records = asyncio.run(capture.poll())
    assert len(records) == 1
    assert "blocked on the upstream API fix" in records[0].block_reason


def test_build_diagnostics_tools_disabled() -> None:
    """Disabled diagnostics returns no tools."""
    from robotsix_chat.config import DiagnosticsSettings
    from robotsix_chat.diagnostics import build_diagnostics_tools

    tools = build_diagnostics_tools(DiagnosticsSettings(enabled=False))
    assert tools == []


def test_build_diagnostics_tools_enabled() -> None:
    """Enabled diagnostics returns list_diagnostic_records tool."""
    from robotsix_chat.config import DiagnosticsSettings
    from robotsix_chat.diagnostics import build_diagnostics_tools

    settings = DiagnosticsSettings(enabled=True, data_dir=":memory:")  # won't be used
    tools = build_diagnostics_tools(settings)
    assert len(tools) == 1
    assert tools[0].__name__ == "list_diagnostic_records"

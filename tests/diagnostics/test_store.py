"""Tests for DiagnosticStore persistence and API."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from robotsix_chat.diagnostics.store import DiagnosticRecord, DiagnosticStore


def _fixed_clock() -> datetime:
    """Return a fixed timestamp for deterministic tests."""
    return datetime(2025, 6, 27, 12, 0, 0, tzinfo=UTC)


def test_add_and_list(tmp_path: Path) -> None:
    """Records can be added and listed."""
    store = DiagnosticStore(
        path=tmp_path / "diagnostics.json",
        clock=_fixed_clock,
    )
    record = DiagnosticRecord(
        ticket_id="test-1",
        block_reason="Something broke",
        langfuse_trace="🔍 [Trace: abc123](https://langfuse.robotsix.net/trace/abc123)",
        ticket_history='{"state": "BLOCKED"}',
        branch_pr_links="https://github.com/org/repo/pull/42",
        clone_repo_info="repo_id: my-repo",
        captured_at=_fixed_clock().isoformat(),
    )
    store.add(record)

    records = store.list()
    assert len(records) == 1
    assert records[0].ticket_id == "test-1"
    assert records[0].block_reason == "Something broke"


def test_get_returns_most_recent(tmp_path: Path) -> None:
    """get() returns the most recent record for a ticket_id."""
    store = DiagnosticStore(
        path=tmp_path / "diagnostics.json",
        clock=_fixed_clock,
    )
    r1 = DiagnosticRecord(
        ticket_id="dup-1",
        block_reason="First block",
        langfuse_trace="",
        ticket_history="{}",
        branch_pr_links="",
        clone_repo_info="",
        captured_at=_fixed_clock().isoformat(),
    )
    r2 = DiagnosticRecord(
        ticket_id="dup-1",
        block_reason="Second block",
        langfuse_trace="",
        ticket_history="{}",
        branch_pr_links="",
        clone_repo_info="",
        captured_at=_fixed_clock().isoformat(),
    )
    store.add(r1)
    store.add(r2)

    found = store.get("dup-1")
    assert found is not None
    assert found.block_reason == "Second block"


def test_get_unknown_returns_none(tmp_path: Path) -> None:
    """get() returns None for an unknown ticket_id."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)
    assert store.get("nonexistent") is None


def test_has_ticket(tmp_path: Path) -> None:
    """has_ticket() correctly reports record presence."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)
    record = DiagnosticRecord(
        ticket_id="check-1",
        block_reason="x",
        langfuse_trace="",
        ticket_history="{}",
        branch_pr_links="",
        clone_repo_info="",
        captured_at=_fixed_clock().isoformat(),
    )
    store.add(record)

    assert store.has_ticket("check-1") is True
    assert store.has_ticket("check-2") is False


# ---------------------------------------------------------------------------
# known states
# ---------------------------------------------------------------------------


def test_known_state_set_and_get(tmp_path: Path) -> None:
    """Known ticket states can be set and retrieved."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)

    store.set_known_state("ticket-a", "IN_PROGRESS")
    store.set_known_state("ticket-b", "BLOCKED")

    assert store.get_known_state("ticket-a") == "IN_PROGRESS"
    assert store.get_known_state("ticket-b") == "BLOCKED"
    assert store.get_known_state("ticket-c") is None


def test_get_known_states_returns_copy(tmp_path: Path) -> None:
    """get_known_states() returns a copy of all states."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)

    store.set_known_state("a", "READY")
    store.set_known_state("b", "IN_PROGRESS")

    states = store.get_known_states()
    assert states == {"a": "READY", "b": "IN_PROGRESS"}

    # Verify it's a copy (mutation doesn't affect store)
    states["a"] = "CHANGED"
    assert store.get_known_state("a") == "READY"


def test_known_state_overwrite(tmp_path: Path) -> None:
    """Setting the same ticket_id overwrites the previous state."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)

    store.set_known_state("ticket", "DRAFT")
    store.set_known_state("ticket", "READY")
    assert store.get_known_state("ticket") == "READY"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_persist_and_reload(tmp_path: Path) -> None:
    """Records and known states survive a store reload."""
    path = tmp_path / "diagnostics.json"

    # First session
    store1 = DiagnosticStore(path=path, clock=_fixed_clock)
    record = DiagnosticRecord(
        ticket_id="persist-1",
        block_reason="Persist test",
        langfuse_trace="trace-url",
        ticket_history="hist",
        branch_pr_links="links",
        clone_repo_info="clone",
        captured_at=_fixed_clock().isoformat(),
    )
    store1.add(record)
    store1.set_known_state("persist-1", "BLOCKED")

    # Second session — should load from disk
    store2 = DiagnosticStore(path=path, clock=_fixed_clock)
    records = store2.list()
    assert len(records) == 1
    assert records[0].ticket_id == "persist-1"
    assert records[0].block_reason == "Persist test"
    assert store2.get_known_state("persist-1") == "BLOCKED"


def test_load_missing_file_starts_empty(tmp_path: Path) -> None:
    """A missing file yields an empty store."""
    store = DiagnosticStore(
        path=tmp_path / "nonexistent" / "diag.json",
        clock=_fixed_clock,
    )
    assert store.list() == []
    assert store.get_known_states() == {}


def test_load_corrupt_file_starts_empty(tmp_path: Path) -> None:
    """A corrupt JSON file yields an empty store."""
    path = tmp_path / "diagnostics.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {{{")

    store = DiagnosticStore(path=path, clock=_fixed_clock)
    assert store.list() == []
    assert store.get_known_states() == {}


def test_load_empty_file_starts_empty(tmp_path: Path) -> None:
    """An empty JSON file yields an empty store."""
    path = tmp_path / "diagnostics.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")

    store = DiagnosticStore(path=path, clock=_fixed_clock)
    assert store.list() == []
    assert store.get_known_states() == {}


def test_add_creates_parent_directory(tmp_path: Path) -> None:
    """Adding a record creates the parent directory if needed."""
    path = tmp_path / "subdir" / "diagnostics.json"
    store = DiagnosticStore(path=path, clock=_fixed_clock)
    record = DiagnosticRecord(
        ticket_id="dir-test",
        block_reason="",
        langfuse_trace="",
        ticket_history="",
        branch_pr_links="",
        clone_repo_info="",
        captured_at=_fixed_clock().isoformat(),
    )
    store.add(record)

    assert path.exists()
    data = json.loads(path.read_text())
    assert len(data["records"]) == 1
    assert data["records"][0]["ticket_id"] == "dir-test"


def test_forward_compat_unknown_keys(tmp_path: Path) -> None:
    """Unknown keys in the JSON are tolerated on load."""
    path = tmp_path / "diagnostics.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "ticket_id": "fc-1",
                        "block_reason": "test",
                        "langfuse_trace": "",
                        "ticket_history": "",
                        "branch_pr_links": "",
                        "clone_repo_info": "",
                        "captured_at": "",
                        "future_field": "ignored",
                    }
                ],
                "known_states": {"fc-1": "BLOCKED"},
                "future_section": [1, 2, 3],
            }
        )
    )

    store = DiagnosticStore(path=path, clock=_fixed_clock)
    assert len(store.list()) == 1
    assert store.list()[0].ticket_id == "fc-1"
    assert store.get_known_state("fc-1") == "BLOCKED"


def test_list_returns_independent_copy(tmp_path: Path) -> None:
    """list() returns a list independent of the internal store."""
    store = DiagnosticStore(path=tmp_path / "diagnostics.json", clock=_fixed_clock)
    record = DiagnosticRecord(
        ticket_id="copy-test",
        block_reason="",
        langfuse_trace="",
        ticket_history="",
        branch_pr_links="",
        clone_repo_info="",
        captured_at=_fixed_clock().isoformat(),
    )
    store.add(record)

    records = store.list()
    records.clear()  # should not affect store
    assert len(store.list()) == 1

"""Tests for the shadow ``diagnostic_events`` module.

Covers ``emit_diagnostic_event``, ``list_diagnostic_events``, and the
internal ``_event_exists`` helper.

Uses an importlib-based fake-module harness to load the shadow module
directly from its source file, because the ``robotsix_mill`` shadow
package's ``__init__.py`` requires the real ``robotsix_mill`` to be
installed.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Load the shadow module via importlib — stub out mill siblings first
# ---------------------------------------------------------------------------

_SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "robotsix_mill"


class _FakeSettings:
    """Minimal stand-in for ``robotsix_mill.config.Settings``.

    Returns a deterministic JSONL file path under *root*.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def diagnostic_events_file_for(self, board_id: str) -> Path:
        """Return the per-board JSONL event-store path."""
        return self._root / board_id / "diagnostic_events.jsonl"


def _make_pkg_stub(name: str) -> Any:
    """Create a mock module that satisfies package-resolution imports."""
    mod = types.ModuleType(name)
    mod.__path__ = []
    mod.__package__ = name
    return mod


_stubs: dict[str, Any] = {
    "robotsix_mill": _make_pkg_stub("robotsix_mill"),
    "robotsix_mill.config": types.ModuleType("robotsix_mill.config"),
}
_stubs["robotsix_mill.config"].Settings = _FakeSettings

for _mod_name, _stub in _stubs.items():
    sys.modules[_mod_name] = _stub

_spec = importlib.util.spec_from_file_location(
    "robotsix_mill.agents.runners.diagnostic_events",
    _SOURCE_ROOT / "agents" / "runners" / "diagnostic_events.py",
)
assert _spec is not None, f"Could not load spec for {_SOURCE_ROOT / 'agents' / 'runners' / 'diagnostic_events.py'}"
assert _spec.loader is not None
_diag = importlib.util.module_from_spec(_spec)
_diag.__package__ = "robotsix_mill.agents.runners"
sys.modules["robotsix_mill.agents.runners.diagnostic_events"] = _diag
_spec.loader.exec_module(_diag)

emit_diagnostic_event = _diag.emit_diagnostic_event
list_diagnostic_events = _diag.list_diagnostic_events
_event_exists = _diag._event_exists
DiagnosticEvent = _diag.DiagnosticEvent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> _FakeSettings:
    """Provide fake settings pointing at a temp directory."""
    return _FakeSettings(tmp_path)


@pytest.fixture
def board_id() -> str:
    """Provide a stable test board id."""
    return "test-board"


# ---------------------------------------------------------------------------
# emit_diagnostic_event
# ---------------------------------------------------------------------------


class TestEmitDiagnosticEvent:
    """Tests for ``emit_diagnostic_event(settings, board_id, category, ticket_id, reason, normalized_key)``."""

    def test_happy_path(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Returns ``True``; file created; appended line parses as JSON with all keys."""
        result = emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "TKT-001", "some reason", "key-abc"
        )
        assert result is True

        path = settings.diagnostic_events_file_for(board_id)
        assert path.is_file()
        lines = path.read_text("utf-8").strip().split("\n")
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["category"] == "CI_FAILURE"
        assert obj["ticket_id"] == "TKT-001"
        assert obj["repo_id"] == board_id
        assert obj["reason"] == "some reason"
        assert obj["normalized_key"] == "key-abc"
        assert "timestamp" in obj

    def test_parent_directory_auto_created(
        self, settings: _FakeSettings
    ) -> None:
        """Board directory that does not exist is created automatically."""
        board_id = "deep/nested/board"
        result = emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "TKT-001", "r", "key-1"
        )
        assert result is True
        path = settings.diagnostic_events_file_for(board_id)
        assert path.is_file()

    def test_dedup_same_key(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Second call with same ``(ticket_id, normalized_key)`` returns ``False``."""
        assert emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "TKT-001", "r", "key-1"
        ) is True
        assert emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "TKT-001", "r", "key-1"
        ) is False
        # File contains exactly one line.
        lines = (
            settings.diagnostic_events_file_for(board_id)
            .read_text("utf-8")
            .strip()
            .split("\n")
        )
        assert len(lines) == 1

    def test_dedup_different_key(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Two calls with same ``ticket_id`` but different ``normalized_key`` both succeed."""
        assert emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "TKT-001", "r", "key-1"
        ) is True
        assert emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "TKT-001", "r", "key-2"
        ) is True
        lines = (
            settings.diagnostic_events_file_for(board_id)
            .read_text("utf-8")
            .strip()
            .split("\n")
        )
        assert len(lines) == 2

    def test_warning_log_on_dedup(
        self,
        settings: _FakeSettings,
        board_id: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dedup triggers a WARNING record with ``ticket_id`` and ``normalized_key``."""
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "TKT-001", "r", "key-abc"
        )
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "TKT-001", "r", "key-abc"
        )
        # At least one WARNING should contain the dedup message.
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        dedup_records = [
            r for r in warnings if "skipping duplicate event" in r.message
        ]
        assert len(dedup_records) >= 1
        msg = dedup_records[0].message
        assert "TKT-001" in msg
        assert "key-abc" in msg

    def test_io_error_failsafe(
        self,
        settings: _FakeSettings,
        board_id: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Making the target path a directory (not a file) causes ``open`` to fail;
        returns ``False`` without raising and logs a WARNING."""
        path = settings.diagnostic_events_file_for(board_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()  # directory, not a file → open(..., "a") raises

        result = emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "TKT-001", "r", "key-1"
        )
        assert result is False

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        fail_records = [
            r for r in warnings if "failed to emit event" in r.message
        ]
        assert len(fail_records) >= 1


# ---------------------------------------------------------------------------
# list_diagnostic_events
# ---------------------------------------------------------------------------


class TestListDiagnosticEvents:
    """Tests for ``list_diagnostic_events(settings, board_id, *, category=None)``."""

    def test_non_existent_file(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Returns ``[]`` when the JSONL file does not exist."""
        assert list_diagnostic_events(settings, board_id) == []

    def test_round_trip(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Emit one event, then list returns one ``DiagnosticEvent`` with matching fields."""
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "TKT-001", "reason text", "key-1"
        )
        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 1
        ev = events[0]
        assert ev.category == "CI_FAILURE"
        assert ev.ticket_id == "TKT-001"
        assert ev.repo_id == board_id
        assert ev.reason == "reason text"
        assert ev.normalized_key == "key-1"

    def test_category_filter(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Only events matching the requested category are returned."""
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "TKT-001", "r", "key-1"
        )
        emit_diagnostic_event(
            settings, board_id, "OTHER", "TKT-002", "r", "key-2"
        )
        ci_events = list_diagnostic_events(settings, board_id, category="CI_FAILURE")
        assert len(ci_events) == 1
        assert ci_events[0].category == "CI_FAILURE"

    def test_malformed_line_skipped(
        self,
        settings: _FakeSettings,
        board_id: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One valid line + one ``"not-json"`` line → returns one event; logs WARNING."""
        path = settings.diagnostic_events_file_for(board_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"category":"CI_FAILURE","ticket_id":"TKT-001","repo_id":"b","reason":"r","normalized_key":"k","timestamp":"t"}\n'
            'not-json\n'
        )
        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 1
        assert events[0].ticket_id == "TKT-001"

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("malformed" in r.message for r in warnings)

    def test_missing_required_field_skipped(
        self,
        settings: _FakeSettings,
        board_id: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A JSONL line missing ``normalized_key`` is skipped."""
        path = settings.diagnostic_events_file_for(board_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"category":"CI_FAILURE","ticket_id":"TKT-001","repo_id":"b","reason":"r","timestamp":"t"}\n'
        )
        events = list_diagnostic_events(settings, board_id)
        assert events == []

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("invalid entry" in r.message for r in warnings)

    def test_empty_line_tolerance(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Trailing blank lines are tolerated — only genuine events are returned."""
        path = settings.diagnostic_events_file_for(board_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"category":"CI_FAILURE","ticket_id":"TKT-001","repo_id":"b","reason":"r","normalized_key":"k","timestamp":"t"}\n'
            "\n"
            "\n"
        )
        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 1


# ---------------------------------------------------------------------------
# _event_exists (internal helper)
# ---------------------------------------------------------------------------


class TestEventExists:
    """Tests for ``_event_exists(path, ticket_id, normalized_key)``."""

    def test_non_existent_path(self, tmp_path: Path) -> None:
        """Non-existent path → ``False``."""
        path = tmp_path / "nonexistent.jsonl"
        assert _event_exists(path, "TKT-001", "key-1") is False

    def test_matching_pair_found(self, tmp_path: Path) -> None:
        """File contains a matching ``(ticket_id, normalized_key)`` pair → ``True``."""
        path = tmp_path / "events.jsonl"
        path.write_text(
            '{"ticket_id":"TKT-001","normalized_key":"key-1"}\n'
        )
        assert _event_exists(path, "TKT-001", "key-1") is True

    def test_no_match(self, tmp_path: Path) -> None:
        """File has entries but none match the query → ``False``."""
        path = tmp_path / "events.jsonl"
        path.write_text(
            '{"ticket_id":"TKT-001","normalized_key":"key-1"}\n'
        )
        assert _event_exists(path, "TKT-001", "key-999") is False

    def test_malformed_line_does_not_raise(self, tmp_path: Path) -> None:
        """A malformed JSONL line does not raise; returns ``False`` for non-match."""
        path = tmp_path / "events.jsonl"
        path.write_text(
            '{"ticket_id":"TKT-001","normalized_key":"key-1"}\n'
            "not-json\n"
        )
        # Querying a key not present → False, no crash.
        assert _event_exists(path, "TKT-001", "key-999") is False
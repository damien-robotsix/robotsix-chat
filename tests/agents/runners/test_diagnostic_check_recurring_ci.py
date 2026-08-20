"""Tests for the shadow ``diagnostic_check_recurring_ci`` module.

Covers the vendored :class:`RecurringCIFailureCheck` (same-key across
distinct tickets) and the new :class:`MultiCauseCIFailureCheck`
(distinct keys on a single ticket).

Uses an importlib-based fake-module harness to load the shadow modules
directly from their source files, because the ``robotsix_mill`` shadow
package's ``__init__.py`` requires the real ``robotsix_mill`` to be
installed, and its sibling mill modules (core, config, service) do not
exist in this checkout.  The real shadow ``diagnostic_events.py`` is
loaded the same way so the checks run against the actual JSONL store
behaviour (emit / dedup on ``(ticket_id, normalized_key)``).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Load the shadow modules via importlib — stub out mill siblings first
# ---------------------------------------------------------------------------

_SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "robotsix_mill"


class _FakeSettings:
    """Minimal stand-in for ``robotsix_mill.config.Settings``.

    Carries the diagnostic thresholds as plain attributes (any attribute
    not supplied at construction is absent, mirroring a mill build whose
    Settings predates ``diagnostic_ci_multicause_threshold``).
    """

    diagnostic_ci_failure_threshold: int
    diagnostic_ci_multicause_threshold: int

    def __init__(self, root: Path, **overrides: Any) -> None:
        self._root = root
        for name, value in overrides.items():
            setattr(self, name, value)

    def diagnostic_events_file_for(self, board_id: str) -> Path:
        """Return the per-board JSONL event-store path."""
        return self._root / board_id / "diagnostic_events.jsonl"


class _FakeTicket:
    """A ticket as returned/created by the fake service."""

    def __init__(
        self,
        ticket_id: str,
        title: str,
        state: str = "draft",
        source: Any = None,
        kind: Any = None,
        body: str = "",
    ) -> None:
        self.id = ticket_id
        self.title = title
        self.state = state
        self.source = source
        self.kind = kind
        self.body = body


_CREATED_TICKETS: list[_FakeTicket] = []


class _FakeTicketService:
    """Record created tickets; ``list`` serves the shared ticket list."""

    def __init__(self, settings: Any = None, board_id: str = "") -> None:
        self.settings = settings
        self.board_id = board_id

    def list(self) -> list[_FakeTicket]:
        """Return every ticket created so far."""
        return list(_CREATED_TICKETS)

    def create(
        self,
        title: str,
        body: str,
        source: Any = None,
        kind: Any = None,
    ) -> _FakeTicket:
        """Record and return a new ticket."""
        ticket = _FakeTicket(
            f"T-{len(_CREATED_TICKETS) + 1}",
            title,
            source=source,
            kind=kind,
            body=body,
        )
        _CREATED_TICKETS.append(ticket)
        return ticket


@dataclass
class _FakeDiagnosticCheckContext:
    """Mirror of the mill's DiagnosticCheckContext."""

    board_id: str
    settings: Any


@dataclass
class _FakeDiagnosticCheckResult:
    """Mirror of the mill's DiagnosticCheckResult."""

    name: str
    ok: bool
    summary: str
    drafts_created: list[dict[str, Any]] = field(default_factory=list)


_FAKE_CHECK_REGISTRY: list[Any] = []


def _fake_register_check(check: Any) -> Any:
    _FAKE_CHECK_REGISTRY.append(check)
    return check


_fake_diagnostic_checks = SimpleNamespace(
    DIAGNOSTIC_CHECKS=_FAKE_CHECK_REGISTRY,
    DiagnosticCheck=Any,  # annotation-only in the shadow module
    DiagnosticCheckContext=_FakeDiagnosticCheckContext,
    DiagnosticCheckResult=_FakeDiagnosticCheckResult,
    register_check=_fake_register_check,
)
_fake_core_models = SimpleNamespace(
    SourceKind=SimpleNamespace(AGENT="agent"),
    TicketKind=SimpleNamespace(TASK="task"),
)
_fake_core_service = SimpleNamespace(TicketService=_FakeTicketService)
_fake_core_states = SimpleNamespace(DONE_OR_CLOSED=frozenset({"closed", "done"}))
_fake_config = SimpleNamespace(Settings=_FakeSettings)


def _make_pkg_stub(name: str) -> Any:
    """Create a mock module that satisfies package-resolution imports."""
    mod = types.ModuleType(name)
    mod.__path__ = []
    mod.__package__ = name
    return mod


_stubs: dict[str, Any] = {
    "robotsix_mill": _make_pkg_stub("robotsix_mill"),
    "robotsix_mill.core": _make_pkg_stub("robotsix_mill.core"),
    "robotsix_mill.core.models": _fake_core_models,
    "robotsix_mill.core.service": _fake_core_service,
    "robotsix_mill.core.states": _fake_core_states,
    "robotsix_mill.agents": _make_pkg_stub("robotsix_mill.agents"),
    "robotsix_mill.agents.runners": _make_pkg_stub("robotsix_mill.agents.runners"),
    "robotsix_mill.agents.runners.diagnostic_checks": _fake_diagnostic_checks,
    "robotsix_mill.config": _fake_config,
}

for _mod_name, _stub in _stubs.items():
    sys.modules[_mod_name] = _stub


def _load_shadow_module(rel_path: str, full_name: str, package: str) -> Any:
    """Load a shadow module from its source file via importlib."""
    spec = importlib.util.spec_from_file_location(full_name, _SOURCE_ROOT / rel_path)
    assert spec is not None, f"Could not load spec for {_SOURCE_ROOT / rel_path}"
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = package
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


_diagnostic_events = _load_shadow_module(
    "agents/runners/diagnostic_events.py",
    "robotsix_mill.agents.runners.diagnostic_events",
    "robotsix_mill.agents.runners",
)
_check_module = _load_shadow_module(
    "agents/runners/diagnostic_check_recurring_ci.py",
    "robotsix_mill.agents.runners.diagnostic_check_recurring_ci",
    "robotsix_mill.agents.runners",
)

RecurringCIFailureCheck = _check_module.RecurringCIFailureCheck
MultiCauseCIFailureCheck = _check_module.MultiCauseCIFailureCheck
emit_diagnostic_event = _diagnostic_events.emit_diagnostic_event

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> _FakeSettings:
    """Provide settings with both thresholds at their 3 defaults."""
    return _FakeSettings(
        tmp_path / "data",
        diagnostic_ci_failure_threshold=3,
        diagnostic_ci_multicause_threshold=3,
    )


@pytest.fixture
def board_id() -> str:
    """Provide a stable test board id."""
    return "test-board"


@pytest.fixture(autouse=True)
def _reset_shared_state() -> Iterator[None]:
    """Isolate the fake ticket list and check registry between tests."""
    registry_snapshot = list(_FAKE_CHECK_REGISTRY)
    _CREATED_TICKETS.clear()
    yield
    _CREATED_TICKETS.clear()
    _FAKE_CHECK_REGISTRY[:] = registry_snapshot


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_both_checks_registered_by_name() -> None:
    """Both checks register themselves at module import time."""
    names = {c.name for c in _FAKE_CHECK_REGISTRY}
    assert names == {"recurring_ci_failure", "multicause_ci_failure"}


def test_register_check_once_dedups_same_name() -> None:
    """Re-registering a check name replaces the previous instance."""
    _check_module._register_check_once(RecurringCIFailureCheck())
    names = [c.name for c in _FAKE_CHECK_REGISTRY]
    assert names.count("recurring_ci_failure") == 1
    assert names.count("multicause_ci_failure") == 1


# ---------------------------------------------------------------------------
# MultiCauseCIFailureCheck
# ---------------------------------------------------------------------------


class TestMultiCauseCIFailureCheck:
    """Tests for the multi-cause (distinct keys on one ticket) check."""

    def test_no_events_returns_ok(self, settings: _FakeSettings, board_id: str) -> None:
        """An empty store yields an ok result with no drafts."""
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = MultiCauseCIFailureCheck().run(ctx)
        assert result.ok is True
        assert result.drafts_created == []
        assert "no CI_FAILURE events" in result.summary

    def test_threshold_zero_disabled(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Threshold 0 disables the check even with events present."""
        settings.diagnostic_ci_multicause_threshold = 0
        for i in range(3):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", "ticket-1", "r", f"key-{i}"
            )
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = MultiCauseCIFailureCheck().run(ctx)
        assert result.ok is True
        assert result.drafts_created == []
        assert "disabled" in result.summary

    def test_missing_settings_field_defaults_to_3(
        self, tmp_path: Path, board_id: str
    ) -> None:
        """A Settings without the field (pre-mill-upgrade) trips at 3."""
        settings = _FakeSettings(tmp_path / "data")
        for i in range(3):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", "ticket-1", "r", f"key-{i}"
            )
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = MultiCauseCIFailureCheck().run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 1
        assert "3 distinct failure causes" in result.drafts_created[0]["title"]

    def test_below_threshold_no_drafts(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Two distinct causes on one ticket stay below the threshold."""
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-1", "r", "key-1"
        )
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-1", "r", "key-2"
        )
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = MultiCauseCIFailureCheck().run(ctx)
        assert result.ok is True
        assert result.drafts_created == []
        assert "none reached threshold" in result.summary

    def test_repeated_same_key_counts_once(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """The store dedups (ticket, key) pairs, so repeats never inflate."""
        emitted = [
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", "ticket-1", "r", "key-1"
            )
            for _ in range(5)
        ]
        assert emitted[0] is True
        assert emitted[1:] == [False] * 4  # store-level dedup
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = MultiCauseCIFailureCheck().run(ctx)
        assert result.ok is True
        assert result.drafts_created == []

    def test_three_distinct_causes_file_hardening_draft(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Three distinct causes on one ticket file one hardening draft."""
        reasons = ["pre-existing failures", "ruff violations", "unknown"]
        for i, reason in enumerate(reasons):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", "ticket-1", reason, f"key-{i}"
            )
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = MultiCauseCIFailureCheck().run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 1
        title = result.drafts_created[0]["title"]
        assert title == (
            "[diagnostic] flaky CI on ticket ticket-1: 3 distinct failure causes"
        )
        assert "1 hardening draft(s) filed" in result.summary

        assert len(_CREATED_TICKETS) == 1
        ticket = _CREATED_TICKETS[0]
        assert ticket.title == title
        assert ticket.source == "agent"
        assert ticket.kind == "task"
        # Body carries board, ticket, each key, and the latest reason.
        assert f"- **Repository / board:** `{board_id}`" in ticket.body
        assert "- **Source ticket:** `ticket-1`" in ticket.body
        for i in range(3):
            assert f"- `key-{i}`" in ticket.body
        assert "unknown" in ticket.body  # latest reason wins
        assert "### Action" in ticket.body

    def test_counts_are_per_ticket(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Only the ticket at threshold files; the other does not."""
        for i in range(3):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", "ticket-1", "r", f"a-{i}"
            )
        for i in range(2):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", "ticket-2", "r", f"b-{i}"
            )
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = MultiCauseCIFailureCheck().run(ctx)
        assert len(result.drafts_created) == 1
        assert (
            "ticket ticket-1: 3 distinct failure causes"
            in result.drafts_created[0]["title"]
        )

    def test_non_ci_events_ignored(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Only CI_FAILURE events count toward the distinct-cause tally."""
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-1", "r", "key-1"
        )
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-1", "r", "key-2"
        )
        emit_diagnostic_event(settings, board_id, "OTHER", "ticket-1", "r", "key-3")
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = MultiCauseCIFailureCheck().run(ctx)
        assert result.ok is True
        assert result.drafts_created == []

    def test_open_duplicate_title_skipped(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """An existing open ticket with the same title suppresses refiling."""
        for i in range(3):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", "ticket-1", "r", f"key-{i}"
            )
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-EXISTING",
                "[diagnostic] flaky CI on ticket ticket-1: 3 distinct failure causes",
                state="draft",
            )
        )
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = MultiCauseCIFailureCheck().run(ctx)
        assert result.ok is True
        assert result.drafts_created == []
        assert len(_CREATED_TICKETS) == 1  # nothing new filed

    def test_terminal_duplicate_does_not_dedupe(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """A closed ticket with the same title does not suppress refiling."""
        for i in range(3):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", "ticket-1", "r", f"key-{i}"
            )
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-EXISTING",
                "[diagnostic] flaky CI on ticket ticket-1: 3 distinct failure causes",
                state="closed",
            )
        )
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = MultiCauseCIFailureCheck().run(ctx)
        assert len(result.drafts_created) == 1
        assert len(_CREATED_TICKETS) == 2

    def test_check_exception_reports_failure(
        self, settings: _FakeSettings, board_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception inside the check yields a failed result, not a crash."""

        def boom(*args: Any, **kwargs: Any) -> list[Any]:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(_check_module, "list_diagnostic_events", boom)
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = MultiCauseCIFailureCheck().run(ctx)
        assert result.ok is False
        assert result.drafts_created == []
        assert "raised an exception" in result.summary


# ---------------------------------------------------------------------------
# RecurringCIFailureCheck (vendored regression coverage)
# ---------------------------------------------------------------------------


class TestRecurringCIFailureCheck:
    """Tests for the vendored recurring (same key across tickets) check."""

    def test_same_key_across_tickets_files_draft(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """The same key on 3 distinct tickets files a fix-proposal draft."""
        for i in range(3):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", f"ticket-{i}", "r", "key-1"
            )
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = RecurringCIFailureCheck().run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 1
        title = result.drafts_created[0]["title"]
        assert title.startswith("[diagnostic] recurring CI failure: key=")
        assert "(3 tickets)" in title

    def test_below_threshold_no_drafts(
        self, settings: _FakeSettings, board_id: str
    ) -> None:
        """Two tickets sharing a key stay below the threshold of 3."""
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-1", "r", "key-1"
        )
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-2", "r", "key-1"
        )
        ctx = _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)
        result = RecurringCIFailureCheck().run(ctx)
        assert result.ok is True
        assert result.drafts_created == []
        assert "none reached threshold" in result.summary

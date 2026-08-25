"""Tests for the shadow ``diagnostic_check_oversized_ticket`` module.

Covers the :class:`OversizedTicketCheck` — detection of tickets with
repeated implement cycles and promotion to EPIC for splitting.

Uses an importlib-based fake-module harness to load the shadow modules
directly from their source files, because the ``robotsix_mill`` shadow
package's ``__init__.py`` requires the real ``robotsix_mill`` to be
installed, and its sibling mill modules (core, config, service) do not
exist in this checkout.
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
    """Minimal stand-in for ``robotsix_mill.config.Settings``."""

    def __init__(self, root: Path, **overrides: Any) -> None:
        self._root = root
        for name, value in overrides.items():
            setattr(self, name, value)

    def diagnostic_events_file_for(self, board_id: str) -> Path:
        return self._root / board_id / "diagnostic_events.jsonl"


class _FakeTicket:
    """A ticket as returned/created by the fake service."""

    def __init__(
        self,
        ticket_id: str,
        title: str,
        state: str = "draft",
        kind: Any = None,
        source: Any = None,
        body: str = "",
        parent_id: str | None = None,
        implement_cycles: int = 0,
    ) -> None:
        self.id = ticket_id
        self.title = title
        self.state = state
        self.kind = kind
        self.source = source
        self.body = body
        self.parent_id = parent_id
        self.implement_cycles = implement_cycles


# Module-level mutable state for the fake ticket store.
_CREATED_TICKETS: list[_FakeTicket] = []
_EPIC_PROMOTIONS: list[str] = []
_ADDED_COMMENTS: list[dict[str, Any]] = []


class _FakeTicketService:
    """Record created tickets; ``list`` serves the shared ticket list."""

    def __init__(self, settings: Any = None, board_id: str = "") -> None:
        self.settings = settings
        self.board_id = board_id

    def list(
        self,
        state: Any = None,
        exclude_states: Any = None,
        **kwargs: Any,
    ) -> list[_FakeTicket]:
        result = list(_CREATED_TICKETS)
        if exclude_states is not None:
            result = [t for t in result if t.state not in exclude_states]
        return result

    def list_children(self, ticket_id: str) -> list[_FakeTicket]:
        return [t for t in _CREATED_TICKETS if t.parent_id == ticket_id]

    def create(
        self,
        title: str,
        body: str = "",
        source: Any = None,
        kind: Any = None,
        parent_id: str | None = None,
        **kwargs: Any,
    ) -> _FakeTicket:
        ticket = _FakeTicket(
            f"T-{len(_CREATED_TICKETS) + 1}",
            title,
            source=source,
            kind=kind,
            body=body,
            parent_id=parent_id,
        )
        _CREATED_TICKETS.append(ticket)
        return ticket

    def promote_to_epic(self, ticket_id: str) -> None:
        _EPIC_PROMOTIONS.append(ticket_id)
        for t in _CREATED_TICKETS:
            if t.id == ticket_id:
                t.kind = "epic"
                break

    def add_comment(
        self,
        ticket_id: str,
        body: str,
        author: str = "user",
        **kwargs: Any,
    ) -> Any:
        _ADDED_COMMENTS.append({"ticket_id": ticket_id, "body": body, "author": author})
        return SimpleNamespace(id=999, ticket_id=ticket_id, body=body)


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


# ---------------------------------------------------------------------------
# Stub modules
# ---------------------------------------------------------------------------

_fake_diagnostic_checks = SimpleNamespace(
    DIAGNOSTIC_CHECKS=_FAKE_CHECK_REGISTRY,
    DiagnosticCheck=Any,
    DiagnosticCheckContext=_FakeDiagnosticCheckContext,
    DiagnosticCheckResult=_FakeDiagnosticCheckResult,
    register_check=_fake_register_check,
)


class _FakeTicketKind:
    """Minimal stand-in for ``TicketKind`` enum."""

    TASK = "task"
    EPIC = "epic"
    INQUIRY = "inquiry"


_fake_core_models = SimpleNamespace(
    SourceKind=SimpleNamespace(AGENT="agent"),
    TicketKind=_FakeTicketKind,
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


_check_module = _load_shadow_module(
    "agents/runners/diagnostic_check_oversized_ticket.py",
    "robotsix_mill.agents.runners.diagnostic_check_oversized_ticket",
    "robotsix_mill.agents.runners",
)

OversizedTicketCheck = _check_module.OversizedTicketCheck


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """Reset mutable module-level state between tests."""
    _CREATED_TICKETS.clear()
    _EPIC_PROMOTIONS.clear()
    _ADDED_COMMENTS.clear()
    _FAKE_CHECK_REGISTRY.clear()
    yield
    _CREATED_TICKETS.clear()
    _EPIC_PROMOTIONS.clear()
    _ADDED_COMMENTS.clear()
    _FAKE_CHECK_REGISTRY.clear()


@pytest.fixture()
def _check() -> OversizedTicketCheck:
    return OversizedTicketCheck()


@pytest.fixture()
def _settings(tmp_path: Path) -> _FakeSettings:
    return _FakeSettings(tmp_path)


def _make_ctx(
    settings: _FakeSettings,
    board_id: str = "test-board",
) -> _FakeDiagnosticCheckContext:
    return _FakeDiagnosticCheckContext(board_id=board_id, settings=settings)


# ---------------------------------------------------------------------------
# Tests — registration
# ---------------------------------------------------------------------------


class TestRegistration:
    """Verify the check registers correctly."""

    def test_check_name(self) -> None:
        """Check name matches the expected identifier."""
        assert OversizedTicketCheck.name == "oversized_ticket"


# ---------------------------------------------------------------------------
# Tests — no tickets to process
# ---------------------------------------------------------------------------


class TestNoOversizedTickets:
    """When no tickets are oversized, the check should report ok=True."""

    def test_empty_board(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """Empty board yields ok result with no drafts."""
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)
        assert result.ok is True
        assert result.name == "oversized_ticket"
        assert "none with implement_cycles" in result.summary

    def test_below_threshold(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """Tickets below the threshold should not be flagged."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Some task",
                state="ready",
                kind="task",
                implement_cycles=1,
            )
        )
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 0
        assert _EPIC_PROMOTIONS == []

    def test_threshold_zero_disables(
        self, _check: OversizedTicketCheck, tmp_path: Path
    ) -> None:
        """Threshold of zero disables the check entirely."""
        settings = _FakeSettings(tmp_path, diagnostic_oversized_ticket_threshold=0)
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Big task",
                state="ready",
                kind="task",
                implement_cycles=5,
            )
        )
        ctx = _make_ctx(settings)
        result = _check.run(ctx)
        assert result.ok is True
        assert "disabled" in result.summary


# ---------------------------------------------------------------------------
# Tests — detection and promotion
# ---------------------------------------------------------------------------


class TestPromotionBehavior:
    """Verify tickets at/above threshold get promoted to EPIC."""

    def test_oversized_ticket_promoted(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """Ticket at or above threshold gets promoted and commented on."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Config cleanup",
                state="ready",
                kind="task",
                implement_cycles=3,
            )
        )
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)

        assert result.ok is True
        assert len(result.drafts_created) == 1
        assert result.drafts_created[0]["id"] == "T-1"

        # Ticket should have been promoted.
        assert "T-1" in _EPIC_PROMOTIONS

        # A diagnostic comment should have been posted.
        assert len(_ADDED_COMMENTS) == 1
        assert _ADDED_COMMENTS[0]["ticket_id"] == "T-1"
        assert "promoted to EPIC" in _ADDED_COMMENTS[0]["body"]
        assert "Implement cycles" in _ADDED_COMMENTS[0]["body"]

    def test_at_exact_threshold(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """Ticket at exactly the default threshold (2) should be promoted."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Big task",
                state="ready",
                kind="task",
                implement_cycles=2,
            )
        )
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 1
        assert "T-1" in _EPIC_PROMOTIONS

    def test_custom_threshold(
        self, _check: OversizedTicketCheck, tmp_path: Path
    ) -> None:
        """Custom threshold from settings is respected."""
        settings = _FakeSettings(tmp_path, diagnostic_oversized_ticket_threshold=5)
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Huge task",
                state="ready",
                kind="task",
                implement_cycles=3,
            )
        )
        ctx = _make_ctx(settings)
        result = _check.run(ctx)
        # 3 < 5, so should NOT be promoted.
        assert result.ok is True
        assert len(result.drafts_created) == 0
        assert _EPIC_PROMOTIONS == []


# ---------------------------------------------------------------------------
# Tests — safety guards (skip conditions)
# ---------------------------------------------------------------------------


class TestSkipConditions:
    """Verify tickets are skipped when they should be."""

    def test_already_epic_skipped(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """Tickets already promoted to EPIC are skipped."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Already epic",
                state="epic_open",
                kind="epic",
                implement_cycles=5,
            )
        )
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 0
        assert _EPIC_PROMOTIONS == []

    def test_has_children_skipped(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """Tickets with existing children are skipped."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Parent task",
                state="ready",
                kind="task",
                implement_cycles=5,
            )
        )
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-2",
                "Child task",
                state="ready",
                kind="task",
                parent_id="T-1",
            )
        )
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 0
        assert _EPIC_PROMOTIONS == []

    def test_has_parent_skipped(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """Ticket already under a parent should not be re-promoted."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Oversized child",
                state="ready",
                kind="task",
                implement_cycles=5,
                parent_id="T-0",
            )
        )
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 0

    def test_done_ticket_skipped(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """Done tickets are excluded from detection."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Done task",
                state="done",
                kind="task",
                implement_cycles=5,
            )
        )
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 0

    def test_closed_ticket_skipped(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """Closed tickets are excluded from detection."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Closed task",
                state="closed",
                kind="task",
                implement_cycles=5,
            )
        )
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 0

    def test_existing_diagnostic_epic_skipped(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """If a diagnostic EPIC already exists for this ticket, skip."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Big task",
                state="ready",
                kind="task",
                implement_cycles=5,
            )
        )
        # Simulate an existing diagnostic EPIC.
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-2",
                "[diagnostic] oversized ticket: Big task",
                state="epic_open",
                kind="epic",
            )
        )
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 0
        assert _EPIC_PROMOTIONS == []


# ---------------------------------------------------------------------------
# Tests — multiple tickets
# ---------------------------------------------------------------------------


class TestMultipleTickets:
    """Verify batch behavior with multiple oversized tickets."""

    def test_multiple_oversized_tickets(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """Multiple oversized tickets are all promoted."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Task A",
                state="ready",
                kind="task",
                implement_cycles=4,
            )
        )
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-2",
                "Task B",
                state="ready",
                kind="task",
                implement_cycles=3,
            )
        )
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-3",
                "Task C",
                state="ready",
                kind="task",
                implement_cycles=1,
            )
        )
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)

        assert result.ok is True
        assert len(result.drafts_created) == 2
        assert set(t["id"] for t in result.drafts_created) == {"T-1", "T-2"}
        assert set(_EPIC_PROMOTIONS) == {"T-1", "T-2"}
        assert len(_ADDED_COMMENTS) == 2

    def test_mixed_states(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """Only active non-terminal tickets should be considered."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Active",
                state="ready",
                kind="task",
                implement_cycles=5,
            )
        )
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-2",
                "Done",
                state="done",
                kind="task",
                implement_cycles=5,
            )
        )
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-3",
                "Closed",
                state="closed",
                kind="task",
                implement_cycles=5,
            )
        )
        ctx = _make_ctx(_settings)
        result = _check.run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 1
        assert result.drafts_created[0]["id"] == "T-1"


# ---------------------------------------------------------------------------
# Tests — error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Verify graceful handling of errors during promotion."""

    def test_promote_failure_does_not_crash(
        self, _check: OversizedTicketCheck, _settings: _FakeSettings
    ) -> None:
        """A promote_to_epic failure should be logged but not crash the check."""
        _CREATED_TICKETS.append(
            _FakeTicket(
                "T-1",
                "Task",
                state="ready",
                kind="task",
                implement_cycles=3,
            )
        )

        original_promote = _FakeTicketService.promote_to_epic

        def failing_promote(self: Any, ticket_id: str) -> None:
            raise RuntimeError("DB locked")

        _FakeTicketService.promote_to_epic = failing_promote  # type: ignore[assignment]
        try:
            ctx = _make_ctx(_settings)
            result = _check.run(ctx)
            # The check should still report ok (it catches per-ticket errors).
            assert result.ok is True
            assert len(result.drafts_created) == 0
        finally:
            _FakeTicketService.promote_to_epic = original_promote  # type: ignore[assignment]

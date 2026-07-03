"""Tests for the systemic fix surfacing module.

Covers:
- FixProposal dataclass
- FixProposalStore persistence and CRUD
- RecurrenceDetector threshold logic
- FixSurfacer template generation
- Agent tools: list/apply/reject flow
- Configurable threshold via DiagnosticsSettings
"""

# mypy: disable_error_code = "no-untyped-def"
# ruff: noqa: D102
from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from robotsix_chat.config import DiagnosticsSettings
from robotsix_chat.diagnostics import (
    FixProposalStore,
    FixSurfacer,
    RecurrenceDetector,
    build_diagnostics_tools,
)
from robotsix_chat.diagnostics.fixes import _CATEGORY_FIX_TEMPLATES, FixProposal
from robotsix_chat.diagnostics.store import DiagnosticStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_clock(iso: str) -> Callable[[], datetime]:
    """Return a clock callable pinned to *iso*."""
    dt = datetime.fromisoformat(iso)
    return lambda: dt


def _wipe_env_vars(monkeypatch) -> None:
    """Remove all DIAGNOSTICS_* env vars so tests start clean."""
    for key in list(os.environ):
        if key.startswith("DIAGNOSTICS_"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# FixProposal dataclass
# ---------------------------------------------------------------------------


class TestFixProposal:
    """FixProposal dataclass field tests."""

    def test_defaults(self) -> None:
        p = FixProposal(
            id="abc",
            category="CI_FAILURE",
            description="test",
            suggested_fix="fix",
        )
        assert p.id == "abc"
        assert p.category == "CI_FAILURE"
        assert p.status == "proposed"
        assert p.created_at == ""
        assert p.applied_at is None

    def test_all_fields_explicit(self):
        p = FixProposal(
            id="xyz",
            category="CLONE_TARGET",
            description="Recurring clone failures",
            suggested_fix="Add validation",
            status="applied",
            created_at="2025-01-01T00:00:00",
            applied_at="2025-01-02T00:00:00",
        )
        assert p.id == "xyz"
        assert p.status == "applied"
        assert p.applied_at == "2025-01-02T00:00:00"


# ---------------------------------------------------------------------------
# FixProposalStore
# ---------------------------------------------------------------------------


class TestFixProposalStore:
    """FixProposalStore CRUD and persistence tests."""

    def test_add_and_get(self, tmp_path) -> None:
        store = FixProposalStore(
            tmp_path / "proposals.json",
            clock=_fake_clock("2025-06-01T12:00:00+00:00"),
        )
        p = store.add_proposal(
            category="CI_FAILURE",
            description="desc",
            suggested_fix="fix",
        )
        assert p.id
        assert p.category == "CI_FAILURE"
        assert p.status == "proposed"
        assert p.created_at == "2025-06-01T12:00:00+00:00"

        got = store.get_proposal(p.id)
        assert got is not None
        assert got.id == p.id

    def test_get_unknown(self, tmp_path) -> None:
        store = FixProposalStore(tmp_path / "proposals.json")
        assert store.get_proposal("nonexistent") is None

    def test_list_all(self, tmp_path) -> None:
        store = FixProposalStore(tmp_path / "proposals.json")
        p1 = store.add_proposal("CI_FAILURE", "d1", "f1")
        p2 = store.add_proposal("CLONE_TARGET", "d2", "f2")
        all_p = store.list_proposals()
        assert len(all_p) == 2
        ids = {p.id for p in all_p}
        assert p1.id in ids
        assert p2.id in ids

    def test_list_filtered(self, tmp_path) -> None:
        store = FixProposalStore(tmp_path / "proposals.json")
        store.add_proposal("CI_FAILURE", "d1", "f1")
        store.add_proposal("CLONE_TARGET", "d2", "f2")
        store.add_proposal("ci_failure", "d3", "f3")  # case-insensitive

        ci = store.list_proposals("CI_FAILURE")
        assert len(ci) == 2

    def test_apply(self, tmp_path) -> None:
        store = FixProposalStore(
            tmp_path / "proposals.json",
            clock=_fake_clock("2025-06-02T12:00:00+00:00"),
        )
        p = store.add_proposal("REFINEMENT", "d", "f")
        updated = store.apply(p.id)
        assert updated is not None
        assert updated.status == "applied"
        assert updated.applied_at == "2025-06-02T12:00:00+00:00"

    def test_apply_unknown(self, tmp_path) -> None:
        store = FixProposalStore(tmp_path / "proposals.json")
        assert store.apply("nonexistent") is None

    def test_reject(self, tmp_path) -> None:
        store = FixProposalStore(tmp_path / "proposals.json")
        p = store.add_proposal("DEPENDENCY", "d", "f")
        updated = store.reject(p.id)
        assert updated is not None
        assert updated.status == "rejected"

    def test_reject_unknown(self, tmp_path) -> None:
        store = FixProposalStore(tmp_path / "proposals.json")
        assert store.reject("nonexistent") is None

    def test_persistence_round_trip(self, tmp_path) -> None:
        path = tmp_path / "proposals.json"
        store1 = FixProposalStore(path)
        p = store1.add_proposal("CI_FAILURE", "desc", "fix")
        store1.apply(p.id)

        store2 = FixProposalStore(path)
        got = store2.get_proposal(p.id)
        assert got is not None
        assert got.category == "CI_FAILURE"
        assert got.status == "applied"

    def test_missing_file(self, tmp_path) -> None:
        path = tmp_path / "nonexistent.json"
        store = FixProposalStore(path)
        assert store.list_proposals() == []

    def test_corrupt_file(self, tmp_path) -> None:
        path = tmp_path / "corrupt.json"
        path.write_text("not json")
        store = FixProposalStore(path)
        assert store.list_proposals() == []

    def test_empty_file(self, tmp_path) -> None:
        path = tmp_path / "empty.json"
        path.write_text("")
        store = FixProposalStore(path)
        assert store.list_proposals() == []


# ---------------------------------------------------------------------------
# RecurrenceDetector
# ---------------------------------------------------------------------------


class TestRecurrenceDetector:
    """RecurrenceDetector threshold logic tests."""

    @staticmethod
    def _build_store(clock: Callable[[], datetime] | None = None) -> DiagnosticStore:
        return DiagnosticStore(":memory:", clock=clock)

    def test_no_events(self, tmp_path) -> None:
        store = DiagnosticStore(tmp_path / "diag.json")
        detector = RecurrenceDetector(store)
        assert detector.find_recurring() == {}

    def test_below_threshold(self, tmp_path) -> None:
        now = datetime(2025, 6, 15, tzinfo=UTC)

        def clock() -> datetime:
            return now

        store = DiagnosticStore(
            tmp_path / "diag.json",
            clock=clock,
        )
        store.record_event("CI_FAILURE", "fail 1")
        store.record_event("CI_FAILURE", "fail 2")

        detector = RecurrenceDetector(store, threshold=3, clock=clock)
        assert detector.find_recurring() == {}

    def test_at_threshold(self, tmp_path) -> None:
        now = datetime(2025, 6, 15, tzinfo=UTC)

        def clock() -> datetime:
            return now

        store = DiagnosticStore(
            tmp_path / "diag.json",
            clock=clock,
        )
        store.record_event("CI_FAILURE", "fail 1")
        store.record_event("CI_FAILURE", "fail 2")
        store.record_event("CI_FAILURE", "fail 3")

        detector = RecurrenceDetector(store, threshold=3, clock=clock)
        result = detector.find_recurring()
        assert result == {"CI_FAILURE": 3}

    def test_above_threshold(self, tmp_path) -> None:
        now = datetime(2025, 6, 15, tzinfo=UTC)

        def clock() -> datetime:
            return now

        store = DiagnosticStore(
            tmp_path / "diag.json",
            clock=clock,
        )
        for _ in range(5):
            store.record_event("DEPENDENCY", "fail")

        detector = RecurrenceDetector(store, threshold=3, clock=clock)
        result = detector.find_recurring()
        assert result == {"DEPENDENCY": 5}

    def test_multiple_categories(self, tmp_path) -> None:
        now = datetime(2025, 6, 15, tzinfo=UTC)

        def clock() -> datetime:
            return now

        store = DiagnosticStore(
            tmp_path / "diag.json",
            clock=clock,
        )
        for _ in range(4):
            store.record_event("CI_FAILURE", "ci")
        for _ in range(3):
            store.record_event("CLONE_TARGET", "clone")
        store.record_event("REFINEMENT", "refine")  # only 1

        detector = RecurrenceDetector(store, threshold=3, clock=clock)
        result = detector.find_recurring()
        assert result == {"CI_FAILURE": 4, "CLONE_TARGET": 3}

    def test_window_filters_old_events(self, tmp_path) -> None:
        now = datetime(2025, 6, 15, tzinfo=UTC)

        def clock() -> datetime:
            return now

        store = DiagnosticStore(
            tmp_path / "diag.json",
            clock=_fake_clock("2024-01-01T00:00:00+00:00"),
        )
        # Record old events with the pinned old clock
        for _ in range(5):
            store.record_event("CI_FAILURE", "old fail")

        # Now use a detector with a "now" clock — old events should be
        # outside the 30-day window.
        detector = RecurrenceDetector(store, threshold=3, clock=clock)
        assert detector.find_recurring() == {}

    def test_window_includes_recent(self, tmp_path) -> None:
        now = datetime(2025, 6, 15, tzinfo=UTC)

        def clock() -> datetime:
            return now

        store = DiagnosticStore(
            tmp_path / "diag.json",
            clock=clock,
        )
        store.record_event("CI_FAILURE", "recent 1")
        store.record_event("CI_FAILURE", "recent 2")
        store.record_event("CI_FAILURE", "recent 3")

        # Also add an old event
        old_clock_store = DiagnosticStore(
            tmp_path / "diag2.json",
            clock=_fake_clock("2024-01-01T00:00:00+00:00"),
        )
        old_clock_store.record_event("CI_FAILURE", "old")

        detector = RecurrenceDetector(store, threshold=2, clock=clock)
        result = detector.find_recurring()
        assert result == {"CI_FAILURE": 3}


# ---------------------------------------------------------------------------
# FixSurfacer
# ---------------------------------------------------------------------------


class TestFixSurfacer:
    """FixSurfacer template generation tests."""

    def test_surface_with_known_template(self, tmp_path) -> None:
        ps = FixProposalStore(tmp_path / "proposals.json")
        surfacer = FixSurfacer(ps)
        proposal = surfacer.surface_fix("CI_FAILURE", 5)
        assert proposal.category == "CI_FAILURE"
        assert "5 times" in proposal.description
        assert proposal.suggested_fix == _CATEGORY_FIX_TEMPLATES["CI_FAILURE"]
        assert proposal.status == "proposed"

    def test_surface_unknown_category_fallback(self, tmp_path) -> None:
        ps = FixProposalStore(tmp_path / "proposals.json")
        surfacer = FixSurfacer(ps)
        proposal = surfacer.surface_fix("MYSTERY", 10)
        assert proposal.category == "MYSTERY"
        assert "Investigate systemic cause" in proposal.suggested_fix

    def test_custom_templates(self, tmp_path) -> None:
        ps = FixProposalStore(tmp_path / "proposals.json")
        custom = {"CUSTOM_CAT": "Do the custom thing"}
        surfacer = FixSurfacer(ps, templates=custom)
        proposal = surfacer.surface_fix("CUSTOM_CAT", 3)
        assert proposal.suggested_fix == "Do the custom thing"

    def test_all_four_non_other_templates_present(self) -> None:
        """Verify the curated mapping covers all 4 non-OTHER categories."""
        assert "CLONE_TARGET" in _CATEGORY_FIX_TEMPLATES
        assert "CI_FAILURE" in _CATEGORY_FIX_TEMPLATES
        assert "DEPENDENCY" in _CATEGORY_FIX_TEMPLATES
        assert "REFINEMENT" in _CATEGORY_FIX_TEMPLATES
        assert len(_CATEGORY_FIX_TEMPLATES) == 4


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------


class TestBuildDiagnosticsTools:
    """Agent tool factory tests."""

    def test_disabled_returns_empty(self) -> None:
        settings = DiagnosticsSettings(enabled=False)
        tools = build_diagnostics_tools(settings)
        assert tools == []

    def test_enabled_returns_six_tools(self, tmp_path) -> None:
        settings = DiagnosticsSettings(
            enabled=True,
            store_path=str(tmp_path / "diag.json"),
            proposals_path=str(tmp_path / "prop.json"),
            effectiveness_path=str(tmp_path / "eff.json"),
        )
        tools = build_diagnostics_tools(settings)
        assert len(tools) == 6

    @pytest.mark.anyio
    async def test_list_diagnostic_events(self, tmp_path) -> None:
        settings = DiagnosticsSettings(
            enabled=True,
            store_path=str(tmp_path / "diag.json"),
            proposals_path=str(tmp_path / "prop.json"),
        )
        tools = build_diagnostics_tools(settings)
        list_events = tools[0]

        result = await list_events()
        assert "No diagnostic events found" in result

    @pytest.mark.anyio
    async def test_list_fix_proposals_empty(self, tmp_path) -> None:
        settings = DiagnosticsSettings(
            enabled=True,
            store_path=str(tmp_path / "diag.json"),
            proposals_path=str(tmp_path / "prop.json"),
        )
        tools = build_diagnostics_tools(settings)
        list_proposals = tools[2]

        result = await list_proposals()
        assert "No fix proposals found" in result

    @pytest.mark.anyio
    async def test_apply_fix_unknown(self, tmp_path) -> None:
        settings = DiagnosticsSettings(
            enabled=True,
            store_path=str(tmp_path / "diag.json"),
            proposals_path=str(tmp_path / "prop.json"),
        )
        tools = build_diagnostics_tools(settings)
        apply_fn = tools[3]

        result = await apply_fn("nonexistent")
        assert "no fix proposal found" in result

    @pytest.mark.anyio
    async def test_reject_fix_unknown(self, tmp_path) -> None:
        settings = DiagnosticsSettings(
            enabled=True,
            store_path=str(tmp_path / "diag.json"),
            proposals_path=str(tmp_path / "prop.json"),
        )
        tools = build_diagnostics_tools(settings)
        reject_fn = tools[4]

        result = await reject_fn("nonexistent")
        assert "no fix proposal found" in result

    @pytest.mark.anyio
    async def test_list_apply_reject_flow(self, tmp_path) -> None:
        settings = DiagnosticsSettings(
            enabled=True,
            store_path=str(tmp_path / "diag.json"),
            proposals_path=str(tmp_path / "prop.json"),
            recurrence_window_days=365 * 10,  # wide window for pinned timestamps
        )

        # Add events to trigger recurrence using a recent timestamp
        now = datetime.now(UTC)

        def recent_clock() -> datetime:
            return now

        store = DiagnosticStore(
            tmp_path / "diag.json",
            clock=recent_clock,
        )
        for _ in range(3):
            store.record_event("CI_FAILURE", "test ci failure")

        # build_diagnostics_tools creates its own store — we need to use
        # the same path.  Rebuild tools to pick up persisted events.
        tools2 = build_diagnostics_tools(settings)
        check_fn2 = tools2[1]
        list_fn2 = tools2[2]
        apply_fn2 = tools2[3]
        reject_fn2 = tools2[4]

        result = await check_fn2()
        assert "Recurring categories detected" in result
        assert "CI_FAILURE" in result

        # Now list proposals
        list_result = await list_fn2()
        assert "CI_FAILURE" in list_result
        assert "proposed" in list_result

        # Extract proposal id
        lines = list_result.split("\n")
        id_line = lines[0]
        proposal_id = id_line[1:].split("]")[0]  # "[<id>]"

        # Apply it
        apply_result = await apply_fn2(proposal_id)
        assert "Applied fix proposal" in apply_result

        # Verify rejection works on unknown ids (proposal was already applied)
        reject_result = await reject_fn2("nonexistent")
        assert "no fix proposal found" in reject_result

    @pytest.mark.anyio
    async def test_check_recurring_no_recurrence(self, tmp_path) -> None:
        settings = DiagnosticsSettings(
            enabled=True,
            store_path=str(tmp_path / "diag.json"),
            proposals_path=str(tmp_path / "prop.json"),
        )
        tools = build_diagnostics_tools(settings)
        check_fn = tools[1]
        result = await check_fn()
        assert "No categories have recurred" in result


# ---------------------------------------------------------------------------
# DiagnosticsSettings config
# ---------------------------------------------------------------------------


class TestDiagnosticsSettings:
    """Configuration model tests."""

    def test_defaults(self) -> None:
        s = DiagnosticsSettings()
        assert s.enabled is True
        assert s.store_path == "/data/diagnostics.json"
        assert s.proposals_path == "/data/fix_proposals.json"
        assert s.recurrence_threshold == 3
        assert s.recurrence_window_days == 30

    def test_from_env(self, monkeypatch) -> None:
        _wipe_env_vars(monkeypatch)
        monkeypatch.setenv("DIAGNOSTICS_ENABLED", "false")
        monkeypatch.setenv("DIAGNOSTICS_RECURRENCE_THRESHOLD", "5")
        monkeypatch.setenv("DIAGNOSTICS_RECURRENCE_WINDOW_DAYS", "14")
        monkeypatch.setenv("DIAGNOSTICS_STORE_PATH", "/fake/test/diag.json")
        monkeypatch.setenv("DIAGNOSTICS_PROPOSALS_PATH", "/fake/test/prop.json")

        # Drive through _build_diagnostics_raw
        from robotsix_chat.config import _build_diagnostics_raw

        raw = _build_diagnostics_raw({})
        s = DiagnosticsSettings(**raw)
        assert s.enabled is False
        assert s.recurrence_threshold == 5
        assert s.recurrence_window_days == 14
        assert s.store_path == "/fake/test/diag.json"
        assert s.proposals_path == "/fake/test/prop.json"

    def test_env_threshold_invalid(self, monkeypatch) -> None:
        _wipe_env_vars(monkeypatch)
        monkeypatch.setenv("DIAGNOSTICS_RECURRENCE_THRESHOLD", "not_a_number")
        from robotsix_chat.config import _build_diagnostics_raw

        with pytest.raises(ValueError, match="DIAGNOSTICS_RECURRENCE_THRESHOLD"):
            _build_diagnostics_raw({})

    def test_env_window_invalid(self, monkeypatch) -> None:
        _wipe_env_vars(monkeypatch)
        monkeypatch.setenv("DIAGNOSTICS_RECURRENCE_WINDOW_DAYS", "abc")
        from robotsix_chat.config import _build_diagnostics_raw

        with pytest.raises(ValueError, match="DIAGNOSTICS_RECURRENCE_WINDOW_DAYS"):
            _build_diagnostics_raw({})


# ---------------------------------------------------------------------------
# DiagnosticStore (minimal integration tests)
# ---------------------------------------------------------------------------


class TestDiagnosticStore:
    """DiagnosticStore basic operation tests needed by fixes module."""

    def test_record_and_list(self, tmp_path) -> None:
        store = DiagnosticStore(
            tmp_path / "diag.json",
            clock=_fake_clock("2025-06-01T12:00:00+00:00"),
        )
        b = store.record_event("CI_FAILURE", "test message", {"key": "val"})
        assert b.id
        assert b.category == "CI_FAILURE"
        assert b.created_at == "2025-06-01T12:00:00+00:00"
        assert b.details == {"key": "val"}

        events = store.list_events()
        assert len(events) == 1

    def test_list_filtered(self, tmp_path) -> None:
        store = DiagnosticStore(tmp_path / "diag.json")
        store.record_event("CI_FAILURE", "ci")
        store.record_event("CLONE_TARGET", "clone")
        ci = store.list_events("CI_FAILURE")
        assert len(ci) == 1
        assert ci[0].category == "CI_FAILURE"

    def test_events_since(self, tmp_path) -> None:
        now = datetime(2025, 6, 15, tzinfo=UTC)

        def clock() -> datetime:
            return now

        store = DiagnosticStore(
            tmp_path / "diag.json",
            clock=clock,
        )
        store.record_event("CI_FAILURE", "recent")

        # Events since 1 day ago
        cutoff = now - timedelta(days=1)
        events = store.events_since(cutoff)
        assert len(events) == 1

        # Events since 1 day in the future — none
        future = now + timedelta(days=1)
        events = store.events_since(future)
        assert len(events) == 0

    def test_persistence(self, tmp_path) -> None:
        path = tmp_path / "diag.json"
        store1 = DiagnosticStore(path)
        store1.record_event("TEST", "msg")

        store2 = DiagnosticStore(path)
        events = store2.list_events()
        assert len(events) == 1
        assert events[0].category == "TEST"

    def test_corrupt_file(self, tmp_path) -> None:
        path = tmp_path / "corrupt.json"
        path.write_text("garbage")
        store = DiagnosticStore(path)
        assert store.list_events() == []

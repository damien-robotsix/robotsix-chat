"""Tests for fix-effectiveness verification (RecurrenceMeasurer)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from robotsix_chat.diagnostics.store import DiagnosticBundle, DiagnosticStore
from robotsix_chat.diagnostics.verification import (
    EffectivenessStore,
    FixEffectivenessReport,
    RecurrenceMeasurer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixed_clock(iso: str):
    """Return a clock callable that always returns the given datetime."""

    def _clock() -> datetime:
        return datetime.fromisoformat(iso)

    return _clock


def _add_entries(
    store: DiagnosticStore,
    category: str,
    base_dt: datetime,
    count: int,
    *,
    hours_apart: int = 1,
) -> None:
    """Add *count* diagnostic entries at *hours_apart* intervals from *base_dt*."""
    for i in range(count):
        ts = (base_dt + timedelta(hours=i * hours_apart)).isoformat()
        bundle = DiagnosticBundle(
            id=uuid.uuid4().hex,
            category=category,
            message=f"test event {i}",
            details={"test": True},
            created_at=ts,
        )
        store._items[bundle.id] = bundle
    store._persist()


# ---------------------------------------------------------------------------
# EffectivenessStore tests
# ---------------------------------------------------------------------------


def test_record_and_retrieve_fix(tmp_path: Path) -> None:
    """FixApplication can be recorded and retrieved by id."""
    path = tmp_path / "eff.json"
    store = EffectivenessStore(path)

    app = store.record_fix(
        "fix-abc",
        "auth-timeout",
        applied_at="2025-06-01T12:00:00+00:00",
    )
    assert app.fix_proposal_id == "fix-abc"
    assert app.category == "auth-timeout"
    assert app.applied_at == "2025-06-01T12:00:00+00:00"

    retrieved = store.get_fix("fix-abc")
    assert retrieved is not None
    assert retrieved.fix_proposal_id == "fix-abc"


def test_get_fix_nonexistent(tmp_path: Path) -> None:
    """get_fix returns None for unknown fix ids."""
    store = EffectivenessStore(path=tmp_path / "eff.json")
    assert store.get_fix("nonexistent") is None


def test_list_fixes(tmp_path: Path) -> None:
    """list_fixes returns all recorded fix applications."""
    store = EffectivenessStore(path=tmp_path / "eff.json")
    store.record_fix("fix-1", "cat-a", applied_at="2025-01-01T00:00:00+00:00")
    store.record_fix("fix-2", "cat-b", applied_at="2025-02-01T00:00:00+00:00")

    fixes = store.list_fixes()
    assert len(fixes) == 2
    assert {f.fix_proposal_id for f in fixes} == {"fix-1", "fix-2"}


def test_record_fix_uses_clock_when_no_applied_at(tmp_path: Path) -> None:
    """When applied_at is not given, the injected clock provides the timestamp."""
    store = EffectivenessStore(
        tmp_path / "eff.json",
        clock=_fixed_clock("2025-06-15T12:00:00+00:00"),
    )
    app = store.record_fix("fix-clock", "cat")
    assert app.applied_at == "2025-06-15T12:00:00+00:00"


def test_save_and_list_reports(tmp_path: Path) -> None:
    """Reports can be saved and listed, optionally filtered by category."""
    store = EffectivenessStore(path=tmp_path / "eff.json")

    r1 = FixEffectivenessReport(
        report_id="r1",
        fix_proposal_id="fix-1",
        category="cat-a",
        applied_at="2025-01-01T00:00:00+00:00",
        pre_fix_period=("2024-12-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
        pre_fix_count=10,
        post_fix_period=("2025-01-01T00:00:00+00:00", "2025-02-01T00:00:00+00:00"),
        post_fix_count=3,
        reduction_pct=70.0,
        effective=True,
    )
    r2 = FixEffectivenessReport(
        report_id="r2",
        fix_proposal_id="fix-2",
        category="cat-b",
        applied_at="2025-02-01T00:00:00+00:00",
        pre_fix_period=("2025-01-01T00:00:00+00:00", "2025-02-01T00:00:00+00:00"),
        pre_fix_count=5,
        post_fix_period=("2025-02-01T00:00:00+00:00", "2025-03-01T00:00:00+00:00"),
        post_fix_count=5,
        reduction_pct=0.0,
        effective=False,
    )

    store.save_report(r1)
    store.save_report(r2)

    all_reports = store.list_reports()
    assert len(all_reports) == 2

    cat_a = store.list_reports("cat-a")
    assert len(cat_a) == 1
    assert cat_a[0].report_id == "r1"

    cat_b = store.list_reports("cat-b")
    assert len(cat_b) == 1
    assert cat_b[0].report_id == "r2"

    empty = store.list_reports("nonexistent")
    assert len(empty) == 0


def test_effectiveness_store_persistence_roundtrip(tmp_path: Path) -> None:
    """Data survives a save → reload round-trip."""
    path = tmp_path / "eff.json"
    store1 = EffectivenessStore(path)

    store1.record_fix("fix-x", "cat-x", applied_at="2025-01-01T00:00:00+00:00")
    r = FixEffectivenessReport(
        report_id="rx",
        fix_proposal_id="fix-x",
        category="cat-x",
        applied_at="2025-01-01T00:00:00+00:00",
        pre_fix_period=("2024-12-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
        pre_fix_count=8,
        post_fix_period=("2025-01-01T00:00:00+00:00", "2025-02-01T00:00:00+00:00"),
        post_fix_count=2,
        reduction_pct=75.0,
        effective=True,
    )
    store1.save_report(r)

    store2 = EffectivenessStore(path)
    fixes = store2.list_fixes()
    assert len(fixes) == 1
    assert fixes[0].fix_proposal_id == "fix-x"

    reports = store2.list_reports()
    assert len(reports) == 1
    assert reports[0].report_id == "rx"
    assert reports[0].pre_fix_count == 8
    assert reports[0].post_fix_count == 2
    assert reports[0].effective is True


# ---------------------------------------------------------------------------
# RecurrenceMeasurer tests
# ---------------------------------------------------------------------------


def test_baseline_calculation_reduction(tmp_path: Path) -> None:
    """Pre-fix count > post-fix count → effective=True with positive reduction."""
    diag_store = DiagnosticStore(tmp_path / "diag.json")
    eff_store = EffectivenessStore(tmp_path / "eff.json")

    # Fix applied on June 1, 2025.
    # Pre-fix window: May 2 – June 1 (30 days)
    # Post-fix window: June 1 – July 1 (30 days)
    measurer = RecurrenceMeasurer(
        diag_store,
        eff_store,
        observation_window_days=30,
    )

    # Add 10 pre-fix events in the 30 days before June 1.
    _add_entries(
        diag_store,
        "auth-timeout",
        datetime(2025, 5, 15, tzinfo=UTC),
        10,
        hours_apart=24,
    )

    # Add 3 post-fix events in the 30 days after June 1.
    _add_entries(
        diag_store,
        "auth-timeout",
        datetime(2025, 6, 10, tzinfo=UTC),
        3,
        hours_apart=24,
    )

    measurer.apply_fix(
        "fix-auth-1",
        "auth-timeout",
        applied_at="2025-06-01T00:00:00+00:00",
    )
    report = measurer.generate_report("fix-auth-1")

    assert report is not None
    assert report.pre_fix_count == 10
    assert report.post_fix_count == 3
    assert report.reduction_pct == 70.0  # (10-3)/10 * 100
    assert report.effective is True


def test_post_fix_increase_ineffective(tmp_path: Path) -> None:
    """Post-fix count >= pre-fix count → effective=False."""
    diag_store = DiagnosticStore(tmp_path / "diag.json")
    eff_store = EffectivenessStore(tmp_path / "eff.json")

    measurer = RecurrenceMeasurer(
        diag_store,
        eff_store,
        observation_window_days=30,
    )

    # 5 pre-fix events starting May 15 (within May 2 – June 1 window).
    _add_entries(
        diag_store,
        "db-connection",
        datetime(2025, 5, 15, tzinfo=UTC),
        5,
        hours_apart=24,
    )

    # 8 post-fix events starting June 2 (worse!).
    _add_entries(
        diag_store,
        "db-connection",
        datetime(2025, 6, 2, tzinfo=UTC),
        8,
        hours_apart=24,
    )

    measurer.apply_fix(
        "fix-db-1",
        "db-connection",
        applied_at="2025-06-01T00:00:00+00:00",
    )
    report = measurer.generate_report("fix-db-1")

    assert report is not None
    assert report.pre_fix_count == 5
    assert report.post_fix_count == 8
    assert report.reduction_pct == -60.0  # (5-8)/5 * 100 = -60%
    assert report.effective is False


def test_zero_pre_fix_baseline_insufficient(tmp_path: Path) -> None:
    """Zero pre-fix events → effective=False (baseline insufficient)."""
    diag_store = DiagnosticStore(tmp_path / "diag.json")
    eff_store = EffectivenessStore(tmp_path / "eff.json")

    measurer = RecurrenceMeasurer(
        diag_store,
        eff_store,
        observation_window_days=30,
    )

    # No pre-fix events.  The fix was preemptive.
    # 2 post-fix events (still, no baseline to compare).
    _add_entries(
        diag_store,
        "new-category",
        datetime(2025, 6, 10, tzinfo=UTC),
        2,
        hours_apart=24,
    )

    measurer.apply_fix(
        "fix-preemptive",
        "new-category",
        applied_at="2025-06-01T00:00:00+00:00",
    )
    report = measurer.generate_report("fix-preemptive")

    assert report is not None
    assert report.pre_fix_count == 0
    assert report.post_fix_count == 2
    assert report.effective is False
    assert report.reduction_pct == 0.0


def test_generate_report_nonexistent_fix(tmp_path: Path) -> None:
    """generate_report returns None for unknown fix ids."""
    diag_store = DiagnosticStore(tmp_path / "diag.json")
    eff_store = EffectivenessStore(tmp_path / "eff.json")
    measurer = RecurrenceMeasurer(diag_store, eff_store)

    assert measurer.generate_report("nonexistent") is None


def test_generate_report_idempotent(tmp_path: Path) -> None:
    """Calling generate_report twice returns the same report (no double-count)."""
    diag_store = DiagnosticStore(tmp_path / "diag.json")
    eff_store = EffectivenessStore(tmp_path / "eff.json")

    measurer = RecurrenceMeasurer(
        diag_store,
        eff_store,
        observation_window_days=30,
    )

    _add_entries(
        diag_store,
        "cat",
        datetime(2025, 5, 15, tzinfo=UTC),
        5,
        hours_apart=24,
    )
    _add_entries(
        diag_store,
        "cat",
        datetime(2025, 6, 10, tzinfo=UTC),
        2,
        hours_apart=24,
    )

    measurer.apply_fix(
        "fix-idem",
        "cat",
        applied_at="2025-06-01T00:00:00+00:00",
    )
    r1 = measurer.generate_report("fix-idem")
    r2 = measurer.generate_report("fix-idem")

    assert r1 is not None
    assert r2 is not None
    assert r1.report_id == r2.report_id
    assert r1.pre_fix_count == r2.pre_fix_count


def test_generate_pending_reports_only_after_window(
    tmp_path: Path,
) -> None:
    """Reports are only created after the observation window elapses."""
    diag_store = DiagnosticStore(tmp_path / "diag.json")
    eff_store = EffectivenessStore(tmp_path / "eff.json")

    # Fix applied on June 1.  With a 30-day window, the report should only
    # be generated on or after July 1.
    _add_entries(
        diag_store,
        "cat-pending",
        datetime(2025, 5, 15, tzinfo=UTC),
        5,
        hours_apart=24,
    )
    _add_entries(
        diag_store,
        "cat-pending",
        datetime(2025, 6, 10, tzinfo=UTC),
        2,
        hours_apart=24,
    )

    measurer_early = RecurrenceMeasurer(
        diag_store,
        eff_store,
        observation_window_days=30,
        clock=_fixed_clock("2025-06-15T00:00:00+00:00"),
    )
    measurer_early.apply_fix(
        "fix-pending",
        "cat-pending",
        applied_at="2025-06-01T00:00:00+00:00",
    )
    reports_early = measurer_early.generate_pending_reports()
    assert len(reports_early) == 0  # window not elapsed (June 15 < July 1)

    # Now clock at July 2 — window HAS elapsed.
    measurer_late = RecurrenceMeasurer(
        diag_store,
        eff_store,
        observation_window_days=30,
        clock=_fixed_clock("2025-07-02T00:00:00+00:00"),
    )
    reports_late = measurer_late.generate_pending_reports()
    assert len(reports_late) == 1
    assert reports_late[0].fix_proposal_id == "fix-pending"
    assert reports_late[0].effective is True  # 5 → 2


def test_generate_pending_reports_skips_existing(tmp_path: Path) -> None:
    """generate_pending_reports does not regenerate already-existing reports."""
    diag_store = DiagnosticStore(tmp_path / "diag.json")
    eff_store = EffectivenessStore(tmp_path / "eff.json")

    measurer = RecurrenceMeasurer(
        diag_store,
        eff_store,
        observation_window_days=30,
        clock=_fixed_clock("2025-07-02T00:00:00+00:00"),
    )

    _add_entries(
        diag_store,
        "cat-skip",
        datetime(2025, 5, 15, tzinfo=UTC),
        5,
        hours_apart=24,
    )
    _add_entries(
        diag_store,
        "cat-skip",
        datetime(2025, 6, 10, tzinfo=UTC),
        1,
        hours_apart=24,
    )

    measurer.apply_fix(
        "fix-skip",
        "cat-skip",
        applied_at="2025-06-01T00:00:00+00:00",
    )

    # First call generates the report.
    reports1 = measurer.generate_pending_reports()
    assert len(reports1) == 1

    # Second call skips it.
    reports2 = measurer.generate_pending_reports()
    assert len(reports2) == 0


def test_reduction_pct_precision(tmp_path: Path) -> None:
    """reduction_pct is rounded to 1 decimal place."""
    diag_store = DiagnosticStore(tmp_path / "diag.json")
    eff_store = EffectivenessStore(tmp_path / "eff.json")

    measurer = RecurrenceMeasurer(
        diag_store,
        eff_store,
        observation_window_days=30,
    )

    # 7 pre, 3 post → reduction = (7-3)/7 * 100 = 57.1428... → 57.1
    _add_entries(
        diag_store,
        "cat",
        datetime(2025, 5, 15, tzinfo=UTC),
        7,
        hours_apart=24,
    )
    _add_entries(
        diag_store,
        "cat",
        datetime(2025, 6, 10, tzinfo=UTC),
        3,
        hours_apart=24,
    )

    measurer.apply_fix(
        "fix-prec",
        "cat",
        applied_at="2025-06-01T00:00:00+00:00",
    )
    report = measurer.generate_report("fix-prec")

    assert report is not None
    assert report.reduction_pct == 57.1


def test_equal_pre_post_counts_not_effective(tmp_path: Path) -> None:
    """Equal pre and post counts → effective=False."""
    diag_store = DiagnosticStore(tmp_path / "diag.json")
    eff_store = EffectivenessStore(tmp_path / "eff.json")

    measurer = RecurrenceMeasurer(
        diag_store,
        eff_store,
        observation_window_days=30,
    )

    _add_entries(
        diag_store,
        "cat",
        datetime(2025, 5, 15, tzinfo=UTC),
        4,
        hours_apart=24,
    )
    _add_entries(
        diag_store,
        "cat",
        datetime(2025, 6, 10, tzinfo=UTC),
        4,
        hours_apart=24,
    )

    measurer.apply_fix(
        "fix-equal",
        "cat",
        applied_at="2025-06-01T00:00:00+00:00",
    )
    report = measurer.generate_report("fix-equal")

    assert report is not None
    assert report.pre_fix_count == 4
    assert report.post_fix_count == 4
    assert report.reduction_pct == 0.0
    assert report.effective is False

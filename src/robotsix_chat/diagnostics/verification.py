"""Fix-effectiveness verification — measure post-fix recurrence vs. baseline.

:class:`EffectivenessStore` persists fix applications and
:class:`FixEffectivenessReport` instances to a JSON file.

:class:`RecurrenceMeasurer` tracks when a fix is applied and, after a
configurable observation window elapses, computes pre- and post-fix
recurrence counts against a :class:`~robotsix_chat.diagnostics.store.DiagnosticStore`
to determine whether the fix actually reduced the problem.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robotsix_chat.common.json_store import JsonStoreBase

if TYPE_CHECKING:
    from robotsix_chat.diagnostics.store import DiagnosticStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# data classes
# ---------------------------------------------------------------------------


@dataclass
class FixApplication:
    """A recorded fix application — when a fix proposal was applied.

    Attributes:
        fix_proposal_id: The id of the applied fix proposal.
        category: The failure category the fix targets.
        applied_at: ISO-8601 timestamp of when the fix was applied.

    """

    fix_proposal_id: str
    category: str
    applied_at: str


@dataclass
class FixEffectivenessReport:
    """Post-fix effectiveness measurement — pre vs. post recurrence.

    Attributes:
        report_id: Unique identifier for this report (uuid4 hex).
        fix_proposal_id: The applied fix proposal id.
        category: The failure category.
        applied_at: ISO-8601 timestamp of fix application.
        pre_fix_period: ``(start, end)`` ISO-8601 timestamps of the
            pre-fix observation window (same duration as post-fix).
        pre_fix_count: Number of diagnostic events in the pre-fix window.
        post_fix_period: ``(start, end)`` ISO-8601 timestamps of the
            post-fix observation window.
        post_fix_count: Number of diagnostic events in the post-fix window.
        reduction_pct: Percentage reduction (positive = improvement,
            negative = worse).  Rounded to 1 decimal place.
        effective: ``True`` when post-fix count < pre-fix count AND
            pre-fix count > 0.

    """

    report_id: str
    fix_proposal_id: str
    category: str
    applied_at: str
    pre_fix_period: tuple[str, str]
    pre_fix_count: int
    post_fix_period: tuple[str, str]
    post_fix_count: int
    reduction_pct: float
    effective: bool


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


class EffectivenessStore(JsonStoreBase[Any]):
    """Persist fix applications and effectiveness reports to a JSON file.

    Construct with an overridable ``path`` and injectable ``clock`` so
    tests can pin timestamps.  Methods never raise unhandled exceptions —
    they log warnings on persistence failures.
    """

    _store_name = "effectiveness store"

    def __init__(
        self,
        path: str | Path = "/data/diagnostics_effectiveness.json",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a store persisting to *path*."""
        super().__init__(path, clock=clock)

    # ------------------------------------------------------------------
    # storage (overrides single-dict default)
    # ------------------------------------------------------------------

    def _init_storage(self) -> None:
        self._fixes: dict[str, FixApplication] = {}
        self._reports: dict[str, FixEffectivenessReport] = {}

    # ------------------------------------------------------------------
    # serialisation hooks (not used — this store manages two dicts)
    # ------------------------------------------------------------------

    def _to_dict(self, item: Any) -> dict[str, object]:
        raise NotImplementedError("EffectivenessStore uses custom persistence")

    @classmethod
    def _from_dict(cls, d: dict[str, Any]) -> Any:
        raise NotImplementedError("EffectivenessStore uses custom persistence")

    # ------------------------------------------------------------------
    # fix applications
    # ------------------------------------------------------------------

    def record_fix(
        self,
        fix_proposal_id: str,
        category: str,
        applied_at: str | None = None,
    ) -> FixApplication:
        """Record a fix application; returns the :class:`FixApplication`.

        When *applied_at* is ``None`` the injected clock provides the timestamp.
        """
        if applied_at is None:
            applied_at = self._clock().isoformat()
        app = FixApplication(
            fix_proposal_id=fix_proposal_id,
            category=category,
            applied_at=applied_at,
        )
        self._fixes[fix_proposal_id] = app
        self._persist()
        return app

    def get_fix(self, fix_proposal_id: str) -> FixApplication | None:
        """Return the fix application for *fix_proposal_id*, or ``None``."""
        return self._fixes.get(fix_proposal_id)

    def list_fixes(self) -> list[FixApplication]:
        """Return all recorded fix applications."""
        return list(self._fixes.values())

    # ------------------------------------------------------------------
    # reports
    # ------------------------------------------------------------------

    def save_report(self, report: FixEffectivenessReport) -> None:
        """Persist *report*; overwrites any existing report with the same id."""
        self._reports[report.report_id] = report
        self._persist()

    def list_reports(self, category: str = "") -> list[FixEffectivenessReport]:
        """Return all reports, optionally filtered by *category*."""
        if not category:
            return list(self._reports.values())
        return [r for r in self._reports.values() if r.category == category]

    def has_report_for_fix(self, fix_proposal_id: str) -> bool:
        """Return ``True`` if a report already exists for *fix_proposal_id*."""
        return any(r.fix_proposal_id == fix_proposal_id for r in self._reports.values())

    # ------------------------------------------------------------------
    # serialisation (overrides base — two-dict format)
    # ------------------------------------------------------------------

    def _serialize(self) -> bytes:
        """Serialize fixes and reports to JSON bytes."""
        payload: dict[str, list[dict[str, object]]] = {
            "fixes": [
                {
                    "fix_proposal_id": f.fix_proposal_id,
                    "category": f.category,
                    "applied_at": f.applied_at,
                }
                for f in self._fixes.values()
            ],
            "reports": [
                {
                    "report_id": r.report_id,
                    "fix_proposal_id": r.fix_proposal_id,
                    "category": r.category,
                    "applied_at": r.applied_at,
                    "pre_fix_period": list(r.pre_fix_period),
                    "pre_fix_count": r.pre_fix_count,
                    "post_fix_period": list(r.post_fix_period),
                    "post_fix_count": r.post_fix_count,
                    "reduction_pct": r.reduction_pct,
                    "effective": r.effective,
                }
                for r in self._reports.values()
            ],
        }
        return json.dumps(payload, indent=2).encode("utf-8")

    def _deserialize(self, data: bytes) -> None:
        """Deserialize JSON bytes into self._fixes and self._reports."""
        raw = json.loads(data)
        if not isinstance(raw, dict):
            return

        for item in raw.get("fixes") or []:
            if not isinstance(item, dict):
                continue
            app = FixApplication(
                fix_proposal_id=item.get("fix_proposal_id", ""),
                category=item.get("category", ""),
                applied_at=item.get("applied_at", ""),
            )
            if app.fix_proposal_id:
                self._fixes[app.fix_proposal_id] = app

        for item in raw.get("reports") or []:
            if not isinstance(item, dict):
                continue
            pre = item.get("pre_fix_period", ["", ""])
            post = item.get("post_fix_period", ["", ""])
            report = FixEffectivenessReport(
                report_id=item.get("report_id", ""),
                fix_proposal_id=item.get("fix_proposal_id", ""),
                category=item.get("category", ""),
                applied_at=item.get("applied_at", ""),
                pre_fix_period=(
                    pre[0] if isinstance(pre, list) and len(pre) >= 2 else "",
                    pre[1] if isinstance(pre, list) and len(pre) >= 2 else "",
                ),
                pre_fix_count=item.get("pre_fix_count", 0),
                post_fix_period=(
                    post[0] if isinstance(post, list) and len(post) >= 2 else "",
                    post[1] if isinstance(post, list) and len(post) >= 2 else "",
                ),
                post_fix_count=item.get("post_fix_count", 0),
                reduction_pct=item.get("reduction_pct", 0.0),
                effective=item.get("effective", False),
            )
            if report.report_id:
                self._reports[report.report_id] = report


# ---------------------------------------------------------------------------
# recurrence measurer
# ---------------------------------------------------------------------------


class RecurrenceMeasurer:
    """Track fix applications and measure post-fix recurrence.

    On :meth:`apply_fix` the fix application is recorded in the
    :class:`EffectivenessStore`.  After the observation window elapses,
    :meth:`generate_report` counts diagnostic events before and after the
    application date and produces a :class:`FixEffectivenessReport`.

    :meth:`generate_pending_reports` iterates all fixes that have not yet
    had a report generated and whose observation window has elapsed,
    producing reports in batch — this is the entry point for periodic
    auto-generation.
    """

    def __init__(
        self,
        diagnostic_store: DiagnosticStore,
        effectiveness_store: EffectivenessStore,
        observation_window_days: int = 30,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialize the measurer.

        Args:
            diagnostic_store: Store to query for diagnostic events.
            effectiveness_store: Store to persist fix applications and reports.
            observation_window_days: Days before/after fix to count events.
            clock: Injectable clock for deterministic timestamps in tests.

        """
        self._diag = diagnostic_store
        self._eff = effectiveness_store
        self._window_days = observation_window_days
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def apply_fix(
        self,
        fix_proposal_id: str,
        category: str,
        applied_at: str | None = None,
    ) -> FixApplication:
        """Record a fix application and return the :class:`FixApplication`."""
        return self._eff.record_fix(
            fix_proposal_id=fix_proposal_id,
            category=category,
            applied_at=applied_at,
        )

    def generate_report(self, fix_proposal_id: str) -> FixEffectivenessReport | None:
        """Compute and persist an effectiveness report for a fix.

        Returns ``None`` when *fix_proposal_id* is unknown.  The report is
        saved to the :class:`EffectivenessStore` and is idempotent —
        calling twice returns the same persistent report.
        """
        # Check for an already-generated report first (idempotent).
        for existing in self._eff.list_reports():
            if existing.fix_proposal_id == fix_proposal_id:
                return existing

        fix = self._eff.get_fix(fix_proposal_id)
        if fix is None:
            return None

        applied_dt = datetime.fromisoformat(fix.applied_at)
        pre_start = applied_dt - timedelta(days=self._window_days)
        post_end = applied_dt + timedelta(days=self._window_days)

        # Pre-fix events: from (applied_at - window) up to (but not including)
        # applied_at.
        pre_events = self._diag.events_since(pre_start, fix.category)
        pre_count = len(
            [
                e
                for e in pre_events
                if _parse_ts(e.created_at) is not None
                and _parse_ts(e.created_at) < applied_dt  # type: ignore[operator]
            ]
        )

        # Post-fix events: from applied_at up to (applied_at + window).
        post_events = self._diag.events_since(applied_dt, fix.category)
        post_count = len(
            [
                e
                for e in post_events
                if _parse_ts(e.created_at) is not None
                and _parse_ts(e.created_at) <= post_end  # type: ignore[operator]
            ]
        )

        if pre_count == 0:
            effective = False
            reduction_pct = 0.0
        else:
            reduction_pct = round((pre_count - post_count) / pre_count * 100, 1)
            effective = post_count < pre_count

        report = FixEffectivenessReport(
            report_id=uuid.uuid4().hex,
            fix_proposal_id=fix.fix_proposal_id,
            category=fix.category,
            applied_at=fix.applied_at,
            pre_fix_period=(pre_start.isoformat(), fix.applied_at),
            pre_fix_count=pre_count,
            post_fix_period=(fix.applied_at, post_end.isoformat()),
            post_fix_count=post_count,
            reduction_pct=reduction_pct,
            effective=effective,
        )
        self._eff.save_report(report)
        return report

    def generate_pending_reports(self) -> list[FixEffectivenessReport]:
        """Generate reports for all fixes whose observation window has elapsed.

        Skips fixes that already have a report.  Returns the newly-generated
        reports (may be empty).
        """
        now = self._clock()
        new_reports: list[FixEffectivenessReport] = []
        for fix in self._eff.list_fixes():
            if self._eff.has_report_for_fix(fix.fix_proposal_id):
                continue
            applied_dt = datetime.fromisoformat(fix.applied_at)
            window_end = applied_dt + timedelta(days=self._window_days)
            if now >= window_end:
                report = self.generate_report(fix.fix_proposal_id)
                if report is not None:
                    new_reports.append(report)
        return new_reports


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_ts(iso_string: str) -> datetime | None:
    """Parse an ISO-8601 string to a datetime; return ``None`` on failure."""
    try:
        return datetime.fromisoformat(iso_string)
    except ValueError, TypeError:
        return None

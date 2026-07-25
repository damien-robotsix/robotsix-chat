"""Diagnostics module — capture, categorize, and surface systemic fixes.

Exposes :func:`build_diagnostics_tools` — a factory that returns agent tools
for listing diagnostic events and managing fix proposals.  Returns no tools
when diagnostics is disabled.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DiagnosticsSettings

from .fixes import FixProposalStore, FixSurfacer, RecurrenceDetector
from .store import DiagnosticStore
from .verification import (
    EffectivenessStore,
    FixEffectivenessReport,
    RecurrenceMeasurer,
)

__all__ = [
    "DiagnosticStore",
    "EffectivenessStore",
    "FixEffectivenessReport",
    "FixProposalStore",
    "FixSurfacer",
    "RecurrenceDetector",
    "RecurrenceMeasurer",
    "build_diagnostics_tools",
]


def build_diagnostics_tools(
    settings: DiagnosticsSettings,
    *,
    store: DiagnosticStore | None = None,
) -> list[Callable[..., Any]]:
    """Return diagnostics tools, or ``[]`` when disabled.

    When *store* is given it is reused — this lets an HTTP endpoint share
    the same in-memory instance so events posted via the API are visible to
    agent tools immediately.
    """
    if not settings.enabled:
        return []

    if store is None:
        store = DiagnosticStore(settings.store_path)
    proposal_store = FixProposalStore(settings.proposals_path)
    eff_store = EffectivenessStore(settings.effectiveness_path)

    detector = RecurrenceDetector(
        store,
        threshold=settings.recurrence_threshold,
        window_days=settings.recurrence_window_days,
    )
    surfacer = FixSurfacer(proposal_store)
    measurer = RecurrenceMeasurer(
        store,
        eff_store,
        observation_window_days=settings.observation_window_days,
    )

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------

    async def list_diagnostic_events(category: str = "") -> str:
        """List captured diagnostic events, optionally filtered by category.

        Args:
            category: Optional filter (e.g. ``CLONE_TARGET``, ``CI_FAILURE``).
                Omit or pass ``""`` to list all.

        Returns:
            A formatted listing of diagnostic events.

        """
        entries = store.list_events(category)
        if not entries:
            return "No diagnostic events found." + (
                f" (category: {category})" if category else ""
            )

        lines: list[str] = []
        for e in entries:
            lines.append(
                f"[{e.id}] {e.category}\n"
                f"  message: {e.message}\n"
                f"  created_at: {e.created_at}"
            )
        return "\n".join(lines)

    async def check_recurring_categories() -> str:
        """Scan diagnostic events for categories that have recurred above threshold.

        When a category recurs above the configured threshold, a fix proposal
        is auto-generated and stored for review.

        Returns:
            A summary of recurring categories and any new proposals generated.

        """
        recurring = detector.find_recurring()
        if not recurring:
            return "No categories have recurred above the threshold."

        proposals_created: list[str] = []
        for category, count in recurring.items():
            proposal = surfacer.surface_fix(category, count)
            proposals_created.append(
                f"  - {category}: {count} occurrences → proposal {proposal.id}"
            )

        return "Recurring categories detected:\n" + "\n".join(proposals_created)

    async def list_fix_proposals(category: str = "") -> str:
        """List fix proposals, optionally filtered by category.

        Args:
            category: Optional filter (e.g. ``CLONE_TARGET``, ``CI_FAILURE``).
                Omit or pass ``""`` to list all.

        Returns:
            A formatted listing of fix proposals with id, status, and suggestion.

        """
        proposals = proposal_store.list_proposals(category)
        if not proposals:
            return "No fix proposals found." + (
                f" (category: {category})" if category else ""
            )

        lines: list[str] = []
        for p in proposals:
            lines.append(
                f"[{p.id}] {p.category} ({p.status})\n"
                f"  description: {p.description}\n"
                f"  suggested_fix: {p.suggested_fix}\n"
                f"  created_at: {p.created_at}"
            )
        return "\n".join(lines)

    async def apply_fix(proposal_id: str) -> str:
        """Mark a fix proposal as applied and record it for recurrence measurement.

        Args:
            proposal_id: The id of the proposal to apply.

        Returns:
            Confirmation or error when the id is unknown.

        """
        proposal = proposal_store.apply(proposal_id)
        if proposal is None:
            return f"Error: no fix proposal found with id '{proposal_id}'"
        # Record the fix application for recurrence measurement.
        measurer.apply_fix(
            fix_proposal_id=proposal.id,
            category=proposal.category,
        )
        return (
            f"Applied fix proposal {proposal.id} ({proposal.category}).\n"
            f"  suggested_fix: {proposal.suggested_fix}"
        )

    async def reject_fix(proposal_id: str) -> str:
        """Mark a fix proposal as rejected.

        Args:
            proposal_id: The id of the proposal to reject.

        Returns:
            Confirmation or error when the id is unknown.

        """
        proposal = proposal_store.reject(proposal_id)
        if proposal is None:
            return f"Error: no fix proposal found with id '{proposal_id}'"
        return f"Rejected fix proposal {proposal.id} ({proposal.category})."

    async def list_effectiveness_reports(category: str = "") -> str:
        """List fix-effectiveness reports, optionally filtered by category.

        Reports show pre-fix vs. post-fix recurrence counts and whether the
        fix was effective.  For fixes that were ineffective (``effective=false``),
        the report is marked as "needs revisiting."

        Args:
            category: Optional filter (e.g. ``CLONE_TARGET``, ``CI_FAILURE``).
                Omit or pass ``""`` to list all.

        Returns:
            A formatted listing of effectiveness reports.

        """
        # Also try to generate any pending reports first.
        _ = measurer.generate_pending_reports()

        reports = eff_store.list_reports(category)
        if not reports:
            return "No effectiveness reports found." + (
                f" (category: {category})" if category else ""
            )

        lines: list[str] = []
        for r in reports:
            status = "effective" if r.effective else "needs revisiting"
            lines.append(
                f"[{r.report_id}] {r.category} ({status})\n"
                f"  fix: {r.fix_proposal_id}\n"
                f"  applied_at: {r.applied_at}\n"
                f"  pre_fix_count: {r.pre_fix_count}\n"
                f"  post_fix_count: {r.post_fix_count}\n"
                f"  reduction: {r.reduction_pct}%"
            )
        return "\n".join(lines)

    return [
        list_diagnostic_events,
        check_recurring_categories,
        list_fix_proposals,
        apply_fix,
        reject_fix,
        list_effectiveness_reports,
    ]

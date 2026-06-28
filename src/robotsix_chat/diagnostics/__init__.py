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

__all__ = [
    "DiagnosticStore",
    "FixProposalStore",
    "FixSurfacer",
    "RecurrenceDetector",
    "build_diagnostics_tools",
]


def build_diagnostics_tools(
    settings: DiagnosticsSettings,
) -> list[Callable[..., Any]]:
    """Return diagnostics tools, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    store = DiagnosticStore(settings.store_path)
    proposal_store = FixProposalStore(settings.proposals_path)

    detector = RecurrenceDetector(
        store,
        threshold=settings.recurrence_threshold,
        window_days=settings.recurrence_window_days,
    )
    surfacer = FixSurfacer(proposal_store)

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
        """Mark a fix proposal as applied.

        Args:
            proposal_id: The id of the proposal to apply.

        Returns:
            Confirmation or error when the id is unknown.

        """
        proposal = proposal_store.apply(proposal_id)
        if proposal is None:
            return f"Error: no fix proposal found with id '{proposal_id}'"
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

    return [
        list_diagnostic_events,
        check_recurring_categories,
        list_fix_proposals,
        apply_fix,
        reject_fix,
    ]

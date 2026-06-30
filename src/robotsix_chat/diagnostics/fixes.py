"""Systemic fix surfacing — detect recurring failures and propose fixes.

:class:`RecurrenceDetector` scans a
:class:`~robotsix_chat.diagnostics.store.DiagnosticStore` for categories
that have recurred at or above a configurable threshold within a time
window.

:class:`FixSurfacer` generates :class:`FixProposal` instances from a curated
category→template mapping, stored in a :class:`FixProposalStore` for agent
or human review.  Proposals are never auto-applied — they must be explicitly
accepted via ``apply_fix`` or dismissed via ``reject_fix``.
"""

from __future__ import annotations

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
# curated fix templates per category
# ---------------------------------------------------------------------------

_CATEGORY_FIX_TEMPLATES: dict[str, str] = {
    "CLONE_TARGET": (
        "Add/update repos.yaml entry; verify registration in board; "
        "add pre-clone validation check"
    ),
    "CI_FAILURE": (
        "Add CI status check to pipeline gate; implement CI retry with "
        "backoff; add flaky-test quarantine"
    ),
    "DEPENDENCY": (
        "Auto-unblock when dependency resolves; add missing-SHA detection "
        "in refine stage; cross-ticket dependency graph"
    ),
    "REFINEMENT": (
        "Add refinement loop detection; cap refine iterations; improve "
        "spec clarity requirements"
    ),
}


# ---------------------------------------------------------------------------
# FixProposal dataclass
# ---------------------------------------------------------------------------


@dataclass
class FixProposal:
    """A candidate systemic fix surfaced from recurring diagnostic events.

    Attributes:
        id: Unique identifier (uuid4 hex).
        category: Failure category (e.g. ``CLONE_TARGET``).
        description: Human-readable summary of the recurring issue.
        suggested_fix: Curated fix template or custom suggestion.
        status: One of ``proposed``, ``applied``, ``rejected``.
        created_at: ISO-8601 timestamp of proposal creation.
        applied_at: ISO-8601 timestamp when applied, or ``None``.

    """

    id: str
    category: str
    description: str
    suggested_fix: str
    status: str = "proposed"
    created_at: str = ""
    applied_at: str | None = None


# ---------------------------------------------------------------------------
# FixProposalStore — JSON persistence for proposals
# ---------------------------------------------------------------------------


class FixProposalStore(JsonStoreBase[FixProposal]):
    """Persist fix proposals to a JSON file (best-effort atomic writes).

    Tolerates missing/empty/corrupt files on load.  Inject a ``clock``
    callable for deterministic timestamps in tests.
    """

    _store_name = "fix proposals"

    def __init__(
        self,
        path: str | Path = ".data/fix_proposals.json",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a proposal store persisting to *path*."""
        super().__init__(path, clock=clock)

    # ------------------------------------------------------------------
    # serialisation hooks
    # ------------------------------------------------------------------

    def _to_dict(self, item: FixProposal) -> dict[str, object]:
        return {
            "id": item.id,
            "category": item.category,
            "description": item.description,
            "suggested_fix": item.suggested_fix,
            "status": item.status,
            "created_at": item.created_at,
            "applied_at": item.applied_at,
        }

    @classmethod
    def _from_dict(cls, d: dict[str, Any]) -> FixProposal:
        return FixProposal(
            id=d.get("id", ""),
            category=d.get("category", ""),
            description=d.get("description", ""),
            suggested_fix=d.get("suggested_fix", ""),
            status=d.get("status", "proposed"),
            created_at=d.get("created_at", ""),
            applied_at=d.get("applied_at"),
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def add_proposal(
        self,
        category: str,
        description: str,
        suggested_fix: str,
    ) -> FixProposal:
        """Create and persist a new fix proposal."""
        proposal = FixProposal(
            id=uuid.uuid4().hex,
            category=category,
            description=description,
            suggested_fix=suggested_fix,
            status="proposed",
            created_at=self._clock().isoformat(),
            applied_at=None,
        )
        self._items[proposal.id] = proposal
        self._persist()
        return proposal

    def list_proposals(self, category: str = "") -> list[FixProposal]:
        """Return all proposals, optionally filtered by *category*."""
        if not category:
            return list(self._items.values())
        cat = category.strip().lower()
        return [p for p in self._items.values() if p.category.strip().lower() == cat]

    def get_proposal(self, proposal_id: str) -> FixProposal | None:
        """Return the proposal for *proposal_id*, or ``None`` if unknown."""
        return self._items.get(proposal_id)

    def apply(self, proposal_id: str) -> FixProposal | None:
        """Mark a proposal as applied; returns updated proposal or ``None``."""
        proposal = self._items.get(proposal_id)
        if proposal is None:
            return None
        proposal.status = "applied"
        proposal.applied_at = self._clock().isoformat()
        self._persist()
        return proposal

    def reject(self, proposal_id: str) -> FixProposal | None:
        """Mark a proposal as rejected; returns updated proposal or ``None``."""
        proposal = self._items.get(proposal_id)
        if proposal is None:
            return None
        proposal.status = "rejected"
        self._persist()
        return proposal


# ---------------------------------------------------------------------------
# RecurrenceDetector
# ---------------------------------------------------------------------------


class RecurrenceDetector:
    """Scan a ``DiagnosticStore`` for categories that cross a recurrence threshold.

    A category "recurs" when events of that category appear at least
    *threshold* times within the last *window_days*.

    Attributes:
        store: The :class:`~robotsix_chat.diagnostics.store.DiagnosticStore`
            to scan.
        threshold: Minimum number of occurrences to trigger a recurrence.
        window_days: Look-back window in days.
        clock: Injectable callable returning ``datetime`` (defaults to UTC now).

    """

    def __init__(
        self,
        store: DiagnosticStore,
        threshold: int = 3,
        window_days: int = 30,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a detector scanning *store* for recurring categories.

        *threshold* and *window_days* configure recurrence detection;
        *clock* overrides the timestamp source so tests can pin time.
        """
        self._store = store
        self._threshold = threshold
        self._window_days = window_days
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    def find_recurring(self) -> dict[str, int]:
        """Return ``{category: count}`` for every category at or above threshold.

        Categories below the threshold are excluded.
        """
        cutoff = self._clock() - timedelta(days=self._window_days)
        events = self._store.events_since(cutoff)

        counts: dict[str, int] = {}
        for event in events:
            cat = event.category.strip()
            counts[cat] = counts.get(cat, 0) + 1

        return {cat: count for cat, count in counts.items() if count >= self._threshold}


# ---------------------------------------------------------------------------
# FixSurfacer
# ---------------------------------------------------------------------------


class FixSurfacer:
    """Generate :class:`FixProposal` instances for recurring categories.

    Uses a curated mapping of ``category → fix template``.  Categories not in
    the mapping fall through to a generic template.  When a threshold is
    crossed the surfacer creates a proposal in the :class:`FixProposalStore`
    — proposals are **not** auto-applied; they await explicit agent or
    human acceptance.
    """

    def __init__(
        self,
        proposal_store: FixProposalStore,
        *,
        templates: dict[str, str] | None = None,
    ) -> None:
        """Create a surfacer that writes proposals into *proposal_store*.

        *templates* overrides the default category→fix mapping so tests
        can supply custom templates.
        """
        self._proposal_store = proposal_store
        self._templates = templates or _CATEGORY_FIX_TEMPLATES

    def surface_fix(self, category: str, occurrence_count: int) -> FixProposal:
        """Create and persist a fix proposal for a recurring *category*.

        Args:
            category: The recurring failure category.
            occurrence_count: How many times it has recurred.

        Returns:
            The new :class:`FixProposal`.

        """
        suggested = self._templates.get(
            category,
            f"Investigate systemic cause for recurring {category} failures; "
            f"consider automating diagnosis and resolution.",
        )
        description = (
            f"Category '{category}' has recurred {occurrence_count} times "
            f"within the detection window."
        )
        return self._proposal_store.add_proposal(
            category=category,
            description=description,
            suggested_fix=suggested,
        )

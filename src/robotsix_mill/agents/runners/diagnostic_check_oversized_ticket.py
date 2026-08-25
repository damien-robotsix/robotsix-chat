"""Oversized-ticket diagnostic check.

Detects tickets that have been through the implement stage multiple times
without completing — a strong signal that the ticket is too large for a
single implementation pass.  When detected, the ticket is promoted to an
EPIC so the refine stage's ``epic_breakdown`` splits it into smaller,
independently implementable child tasks.

Registered via :func:`register_check` so the daily diagnostic agent
picks the check up automatically — no runner edits required.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.models import TicketKind
from ...core.service import TicketService
from ...core.states import DONE_OR_CLOSED
from .diagnostic_checks import (
    DIAGNOSTIC_CHECKS,
    DiagnosticCheck,
    DiagnosticCheckContext,
    DiagnosticCheckResult,
    register_check,
)

log = logging.getLogger(__name__)

_DIAGNOSTIC_TITLE_PREFIX = "[diagnostic] oversized ticket:"


def _register_check_once(check: DiagnosticCheck) -> DiagnosticCheck:
    """Register *check*, replacing any same-name entry already registered.

    The shadow package ejects a cached installed copy of this module and
    re-imports the local one (see ``src/robotsix_mill/__init__.py``), so
    under some import orders an installed copy may already have registered
    itself.  Dedup by ``name`` keeps exactly one instance per check.
    """
    for existing in list(DIAGNOSTIC_CHECKS):
        if getattr(existing, "name", None) == check.name:
            DIAGNOSTIC_CHECKS.remove(existing)
            log.warning(
                "diagnostic_check_oversized_ticket: replacing duplicate %r "
                "registration",
                check.name,
            )
    return register_check(check)


class OversizedTicketCheck:
    """Detect oversized tickets and promote them to EPICs for splitting.

    A ticket is considered oversized when its ``implement_cycles`` count
    reaches or exceeds a configurable threshold (default 2, settable via
    ``diagnostic_oversized_ticket_threshold``).  Each implement cycle
    represents a full implement-stage pass that failed to complete the
    work — repeated cycles without progress signal the ticket is too
    large for a single pass.

    When an oversized ticket is detected:

    1. The ticket is promoted to an EPIC (``promote_to_epic``).
    2. A diagnostic comment is posted explaining why.
    3. The refine stage's ``epic_breakdown`` then handles splitting the
       EPIC into smaller, independently implementable child tasks.

    Safety guards:
    - Tickets already promoted to EPIC (``kind == EPIC``) are skipped.
    - Tickets that already have children are skipped.
    - Tickets with a ``parent_id`` (already part of a split) are skipped.
    - Duplicate detection prevents filing the same ticket repeatedly.
    """

    name = "oversized_ticket"

    def run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        """Execute the oversized-ticket check and promote qualifying tickets."""
        try:
            return self._run(ctx)
        except Exception:
            log.exception("oversized_ticket check failed")
            return DiagnosticCheckResult(
                name=self.name,
                ok=False,
                summary="oversized_ticket check raised an exception",
            )

    def _run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        settings = ctx.settings
        board_id = ctx.board_id

        threshold = getattr(settings, "diagnostic_oversized_ticket_threshold", 2)
        if threshold == 0:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary="oversized ticket detection disabled (threshold=0)",
            )

        service = TicketService(settings, board_id=board_id)

        # Find active TASK tickets with high implement_cycles.
        active_tickets = service.list(
            exclude_states=DONE_OR_CLOSED,
        )
        oversized = [
            t
            for t in active_tickets
            if t.kind == TicketKind.TASK
            and t.implement_cycles >= threshold
            and not t.parent_id  # not already under a parent/EPIC
        ]

        if not oversized:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary=(
                    f"{len(active_tickets)} active ticket(s); "
                    f"none with implement_cycles >= {threshold}"
                ),
            )

        promoted: list[dict[str, Any]] = []

        for ticket in oversized:
            if self._already_ignored(ticket, service):
                log.info(
                    "oversized_ticket: skipping %s — already promoted or has children",
                    ticket.id,
                )
                continue

            try:
                # Record original metadata before promotion.
                original_title = ticket.title
                original_cycles = ticket.implement_cycles

                # Promote the task to an EPIC so the refine stage's
                # epic_breakdown can split it into children.
                service.promote_to_epic(ticket.id)

                # Post a diagnostic comment explaining the promotion.
                comment_body = self._build_comment(
                    board_id=board_id,
                    implement_cycles=original_cycles,
                    threshold=threshold,
                )
                service.add_comment(
                    ticket.id,
                    comment_body,
                    author="diagnostic",
                )

                log.info(
                    "oversized_ticket: promoted %s to EPIC (%d implement "
                    "cycles >= threshold %d)",
                    ticket.id,
                    original_cycles,
                    threshold,
                )
                promoted.append({"id": ticket.id, "title": original_title})

            except Exception:
                log.exception("oversized_ticket: failed to promote %s", ticket.id)

        summary = (
            f"{len(active_tickets)} active ticket(s); "
            f"{len(oversized)} with implement_cycles >= {threshold}; "
            f"{len(promoted)} promoted to EPIC"
        )
        return DiagnosticCheckResult(
            name=self.name,
            ok=True,
            summary=summary,
            drafts_created=promoted,
        )

    @staticmethod
    def _already_ignored(ticket: Any, service: TicketService) -> bool:
        """Return True if the ticket was already handled.

        Skips tickets that:
        - Already have children (a prior promotion + split).
        - Are already an EPIC (``kind == EPIC``).
        - Have an existing non-terminal diagnostic split ticket for them.
        """
        # Already an EPIC — nothing to do.
        if ticket.kind == TicketKind.EPIC:
            return True

        # Already has children — split was already triggered.
        children = service.list_children(ticket.id)
        if children:
            return True

        # Check for an existing non-terminal diagnostic ticket referencing
        # this ticket's title (prevents re-promotion on subsequent passes).
        norm_title = f"{_DIAGNOSTIC_TITLE_PREFIX} {ticket.title}".strip().casefold()
        for t in service.list():
            if (
                t.title.strip().casefold() == norm_title
                and t.state not in DONE_OR_CLOSED
            ):
                return True

        return False

    @staticmethod
    def _build_comment(
        *,
        board_id: str,
        implement_cycles: int,
        threshold: int,
    ) -> str:
        """Build the diagnostic comment posted on the promoted ticket."""
        lines = [
            "Auto-promoted to EPIC by the `oversized_ticket` diagnostic check.",
            "",
            f"- **Repository / board:** `{board_id}`",
            f"- **Implement cycles before promotion:** {implement_cycles} "
            f"(threshold: {threshold})",
            "",
            "### Why this ticket was promoted",
            "",
            f"This ticket has been through the implement stage **{implement_cycles} "
            f"time(s)** without reaching `done`.  Repeated implement cycles "
            "without completion strongly indicate the ticket is too large for "
            "a single implementation pass.",
            "",
            "### What happens next",
            "",
            "The refine stage's ``epic_breakdown`` will analyze this EPIC's "
            "description and break it into **smaller, independently "
            "implementable child tasks**.  Each child will be a focused, "
            "scoped piece of the original work that can be completed in a "
            "single implement pass.",
            "",
            "If you need to adjust the split boundaries, edit the EPIC "
            "description before the next refine pass to guide the breakdown.",
        ]
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Register the check at import time (pickup by diagnostic runner).
# ---------------------------------------------------------------------------
_register_check_once(OversizedTicketCheck())

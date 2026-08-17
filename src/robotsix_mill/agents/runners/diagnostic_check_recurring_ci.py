"""Recurring CI failure diagnostic checks.

Shadow override of the installed
``robotsix_mill.agents.runners.diagnostic_check_recurring_ci`` — adds
:class:`MultiCauseCIFailureCheck` alongside the existing
:class:`RecurringCIFailureCheck` so that a single ticket/branch
accumulating *different* CI failure causes also gets a hardening draft
ticket.

Registered via :func:`register_check` so the daily diagnostic agent
picks both checks up automatically — no runner edits required.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from ...core.models import SourceKind, TicketKind
from ...core.service import TicketService
from ...core.states import DONE_OR_CLOSED
from .diagnostic_checks import (
    DIAGNOSTIC_CHECKS,
    DiagnosticCheck,
    DiagnosticCheckContext,
    DiagnosticCheckResult,
    register_check,
)
from .diagnostic_events import list_diagnostic_events

log = logging.getLogger(__name__)

_DIAGNOSTIC_TITLE_PREFIX = "[diagnostic] recurring CI failure:"
_MULTICAUSE_TITLE_PREFIX = "[diagnostic] flaky CI on ticket"


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
                "diagnostic_check_recurring_ci: replacing duplicate %r registration",
                check.name,
            )
    return register_check(check)


def _normalized_key_short(key: str) -> str:
    """First 8 chars of a hex key — enough to disambiguate in titles."""
    return key[:8] if len(key) >= 8 else key


# ============================================================================
# RecurringCIFailureCheck — same-key across distinct tickets
# ============================================================================


class RecurringCIFailureCheck:
    """Detect recurring CI failures and file fix-proposal draft tickets.

    Groups ``CI_FAILURE`` events by **normalized key** and files a ticket
    when the *same key* has been hit by ≥ ``diagnostic_ci_failure_threshold``
    **distinct tickets** (default 3).
    """

    name = "recurring_ci_failure"

    def run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        """Execute the recurring CI failure check and file fix-proposal drafts."""
        try:
            return self._run(ctx)
        except Exception:
            log.exception("recurring_ci_failure check failed")
            return DiagnosticCheckResult(
                name=self.name,
                ok=False,
                summary="recurring_ci_failure check raised an exception",
            )

    def _run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        settings = ctx.settings
        board_id = ctx.board_id

        threshold = settings.diagnostic_ci_failure_threshold
        if threshold == 0:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary="recurring CI failure detection disabled (threshold=0)",
            )

        events = list_diagnostic_events(settings, board_id, category="CI_FAILURE")
        if not events:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary="no CI_FAILURE events in store",
            )

        # Group by normalized key; collect distinct ticket ids.
        groups: dict[str, set[str]] = defaultdict(set)
        reasons: dict[str, str] = {}
        for ev in events:
            groups[ev.normalized_key].add(ev.ticket_id)
            reasons[ev.normalized_key] = ev.reason  # last wins; fine for body

        # Find keys that have crossed the threshold.
        triggered = {
            key: tickets for key, tickets in groups.items() if len(tickets) >= threshold
        }
        if not triggered:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary=(
                    f"{len(events)} CI_FAILURE event(s) across "
                    f"{len(groups)} key(s); none reached threshold {threshold}"
                ),
            )

        service = TicketService(settings, board_id=board_id)
        drafts_created: list[dict[str, Any]] = []

        for key, tickets in sorted(triggered.items()):
            short = _normalized_key_short(key)
            title = f"{_DIAGNOSTIC_TITLE_PREFIX} key={short} ({len(tickets)} tickets)"
            if self._is_duplicate(title, service):
                log.info("recurring_ci_failure: skipping duplicate ticket %r", title)
                continue
            body = self._build_body(board_id, key, tickets, reasons.get(key, ""))
            try:
                ticket = service.create(
                    title,
                    body,
                    source=SourceKind.AGENT,
                    kind=TicketKind.TASK,
                )
                log.info(
                    "recurring_ci_failure: filed fix-proposal ticket %s — %r",
                    ticket.id,
                    title,
                )
                drafts_created.append({"id": ticket.id, "title": title})
            except Exception:
                log.exception(
                    "recurring_ci_failure: failed to file ticket for key %s", key
                )

        summary = (
            f"{len(events)} CI_FAILURE event(s) across {len(groups)} key(s); "
            f"{len(triggered)} key(s) reached threshold {threshold}; "
            f"{len(drafts_created)} fix-proposal draft(s) filed"
        )
        return DiagnosticCheckResult(
            name=self.name,
            ok=True,
            summary=summary,
            drafts_created=drafts_created,
        )

    @staticmethod
    def _is_duplicate(title: str, service: TicketService) -> bool:
        """Return True if a non-terminal ticket with *title* already exists."""
        norm = title.strip().casefold()
        for t in service.list():
            if t.title.strip().casefold() == norm and t.state not in DONE_OR_CLOSED:
                return True
        return False

    @staticmethod
    def _build_body(
        board_id: str,
        normalized_key: str,
        tickets: set[str],
        reason: str,
    ) -> str:
        """Build the fix-proposal ticket body."""
        ticket_list = "\n".join(f"- `{tid}`" for tid in sorted(tickets))
        lines = [
            "Auto-filed by the daily diagnostic agent (recurring_ci_failure check).",
            "",
            f"- **Repository / board:** `{board_id}`",
            f"- **Normalized failure key:** `{normalized_key}`",
            f"- **Distinct tickets affected:** {len(tickets)}",
            "",
            "### Affected tickets",
            ticket_list,
            "",
            "### Failure reason (representative)",
            "",
            "```",
            reason[:4000] if reason else "(no reason recorded)",
            "```",
            "",
            "### Action",
            (
                "Review the recurring CI failure pattern above. If a systemic "
                "fix is appropriate (e.g. a pre-commit hook, a CI workflow "
                "change, or a lint rule adjustment), draft a task ticket for "
                "the fix. Once the root cause is resolved, this diagnostic "
                "will stop filing for this key — existing events age out "
                "naturally as new tickets cycle through CI."
            ),
        ]
        return "\n".join(lines) + "\n"


# ============================================================================
# MultiCauseCIFailureCheck — distinct keys on the same ticket
# ============================================================================


class MultiCauseCIFailureCheck:
    """Flag tickets that accumulate *different* CI failure causes.

    Groups ``CI_FAILURE`` events by **ticket_id** and counts the number
    of **distinct** ``normalized_key`` values per ticket.  When a single
    ticket has accumulated ≥ ``diagnostic_ci_multicause_threshold``
    distinct keys (default 3), a hardening draft ticket is filed so a
    human investigates the systemic root cause — otherwise the mill
    iterates single-cause fixes forever.
    """

    name = "multicause_ci_failure"

    def run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        """Execute the multi-cause CI failure check and file hardening drafts."""
        try:
            return self._run(ctx)
        except Exception:
            log.exception("multicause_ci_failure check failed")
            return DiagnosticCheckResult(
                name=self.name,
                ok=False,
                summary="multicause_ci_failure check raised an exception",
            )

    def _run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        settings = ctx.settings
        board_id = ctx.board_id

        threshold = getattr(settings, "diagnostic_ci_multicause_threshold", 3)
        if threshold == 0:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary="multi-cause CI flakiness detection disabled (threshold=0)",
            )

        events = list_diagnostic_events(settings, board_id, category="CI_FAILURE")
        if not events:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary="no CI_FAILURE events in store",
            )

        # Group by ticket_id; collect distinct normalized keys per ticket.
        # Also track the most recent reason per ticket for the body.
        ticket_keys: dict[str, set[str]] = defaultdict(set)
        ticket_reasons: dict[str, str] = {}
        for ev in events:
            ticket_keys[ev.ticket_id].add(ev.normalized_key)
            ticket_reasons[ev.ticket_id] = ev.reason  # last wins

        triggered = {
            tid: keys for tid, keys in ticket_keys.items() if len(keys) >= threshold
        }
        if not triggered:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary=(
                    f"{len(events)} CI_FAILURE event(s) across "
                    f"{len(ticket_keys)} ticket(s); none reached "
                    f"threshold {threshold} distinct causes"
                ),
            )

        service = TicketService(settings, board_id=board_id)
        drafts_created: list[dict[str, Any]] = []

        for ticket_id, keys in sorted(triggered.items()):
            title = (
                f"{_MULTICAUSE_TITLE_PREFIX} {ticket_id}: "
                f"{len(keys)} distinct failure causes"
            )
            if self._is_duplicate(title, service):
                log.info("multicause_ci_failure: skipping duplicate ticket %r", title)
                continue
            body = self._build_body(
                board_id, ticket_id, keys, ticket_reasons.get(ticket_id, "")
            )
            try:
                ticket = service.create(
                    title,
                    body,
                    source=SourceKind.AGENT,
                    kind=TicketKind.TASK,
                )
                log.info(
                    "multicause_ci_failure: filed hardening ticket %s — %r",
                    ticket.id,
                    title,
                )
                drafts_created.append({"id": ticket.id, "title": title})
            except Exception:
                log.exception(
                    "multicause_ci_failure: failed to file ticket for %s", ticket_id
                )

        summary = (
            f"{len(events)} CI_FAILURE event(s) across {len(ticket_keys)} ticket(s); "
            f"{len(triggered)} ticket(s) reached threshold {threshold}; "
            f"{len(drafts_created)} hardening draft(s) filed"
        )
        return DiagnosticCheckResult(
            name=self.name,
            ok=True,
            summary=summary,
            drafts_created=drafts_created,
        )

    @staticmethod
    def _is_duplicate(title: str, service: TicketService) -> bool:
        """Return True if a non-terminal ticket with *title* already exists."""
        norm = title.strip().casefold()
        for t in service.list():
            if t.title.strip().casefold() == norm and t.state not in DONE_OR_CLOSED:
                return True
        return False

    @staticmethod
    def _build_body(
        board_id: str,
        ticket_id: str,
        keys: set[str],
        reason: str,
    ) -> str:
        key_list = "\n".join(f"- `{k}`" for k in sorted(keys))
        lines = [
            "Auto-filed by the daily diagnostic agent (multicause_ci_failure check).",
            "",
            f"- **Repository / board:** `{board_id}`",
            f"- **Source ticket:** `{ticket_id}`",
            f"- **Distinct failure causes:** {len(keys)}",
            "",
            "### Failure keys",
            key_list,
            "",
            "### Most recent failure reason",
            "",
            "```",
            reason[:4000] if reason else "(no reason recorded)",
            "```",
            "",
            "### Action",
            (
                "This ticket has failed CI repeatedly with **different** root "
                "causes each iteration. Single-cause fixes will not stop the "
                "cycle — investigate the systemic issue (dependency drift, "
                "environment flakiness, missing coverage, …) and harden the "
                "CI pipeline. Once the root cause is resolved, this diagnostic "
                "will stop filing for this ticket — existing events age out "
                "naturally as new iterations cycle through CI."
            ),
        ]
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Register both checks at import time (pickup by diagnostic runner).
# ---------------------------------------------------------------------------
_register_check_once(RecurringCIFailureCheck())
_register_check_once(MultiCauseCIFailureCheck())

"""Diagnostics module — capture, categorize, and surface systemic fixes.

Exposes :func:`build_diagnostics_tools` — a factory that returns agent tools
for listing diagnostic events and managing fix proposals.  Returns no tools
when diagnostics is disabled.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from robotsix_chat.config import DiagnosticsSettings, DirectRepoSettings

from .fixes import FixProposalStore, FixSurfacer, RecurrenceDetector
from .store import DiagnosticStore
from .verification import (
    EffectivenessStore,
    FixEffectivenessReport,
    RecurrenceMeasurer,
)

logger = logging.getLogger(__name__)

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
    direct_repo: DirectRepoSettings | None = None,
) -> list[Callable[..., Any]]:
    """Return diagnostics tools, or ``[]`` when disabled.

    When *store* is given it is reused — this lets an HTTP endpoint share
    the same in-memory instance so events posted via the API are visible to
    agent tools immediately.  *direct_repo* supplies the mill board API
    base URL/token used by ``read_diagnostic_events``.
    """
    if not settings.enabled:
        return []

    mill_base_url = (
        direct_repo.board_api_base_url.strip().rstrip("/") if direct_repo else ""
    )
    mill_token = direct_repo.board_api_token.get_secret_value() if direct_repo else ""
    mill_timeout = direct_repo.timeout if direct_repo else 30.0

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

    async def read_diagnostic_events(
        board_id: str = "",
        category: str = "",
        since: str = "",
        limit: int = 100,
        event_type: str = "",
        until: str = "",
    ) -> str:
        """Read the mill's diagnostic events (``GET /diagnostic-events``).

        Queries the mill board API directly — the mill's event store lives
        in the mill's data volume, which chat does not mount, so this tool
        must go through the API (until 2026-09-07 it read a local file
        path that never existed inside the chat container and always
        answered "No mill diagnostic events file found").

        Args:
            board_id: Optional board / repo filter (e.g. ``hexarchy``).
            category: Optional event category filter (e.g. ``CI_FAILURE``,
                ``OPS_CLASSIFY``, ``CI_FIX_RESOLVED``).  Omit for all.
            since: Optional ISO-8601 lower bound (inclusive), forwarded to
                the mill.
            limit: Maximum number of events to return.  Default ``100``.
            event_type: Deprecated alias of *category*.
            until: Optional ISO-8601 upper bound (inclusive), applied
                client-side.

        Returns:
            Formatted listing of matching diagnostic events, or an error
            message when the mill is unreachable.

        """
        if not mill_base_url:
            return (
                "Mill board API is not configured "
                "(direct_repo.board_api_base_url) — cannot read diagnostic events."
            )

        category = (category or event_type).strip()
        until_dt: datetime | None = None
        try:
            if until:
                until_dt = datetime.fromisoformat(until)
            if since:
                datetime.fromisoformat(since)
        except ValueError as exc:
            return f"Invalid ISO-8601 timestamp: {exc}"

        params: dict[str, str] = {"limit": str(max(1, limit))}
        if board_id.strip():
            params["board_id"] = board_id.strip()
        if category:
            params["category"] = category
        if since:
            params["since"] = since

        headers: dict[str, str] = {"Accept": "application/json"}
        if mill_token:
            headers["Authorization"] = f"Bearer {mill_token}"
        url = f"{mill_base_url}/diagnostic-events"
        try:
            async with httpx.AsyncClient(timeout=mill_timeout) as client:
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            logger.warning("read_diagnostic_events: %s", exc)
            return f"Could not read diagnostic events from {url}: {exc}"

        events = payload.get("events", []) if isinstance(payload, dict) else payload
        if not isinstance(events, list):
            kind = type(payload).__name__
            return f"Unexpected /diagnostic-events response shape: {kind}"

        matched: list[dict[str, Any]] = []
        for obj in events:
            if not isinstance(obj, dict):
                continue
            if until_dt is not None:
                ts_raw = obj.get("timestamp") or obj.get("created_at") or ""
                try:
                    if datetime.fromisoformat(str(ts_raw)) > until_dt:
                        continue
                except ValueError, TypeError:
                    pass
            matched.append(obj)

        if not matched:
            parts = ["No matching diagnostic events."]
            if board_id:
                parts.append(f"board_id: {board_id}")
            if category:
                parts.append(f"category: {category}")
            if since:
                parts.append(f"since: {since}")
            if until:
                parts.append(f"until: {until}")
            return " ".join(parts)

        lines_out: list[str] = []
        for i, obj in enumerate(matched, start=1):
            ev_type = obj.get("category") or obj.get("type") or "(no type)"
            ev_ts = obj.get("timestamp") or obj.get("created_at") or "(no timestamp)"
            ev_msg = obj.get("reason") or obj.get("message") or "(no detail)"
            where = " ".join(
                f"{k}={obj[k]}" for k in ("repo_id", "ticket_id") if obj.get(k)
            )
            lines_out.append(f"[{i}] {ev_type} @ {ev_ts} {where}\n  {ev_msg}")
        if len(matched) >= limit:
            lines_out.append(f"\n(result truncated to {limit} events)")
        return "\n".join(lines_out)

    return [
        list_diagnostic_events,
        check_recurring_categories,
        list_fix_proposals,
        apply_fix,
        reject_fix,
        list_effectiveness_reports,
        read_diagnostic_events,
    ]

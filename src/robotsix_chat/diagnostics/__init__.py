"""Blocked-ticket diagnostics capture, categorisation, and tooling.

Exposes :func:`build_diagnostics_tools` — a factory returning the LLM tools
that let the assistant inspect captured diagnostic records.  Returns no tools
when diagnostics is disabled.

The :class:`DiagnosticCapture` polls the board for BLOCKED state transitions
and records diagnostic bundles; this module wires the agent-facing tools.

Also exposes the categorisation engine: :class:`FailureCategory`,
:func:`categorize_record` (inline auto-categorisation during capture), and
:func:`recategorize_blocked_event` (agent tool for manual override).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from robotsix_chat.diagnostics.categories import FailureCategory, categorize_record
from robotsix_chat.diagnostics.models import DiagnosticRecord

if TYPE_CHECKING:
    from robotsix_chat.config import DiagnosticsSettings

logger = logging.getLogger(__name__)

__all__ = [
    "DiagnosticRecord",
    "FailureCategory",
    "build_diagnostics_tools",
    "categorize_record",
    "recategorize_blocked_event",
]


def recategorize_blocked_event(ticket_id: str, new_category: str) -> str:
    """Agent tool: manually override the category of a blocked event.

    Args:
        ticket_id: The id of the blocked ticket to re-categorise.
        new_category: The new ``FailureCategory`` value
            (``CLONE_TARGET``, ``CI_FAILURE``, ``DEPENDENCY``,
            ``REFINEMENT``, or ``OTHER``).

    Returns:
        A confirmation string, or an error message when the ticket id is
        unknown or *new_category* is invalid.

    This is a **tool** meant to be wired into the LLM agent; it mutates the
    diagnostic record store in place.  At this stage the store is not yet
    wired (see the capture sibling ticket), so this function hands off to a
    stub that raises ``NotImplementedError`` until the store is available.

    """
    # Validate the category string.
    try:
        _cat = FailureCategory(new_category.upper())
    except ValueError:
        return (
            f"Error: '{new_category}' is not a valid failure category. "
            f"Choose from: {', '.join(c.value for c in FailureCategory)}"
        )

    # TODO: wire into the diagnostic record store once the capture sibling
    # ticket lands the persistence layer.
    logger.warning(
        "recategorize_blocked_event: store not wired yet — ticket %s "
        "cannot be re-categorised to %s until the diagnostic store is "
        "available.",
        ticket_id,
        new_category,
    )
    return (
        f"Error: the diagnostics store is not yet available. "
        f"Cannot re-categorise ticket '{ticket_id}' to '{_cat.value}'."
    )


def build_diagnostics_tools(
    settings: DiagnosticsSettings,
) -> list[Callable[..., Any]]:
    """Return diagnostics tool(s) for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    from .store import DiagnosticStore

    store = DiagnosticStore(path=settings.data_dir)

    async def list_diagnostic_records() -> str:
        """List all captured diagnostic records for BLOCKED tickets.

        Returns a human-readable summary of every captured diagnostic
        bundle — ticket id, block reason, and trace URL.  Use this to
        answer questions about why tickets are blocked or what the
        investigation state is.

        Returns:
            A formatted text summary of all diagnostic records.

        """
        records = store.list()
        if not records:
            return "No diagnostic records captured."

        lines = [f"{len(records)} diagnostic record(s):", ""]
        for r in records:
            lines.append(f"  Ticket: {r.ticket_id}")
            lines.append(f"    Block reason: {r.block_reason[:200]}")
            if r.langfuse_trace:
                lines.append(f"    Trace: {r.langfuse_trace}")
            lines.append(f"    Category: {r.effective_category}")
            lines.append(f"    Captured: {r.captured_at}")
            lines.append("")
        return "\n".join(lines)

    return [list_diagnostic_records, recategorize_blocked_event]

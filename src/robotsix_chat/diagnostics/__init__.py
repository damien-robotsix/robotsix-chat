"""Blocked-ticket diagnostics capture and tooling.

Exposes :func:`build_diagnostics_tools` — a factory returning the LLM tools
that let the assistant inspect captured diagnostic records.  Returns no tools
when diagnostics is disabled.

The :class:`DiagnosticCapture` polls the board for BLOCKED state transitions
and records diagnostic bundles; this module only wires the agent-facing tools.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DiagnosticsSettings

logger = logging.getLogger(__name__)

__all__ = ["build_diagnostics_tools"]


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
            lines.append(f"    Captured: {r.captured_at}")
            lines.append("")
        return "\n".join(lines)

    return [list_diagnostic_records]

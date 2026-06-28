"""Data models for the diagnostics subsystem.

Defines :class:`DiagnosticRecord` — the bundle captured at BLOCKED transitions
that carries the block reason, diagnostic fields, and category.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class DiagnosticRecord:
    """A diagnostic bundle captured when a ticket transitions to BLOCKED.

    Attributes:
        ticket_id: The ticket that was blocked.
        block_reason: Human-written reason the agent gave for the block.
        langfuse_trace: Langfuse trace reference extracted from ticket comments.
        ticket_history: Full ticket data as JSON string.
        branch_pr_links: GitHub PR/issue links found in ticket.
        clone_repo_info: Clone-target / repo-mapping info from ticket metadata.
        category: Auto-assigned failure category (may be overridden via
            ``category_override``).
        category_override: Manual re-categorization.  When set, it takes
            precedence over the auto-assigned ``category``.
        captured_at: ISO-8601 timestamp of capture.
        extra: Arbitrary additional diagnostic fields (repo id, branch name,
            ticket title, etc.).

    """

    ticket_id: str
    block_reason: str
    langfuse_trace: str = ""
    ticket_history: str = ""
    branch_pr_links: str = ""
    clone_repo_info: str = ""
    category: str = "OTHER"
    category_override: str | None = None
    captured_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_category(self) -> str:
        """The resolved category: override, or the auto-assigned one."""
        return self.category_override or self.category

"""Diagnostic categorisation engine.

Auto-categorises :class:`DiagnosticRecord` bundles into a fixed set of failure
categories using deterministic keyword/regex rules on the block reason string
and diagnostic extra fields.  Manual overrides are supported via the
``category_override`` field on the record.
"""

from __future__ import annotations

import enum
import logging
import re
from typing import ClassVar

from robotsix_chat.diagnostics.models import DiagnosticRecord

logger = logging.getLogger(__name__)

# -- Category enum -----------------------------------------------------------


class FailureCategory(enum.StrEnum):
    """Fixed failure categories for BLOCKED tickets."""

    CLONE_TARGET = "CLONE_TARGET"
    """Clone-target / repo-mapping / registration failures."""

    CI_FAILURE = "CI_FAILURE"
    """Branch / PR / CI / build failures."""

    DEPENDENCY = "DEPENDENCY"
    """Blocked on another ticket / missing SHA / dependency."""

    REFINEMENT = "REFINEMENT"
    """Refinement loop failures (refine / implement stalled)."""

    OTHER = "OTHER"
    """Fallback — no specific rule matched."""


# -- Rule definition ----------------------------------------------------------


# Each rule is ``(regex_pattern, category)``.  Patterns are matched
# case-insensitively against the block reason string.  Matching stops at the
# first hit; order matters.
_RULES: list[tuple[str, FailureCategory]] = [
    # CLONE_TARGET — clone, repo mapping, registration
    (
        r"clone|repo\s*(mapping|registration|target|setup)|"
        r"registration|target\s*repo|missing\s*(repo|repository)",
        FailureCategory.CLONE_TARGET,
    ),
    # DEPENDENCY — blocked on another ticket, missing SHA, dependency chain.
    # Checked BEFORE CI_FAILURE so "blocked on ticket — CI failed" is
    # categorised as DEPENDENCY, not CI_FAILURE.
    (
        r"blocked\s*(on|by)\s*(ticket|#)|depends?\s*on\b|dependency\b|"
        r"prerequisite\b|missing\s*(sha|commit|hash)|"
        r"waiting\s*for\s*ticket",
        FailureCategory.DEPENDENCY,
    ),
    # REFINEMENT — refinement / implement loop failures.
    # Checked BEFORE CI_FAILURE so "implement stuck on test failure" is
    # REFINEMENT, not CI_FAILURE.
    (
        r"refine(ment)?\s*(loop|fail|stuck|error)|"
        r"implement\s*(loop|fail|stuck|error)|"
        r"refinement\b",
        FailureCategory.REFINEMENT,
    ),
    # CI_FAILURE — branch, PR, CI, build, workflow, test failures
    (
        r"ci\b|continuous\s*integration|build\s*(fail|error|broken)|"
        r"workflow\s*(fail|error|broken)|branch\s*(protect|rule|fail)|"
        r"pull\s*request|pr\b|merge\s*(conflict|fail)|"
        r"test\s*(fail|suite|run)|pytest\s*fail",
        FailureCategory.CI_FAILURE,
    ),
]


# -- Categorizer --------------------------------------------------------------


class Categorizer:
    """Deterministic categoriser for diagnostic records.

    Matches a set of ordered regex patterns against ``record.block_reason``
    (case-insensitive) and assigns the first matching category.  Falls back to
    :attr:`FailureCategory.OTHER` when no rule matches.

    Patterns are class-level (shared across all instances) and designed to be
    simple, explicit, and reproducible — no machine learning, no randomness.
    """

    _rules: ClassVar[list[tuple[str, FailureCategory]]] = _RULES

    @classmethod
    def categorise(cls, record: DiagnosticRecord) -> str:
        """Assign a category to *record* and return the category name.

        The assigned category is written to ``record.category`` so callers
        can persist the record afterward.  If ``record.category_override`` is
        set, it is left untouched (the override always takes precedence via
        ``record.effective_category``).

        Returns:
            The category string (one of the ``FailureCategory`` values).

        """
        reason = record.block_reason.lower()
        for pattern, category in cls._rules:
            if re.search(pattern, reason):
                record.category = category.value
                logger.debug(
                    "Categorised %s as %s (matched %r)",
                    record.ticket_id,
                    category.value,
                    pattern,
                )
                return category.value

        record.category = FailureCategory.OTHER.value
        logger.debug(
            "Categorised %s as OTHER (no rule matched)",
            record.ticket_id,
        )
        return FailureCategory.OTHER.value


# -- Convenience function -----------------------------------------------------


def categorize_record(record: DiagnosticRecord) -> str:
    """Auto-categorise *record* and return the assigned category.

    Called inline during capture (before persistence) so every record lands
    with a category assigned.  Use ``recategorize_blocked_event`` to override
    later.

    Returns:
        The category string (one of the ``FailureCategory`` values).

    """
    return Categorizer.categorise(record)

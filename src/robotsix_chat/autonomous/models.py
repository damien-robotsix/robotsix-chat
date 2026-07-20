"""Autonomous session state models."""

from __future__ import annotations

from enum import StrEnum


class AutonomousState(StrEnum):
    """Lifecycle state of an autonomous session."""

    SELECTING_SUBJECT = "selecting_subject"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"


AUTONOMOUS_KIND = "autonomous"
DEFAULT_KIND = "chat"
VALID_KINDS = frozenset({"chat", "autonomous"})
AUTONOMOUS_TITLE_PREFIX = "[AUTONOMOUS] "

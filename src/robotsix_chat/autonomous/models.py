"""Data model for autonomous session state tracking."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class AutonomousState(enum.StrEnum):
    """Lifecycle states for an autonomous session."""

    planning = "planning"
    proposal = "proposal"
    executing = "executing"
    completed = "completed"


@dataclass
class AutonomousSession:
    """Runtime metadata for one autonomous session."""

    session_id: str
    owner_id: str
    state: AutonomousState = AutonomousState.planning
    plan_text: str = ""
    auto_turn_count: int = 0
    consecutive_no_change: int = 0
    completion_suppressed: bool = False
    rejected_subjects: list[str] | None = None
    recent_user_messages: list[str] | None = None
    definition_name: str = ""
    """Name of the session definition that spawned this session."""

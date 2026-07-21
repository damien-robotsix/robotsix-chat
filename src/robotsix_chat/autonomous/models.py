"""Data model for autonomous session state tracking."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class AutonomousState(enum.StrEnum):
    """Lifecycle states for an autonomous session."""

    selecting_subject = "selecting_subject"
    awaiting_approval = "awaiting_approval"
    executing = "executing"
    completed = "completed"


@dataclass
class AutonomousSession:
    """Runtime metadata for one autonomous session."""

    session_id: str
    owner_id: str
    state: AutonomousState = AutonomousState.selecting_subject
    plan_text: str = ""
    auto_turn_count: int = 0

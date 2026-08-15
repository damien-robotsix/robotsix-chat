"""Data model for autonomous session state tracking."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class AutonomousState(enum.StrEnum):
    """Lifecycle states for an autonomous session."""

    executing = "executing"
    completed = "completed"


@dataclass
class AutonomousSession:
    """Runtime metadata for one autonomous session."""

    session_id: str
    owner_id: str
    state: AutonomousState = AutonomousState.executing
    auto_turn_count: int = 0
    definition_name: str = ""
    """Name of the session definition that spawned this session."""

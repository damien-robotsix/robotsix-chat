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
    consecutive_no_change: int = 0
    completion_suppressed: bool = False
    definition_name: str = ""
    """Name of the session definition that spawned this session."""
    last_board_digest: str = ""
    """SHA-256 digest of the board content seen on the previous run.

    Used on restart/resumption to detect that the board state is unchanged
    and avoid re-running the board content check + emitting a duplicate
    digest.  Empty on a fresh session (no prior snapshot yet)."""

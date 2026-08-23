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
    reopened_by_operator: bool = False
    """Whether this session was pulled back out of ``completed`` by a human.

    Its autonomous run is already finished; it is held open only because the
    operator is talking to it.  The runner uses this to tell such a session
    from one still doing its scheduled work, so an abandoned conversation
    eventually releases the preset instead of stalling it forever.
    """
    last_operator_turn_at: float = 0.0
    """Wall-clock time of the last operator-initiated turn, 0.0 if never.

    Distinct from the conversation store's ``wall_last_active``, which the
    runner's own recorded turns also bump.  This tracks *human* activity
    only, so the retire sweep can tell a session someone is chatting with
    from one that merely finished its run recently.
    """

"""Unified subsession system — background agents spawned from chat sessions.

Replaces the former ``delegate_task`` background tasks, check loops, and
pending-questions systems with one model.  See :mod:`.models` for the
kinds and lifecycle, :mod:`.worker` for the turn loop, and :mod:`.tools`
for the agent-facing tool factory.
"""

from .delivery import ParentDelivery
from .models import (
    ACTIVE_STATUSES,
    InboxMessage,
    SubsessionCapacityError,
    SubsessionDepthError,
    SubsessionInfo,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionLevelError,
    SubsessionPeriodicSpawnError,
    SubsessionStatus,
    TranscriptEntry,
)
from .registry import SubsessionRegistry
from .resume import resume_subsessions
from .tools import build_subsession_tools
from .worker import (
    CloseState,
    SubsessionContext,
    SubsessionEnv,
    spawn_subsession,
)

__all__ = [
    "ACTIVE_STATUSES",
    "CloseState",
    "InboxMessage",
    "ParentDelivery",
    "SubsessionCapacityError",
    "SubsessionContext",
    "SubsessionDepthError",
    "SubsessionEnv",
    "SubsessionInfo",
    "SubsessionIntervalError",
    "SubsessionKind",
    "SubsessionLevelError",
    "SubsessionPeriodicSpawnError",
    "SubsessionRegistry",
    "SubsessionStatus",
    "TranscriptEntry",
    "build_subsession_tools",
    "resume_subsessions",
    "spawn_subsession",
]

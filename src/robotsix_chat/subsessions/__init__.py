"""Unified subsession system — background agents spawned from chat sessions.

Replaces the former ``delegate_task`` background tasks, check loops, and
pending-questions systems with one model.  See :mod:`.models` for the
kinds and lifecycle, :mod:`.worker` for the turn loop, and :mod:`.tools`
for the agent-facing tool factory.
"""

from pathlib import Path

from .delivery import ParentDelivery
from .models import (
    ACTIVE_STATUSES,
    InboxMessage,
    SubsessionCapacityError,
    SubsessionDedupError,
    SubsessionDepthError,
    SubsessionInfo,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionLevelError,
    SubsessionPeriodicSpawnError,
    SubsessionStatus,
    SubsessionUserChatSpawnError,
    TranscriptEntry,
)
from .registry import SubsessionRegistry
from .resume import resume_subsessions
from .tools import build_subsession_tools
from .watcher import watch_paused_monitors
from .worker import (
    CloseState,
    SubsessionContext,
    SubsessionEnv,
    spawn_subsession,
)


def load_subsessions_skill() -> str:
    """Return the subsessions component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.

    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


__all__ = [
    "ACTIVE_STATUSES",
    "CloseState",
    "InboxMessage",
    "ParentDelivery",
    "SubsessionCapacityError",
    "SubsessionContext",
    "SubsessionDedupError",
    "SubsessionDepthError",
    "SubsessionEnv",
    "SubsessionInfo",
    "SubsessionIntervalError",
    "SubsessionKind",
    "SubsessionLevelError",
    "SubsessionPeriodicSpawnError",
    "SubsessionRegistry",
    "SubsessionStatus",
    "SubsessionUserChatSpawnError",
    "TranscriptEntry",
    "build_subsession_tools",
    "load_subsessions_skill",
    "resume_subsessions",
    "spawn_subsession",
    "watch_paused_monitors",
]

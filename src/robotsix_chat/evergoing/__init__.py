"""Evergoing session: activation + periodic summarising compaction scheduler.

Wires the storage-layer evergoing/compaction primitives on
:class:`robotsix_chat.chat.conversation.ConversationStore` into the running
app: an activation path (create-on-boot behind ``evergoing.enabled``) and a
background scheduler that folds everything before the last few runs into
the session summary on a deterministic gate (interval + fresh-run count).
"""

from __future__ import annotations

from robotsix_chat.evergoing.cross_session_tools import (
    build_cross_session_tools,
    load_cross_session_skill,
)
from robotsix_chat.evergoing.scheduler import EvergoingSummaryScheduler

__all__ = [
    "EvergoingSummaryScheduler",
    "build_cross_session_tools",
    "load_cross_session_skill",
]

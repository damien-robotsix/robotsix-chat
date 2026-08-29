"""Evergoing session: activation + periodic subject-aware trim scheduler.

Wires the storage-layer evergoing/trim primitives on
:class:`robotsix_chat.chat.conversation.ConversationStore` into the running
app: an activation path (create-on-boot behind ``evergoing.enabled``) and a
background scheduler that trims finished, off-subject leading turns.
"""

from __future__ import annotations

from robotsix_chat.evergoing.decision import (
    TrimDecision,
    decide_trim,
    parse_trim_decision,
)
from robotsix_chat.evergoing.scheduler import EvergoingTrimScheduler

__all__ = [
    "EvergoingTrimScheduler",
    "TrimDecision",
    "decide_trim",
    "parse_trim_decision",
]

"""Periodic summarising compaction scheduler (config block ``evergoing``).

Wires the storage-layer compaction primitives on
:class:`robotsix_chat.chat.conversation.ConversationStore` into the running
app: a background scheduler that folds everything before the last few runs
into the session summary on a deterministic gate (interval + fresh-run
count) and pushes each summary to the memory component.

The package keeps its historical name; the "evergoing session" it once
activated (one never-ending operator session with cross-session tools) was
removed on 2026-09-15.
"""

from __future__ import annotations

from robotsix_chat.evergoing.scheduler import EvergoingSummaryScheduler

__all__ = ["EvergoingSummaryScheduler"]

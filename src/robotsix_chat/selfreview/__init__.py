"""Self-review tools — a read-only digest of live conversation activity.

Exposes :func:`build_recent_activity_tools` — a factory that returns a
single ``read_recent_activity`` tool when enabled, or ``[]`` otherwise.
The tool reads from the in-process :class:`ConversationStore`
(short-lived per-client conversation turns), producing a human-readable
multi-session digest.

This package is **independent of** the optional cognee episodic memory
subsystem (``src/robotsix_chat/memory/``).  cognee automatically recalls
whole past conversations by similarity; the tool here is a deliberate,
explicit, read-only snapshot of the live conversation store — no
embeddings, no persistence writes, no external service.
"""

from __future__ import annotations

from .read import build_recent_activity_tools

__all__ = ["build_recent_activity_tools"]

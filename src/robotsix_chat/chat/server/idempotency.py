"""Per-session idempotency registry for agent runs.

Stores completed (message_id → reply) per session in an LRU-capped
OrderedDict so unbounded memory growth is impossible.  No locking is
needed — asyncio is single-threaded.
"""

from __future__ import annotations

from collections import OrderedDict


class MessageIdempotencyStore:
    """Per-session idempotency registry for agent runs.

    Stores completed (message_id → reply) per session in an LRU-capped
    OrderedDict so unbounded memory growth is impossible.
    No locking is needed — asyncio is single-threaded.
    """

    def __init__(self, max_per_session: int = 100) -> None:
        self._max = max_per_session
        # session_id → OrderedDict[message_id, reply]
        self._store: dict[str, OrderedDict[str, str]] = {}

    def get_reply(self, session_id: str, message_id: str) -> str | None:
        """Return the recorded reply, or None if not yet completed."""
        return self._store.get(session_id, {}).get(message_id)

    def mark_completed(
        self, session_id: str, message_id: str, reply: str
    ) -> None:
        """Record the completed reply; evict oldest entry when over cap."""
        if session_id not in self._store:
            self._store[session_id] = OrderedDict()
        sess = self._store[session_id]
        sess[message_id] = reply
        while len(sess) > self._max:
            sess.popitem(last=False)

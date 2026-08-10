"""In-memory cache of last-known ticket state.

Populated from mill push events (``POST /mill-events``) and successful
``ticket_poll`` / ``ticket_poll_batch`` calls.  Provides a fallback when
the board API is unreachable so the agent can surface last-known state
with a clear staleness caveat rather than returning "I can't confirm."
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Maximum age for a cached entry before it is considered stale (seconds).
# Entries older than this are still returned but carry a stronger caveat.
_MAX_CACHE_AGE_SECONDS: float = 3600.0  # 1 hour


class TicketStateCache:
    """In-memory cache of last-known ticket state.

    Single-threaded asyncio usage — dict operations are safe without a lock
    because the event loop does not preempt coroutines mid-opcode.
    """

    def __init__(self) -> None:
        """Initialize an empty cache."""
        self._entries: dict[str, dict[str, Any]] = {}

    # -- public API --------------------------------------------------------

    def put(
        self,
        ticket_id: str,
        state: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Store the last-known *state* for *ticket_id*."""
        self._entries[ticket_id] = {
            "ticket_id": ticket_id,
            "state": state,
            "data": data or {},
            "cached_at": time.time(),
        }

    def get(self, ticket_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """Return ``(entry, caveat)`` for *ticket_id*, or ``(None, None)``.

        *entry* is a shallow copy of the cached dict.  *caveat* is a
        human-readable note about the data source and staleness; it is
        always present when *entry* is not ``None``.
        """
        raw = self._entries.get(ticket_id)
        if raw is None:
            return None, None

        age = time.time() - raw["cached_at"]
        freshness = "stale" if age > _MAX_CACHE_AGE_SECONDS else "cached"

        caveat = (
            f"[last-known state — board API unreachable; "
            f"showing {freshness} state from {age:.0f}s ago]"
        )
        return dict(raw), caveat

    def put_from_mill_event(self, event_payload: dict[str, object]) -> None:
        """Store state from a mill push event payload."""
        ticket_id = str(event_payload.get("ticket_id", ""))
        if not ticket_id:
            return
        new_state = str(event_payload.get("new_state", ""))
        extra: dict[str, Any] = {
            "old_state": str(event_payload.get("old_state", "")),
            "board_id": str(event_payload.get("board_id", "")),
            "repo_id": str(event_payload.get("repo_id", "")),
            "timestamp": str(event_payload.get("timestamp", "")),
        }
        self.put(ticket_id, new_state, data=extra)
        logger.debug(
            "ticket_state_cache: stored %s → %r (from mill event)",
            ticket_id,
            new_state,
        )

    def put_from_poll(self, ticket_id: str, poll_result: dict[str, Any]) -> None:
        """Store state from a successful poll result."""
        state = poll_result.get("state")
        if state is None:
            return
        self.put(ticket_id, str(state), data=dict(poll_result))


# Module-level singleton — shared across ticket_poll tools, mill_events
# endpoint, and any future consumers that need last-known ticket state.
ticket_state_cache = TicketStateCache()

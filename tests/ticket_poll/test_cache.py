"""Tests for the ticket-state cache (``ticket_poll.cache``)."""

from __future__ import annotations

import time

import pytest

from robotsix_chat.ticket_poll.cache import TicketStateCache, ticket_state_cache


class TestTicketStateCache:
    """Unit tests for the TicketStateCache class."""

    def test_put_and_get_basic(self) -> None:
        """Put an entry, get it back with a caveat."""
        cache = TicketStateCache()
        cache.put("ticket-1", "IN_PROGRESS")
        entry, caveat = cache.get("ticket-1")
        assert entry is not None
        assert entry["ticket_id"] == "ticket-1"
        assert entry["state"] == "IN_PROGRESS"
        assert entry["cached_at"] > 0
        assert caveat is not None
        assert "board API unreachable" in caveat

    def test_get_missing_returns_none(self) -> None:
        """Missing tickets return (None, None)."""
        cache = TicketStateCache()
        entry, caveat = cache.get("nonexistent")
        assert entry is None
        assert caveat is None

    def test_stale_entry_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Entries older than 1 hour are flagged as stale."""
        cache = TicketStateCache()
        cache.put("ticket-1", "DONE")
        # Advance time by 2 hours.
        fake_now = time.time() + 7200.0
        monkeypatch.setattr(time, "time", lambda: fake_now)
        entry, caveat = cache.get("ticket-1")
        assert entry is not None
        assert "stale" in caveat

    def test_put_overwrites_previous(self) -> None:
        """Putting the same ticket ID overwrites the old entry."""
        cache = TicketStateCache()
        cache.put("ticket-1", "IN_PROGRESS")
        cache.put("ticket-1", "DONE")
        entry, _caveat = cache.get("ticket-1")
        assert entry is not None
        assert entry["state"] == "DONE"

    # -- put_from_mill_event --------------------------------------------------

    def test_put_from_mill_event_stores_state(self) -> None:
        """A mill event populates the cache correctly."""
        cache = TicketStateCache()
        event: dict[str, object] = {
            "ticket_id": "20250101T120000Z-my-ticket-a1b2",
            "old_state": "IN_PROGRESS",
            "new_state": "BLOCKED",
            "board_id": "board-1",
            "repo_id": "my-repo",
            "timestamp": "2025-01-01T12:00:00Z",
        }
        cache.put_from_mill_event(event)
        entry, caveat = cache.get("20250101T120000Z-my-ticket-a1b2")
        assert entry is not None
        assert entry["state"] == "BLOCKED"
        assert entry["data"]["old_state"] == "IN_PROGRESS"
        assert entry["data"]["board_id"] == "board-1"
        assert caveat is not None

    def test_put_from_mill_event_empty_id_noop(self) -> None:
        """Empty ticket_id is a no-op."""
        cache = TicketStateCache()
        cache.put_from_mill_event({"ticket_id": "", "new_state": "DONE"})
        entry, _caveat = cache.get("")
        assert entry is None

    # -- put_from_poll --------------------------------------------------------

    def test_put_from_poll_stores_state(self) -> None:
        """A successful poll result populates the cache."""
        cache = TicketStateCache()
        result = {
            "ticket_id": "ticket-2",
            "state": "APPROVED",
            "error": "",
        }
        cache.put_from_poll("ticket-2", result)
        entry, _caveat = cache.get("ticket-2")
        assert entry is not None
        assert entry["state"] == "APPROVED"

    def test_put_from_poll_null_state_noop(self) -> None:
        """A poll result with state=None is a no-op."""
        cache = TicketStateCache()
        cache.put_from_poll("ticket-3", {"ticket_id": "ticket-3", "state": None})
        entry, _caveat = cache.get("ticket-3")
        assert entry is None

    # -- module-level singleton -----------------------------------------------

    def test_singleton_is_ticket_state_cache_instance(self) -> None:
        """The module-level singleton is a TicketStateCache."""
        assert isinstance(ticket_state_cache, TicketStateCache)

    def test_singleton_is_shared(self) -> None:
        """Multiple imports return the same instance."""
        from robotsix_chat.ticket_poll import cache as m1
        from robotsix_chat.ticket_poll import cache as m2

        assert m1.ticket_state_cache is m2.ticket_state_cache

"""Unit tests for ``MessageIdempotencyStore``."""

from __future__ import annotations

from robotsix_chat.chat.server.idempotency import MessageIdempotencyStore


class TestMessageIdempotencyStore:
    """Tests for the per-session LRU reply cache."""

    def test_get_reply_unknown_session_returns_none(self) -> None:
        """An unknown session + message_id pair returns ``None``."""
        store = MessageIdempotencyStore()
        assert store.get_reply("ghost", "m1") is None

    def test_mark_and_retrieve_single_session(self) -> None:
        """Storing and retrieving a reply for a single session works."""
        store = MessageIdempotencyStore()
        store.mark_completed("s1", "m1", "reply-a")
        assert store.get_reply("s1", "m1") == "reply-a"

    def test_mark_and_retrieve_multiple_sessions(self) -> None:
        """Replies for different sessions are isolated from each other."""
        store = MessageIdempotencyStore()
        store.mark_completed("s1", "m1", "reply-a")
        store.mark_completed("s2", "m2", "reply-b")
        assert store.get_reply("s1", "m1") == "reply-a"
        assert store.get_reply("s2", "m2") == "reply-b"

    def test_lru_eviction_when_over_cap(self) -> None:
        """Entries beyond ``max_per_session`` evict the oldest (LRU)."""
        store = MessageIdempotencyStore(max_per_session=100)
        # Insert 101 entries for one session
        for i in range(101):
            store.mark_completed("s1", f"m{i}", f"reply-{i}")
        # The oldest (m0) should be evicted
        assert store.get_reply("s1", "m0") is None
        # The newest (m100) should still be present
        assert store.get_reply("s1", "m100") == "reply-100"

    def test_distinct_sessions_dont_interfere(self) -> None:
        """A message_id from one session is not visible in another."""
        store = MessageIdempotencyStore()
        store.mark_completed("s1", "m1", "reply-a")
        # Same message_id in a different session should not be found
        assert store.get_reply("s2", "m1") is None
        # Original session still has its entry
        assert store.get_reply("s1", "m1") == "reply-a"

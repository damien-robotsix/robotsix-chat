"""Tests for the self-review tools.

Covers ``recent_activity()`` on ``ConversationStore`` and
``build_recent_activity_tools``.

* ``recent_activity()`` ordering, ``limit``, ``max_turns``, multi-client,
  and read-only (non-mutating) discipline.
* ``build_recent_activity_tools`` gating (disabled, store-is-None, enabled).
* Tool invocation producing a digest string containing known content.
"""

from __future__ import annotations

import pytest

from robotsix_chat.chat.conversation import ConversationStore, _OwnerState
from robotsix_chat.config import SelfReviewSettings
from robotsix_chat.selfreview import build_recent_activity_tools

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_store(store: ConversationStore) -> None:
    """Seed *store* with conversations for two clients across two sessions.

    Sessions are recorded via the public ``begin``/``record`` API.  Owner
    state is registered explicitly so ``recent_activity()`` can resolve
    ``client_id`` — in normal server operation owners are created by the
    ``/sessions`` or ``create_session()`` endpoints before ``record()``
    targets them.
    """
    # Session a1 (client-a, oldest)
    sid_a1, _ = store.begin("a1")
    store.record(sid_a1, "client-a", "hello from A", "hi A!")
    store.record(sid_a1, "client-a", "how are you A", "good A")

    # Session b1 (client-b)
    sid_b1, _ = store.begin("b1")
    store.record(sid_b1, "client-b", "hello from B", "hi B!")

    # Session a2 (client-a, most recent)
    sid_a2, _ = store.begin("a2")
    store.record(sid_a2, "client-a", "another session A", "ok A")

    # Register owners so recent_activity() can resolve client_id from owner_id.
    store._owners["client-a"] = _OwnerState(
        active_session_id=sid_a2, session_ids={"a1", "a2"}
    )
    store._owners["client-b"] = _OwnerState(
        active_session_id=sid_b1, session_ids={"b1"}
    )


# ---------------------------------------------------------------------------
# ConversationStore.recent_activity()
# ---------------------------------------------------------------------------


class TestRecentActivity:
    """Unit tests for ``ConversationStore.recent_activity()``."""

    def test_returns_empty_for_new_store(self) -> None:
        """``recent_activity()`` on an unseeded store returns ``[]``."""
        store = ConversationStore()
        assert store.recent_activity() == []

    def test_returns_most_recent_active_first(self) -> None:
        """Entries are ordered most-recently-active first."""
        store = ConversationStore()
        _seed_store(store)
        entries = store.recent_activity()
        # Most recently active session first (a2 was last to record)
        assert len(entries) == 3
        assert entries[0]["session_id"] == "a2"
        assert entries[1]["session_id"] == "b1"
        assert entries[2]["session_id"] == "a1"

    def test_respects_limit(self) -> None:
        """``limit`` parameter caps the number of returned entries."""
        store = ConversationStore()
        _seed_store(store)
        entries = store.recent_activity(limit=1)
        assert len(entries) == 1
        assert entries[0]["session_id"] == "a2"

    def test_respects_max_turns(self) -> None:
        """``max_turns`` truncates each conversation to the last N turns."""
        store = ConversationStore()
        _seed_store(store)
        # Session a1 has 2 turns; ask for max_turns=1
        entries = store.recent_activity(limit=10, max_turns=1)
        # Find a1 in results
        a1 = next(e for e in entries if e["session_id"] == "a1")
        assert len(a1["turns"]) == 1
        assert a1["turns"][0] == ("how are you A", "good A")

    def test_includes_client_id_and_session_id(self) -> None:
        """Each entry has ``client_id`` and ``session_id`` keys."""
        store = ConversationStore()
        _seed_store(store)
        entries = store.recent_activity()
        client_ids = {e["client_id"] for e in entries}
        session_ids = {e["session_id"] for e in entries}
        assert "client-a" in client_ids
        assert "client-b" in client_ids
        assert session_ids == {"a1", "a2", "b1"}

    def test_turns_are_copies_not_references(self) -> None:
        """Returned ``turns`` lists are copies of the original."""
        store = ConversationStore()
        sid, _ = store.begin("s1")
        store.record(sid, "client-x", "msg", "reply")
        entries = store.recent_activity()
        # Mutate the returned turns list
        entries[0]["turns"].append(("mutated", "mutated"))
        # Re-read — original should be unchanged
        entries2 = store.recent_activity()
        assert len(entries2[0]["turns"]) == 1

    def test_does_not_mutate_store_state(self) -> None:
        """``recent_activity()`` does not change session ordering or history."""
        store = ConversationStore()
        _seed_store(store)
        # Snapshot ordering before
        sessions_before = list(store._sessions.keys())
        history_before = store.history("a1")
        # Call recent_activity
        store.recent_activity()
        # Ordering unchanged
        assert list(store._sessions.keys()) == sessions_before
        # history unchanged
        assert store.history("a1") == history_before


# ---------------------------------------------------------------------------
# build_recent_activity_tools — gating
# ---------------------------------------------------------------------------


class TestBuildRecentActivityTools:
    """Tests for ``build_recent_activity_tools`` factory gating."""

    def test_disabled_returns_empty(self) -> None:
        """Returns ``[]`` when ``enabled=False``."""
        settings = SelfReviewSettings(enabled=False)
        store = ConversationStore()
        assert build_recent_activity_tools(settings, store) == []

    def test_settings_none_returns_empty(self) -> None:
        """Returns ``[]`` when *settings* is ``None``."""
        store = ConversationStore()
        assert build_recent_activity_tools(None, store) == []  # type: ignore[arg-type]

    def test_store_none_returns_empty(self) -> None:
        """Returns ``[]`` when *store* is ``None``."""
        settings = SelfReviewSettings(enabled=True)
        assert build_recent_activity_tools(settings, None) == []

    def test_enabled_returns_one_tool(self) -> None:
        """Returns exactly one tool named ``read_recent_activity``."""
        settings = SelfReviewSettings(enabled=True)
        store = ConversationStore()
        tools = build_recent_activity_tools(settings, store)
        assert len(tools) == 1
        assert tools[0].__name__ == "read_recent_activity"


# ---------------------------------------------------------------------------
# Tool invocation
# ---------------------------------------------------------------------------


class TestReadRecentActivityTool:
    """Tests for invoking the returned ``read_recent_activity`` tool."""

    @pytest.mark.asyncio
    async def test_returns_digest_with_known_content(self) -> None:
        """The digest string contains seeded conversation snippets."""
        settings = SelfReviewSettings(enabled=True)
        store = ConversationStore()
        _seed_store(store)
        tools = build_recent_activity_tools(settings, store)
        tool = tools[0]
        result = await tool()
        assert isinstance(result, str)
        assert "hello from A" in result
        assert "hello from B" in result
        assert "another session A" in result
        assert "client-a" in result
        assert "client-b" in result

    @pytest.mark.asyncio
    async def test_returns_no_activity_message_when_empty(self) -> None:
        """Returns a 'no recent activity' message for an empty store."""
        settings = SelfReviewSettings(enabled=True)
        store = ConversationStore()
        tools = build_recent_activity_tools(settings, store)
        result = await tools[0]()
        assert "No recent conversation activity" in result

    @pytest.mark.asyncio
    async def test_clamps_limit_to_settings(self) -> None:
        """The effective limit is clamped to ``recent_activity_limit``."""
        settings = SelfReviewSettings(enabled=True, recent_activity_limit=1)
        store = ConversationStore()
        _seed_store(store)
        tools = build_recent_activity_tools(settings, store)
        result = await tools[0](limit=100)
        # Should only report 1 conversation (clamped to recent_activity_limit)
        assert "## " in result
        assert result.count("## ") == 1

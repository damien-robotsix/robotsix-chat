"""Tests for :class:`ConversationStore` — per-session history with owner grouping."""

from __future__ import annotations

import json
import tempfile
from itertools import count
from pathlib import Path
from typing import cast

from robotsix_chat.chat.conversation import ConversationStore


class _FakeWallClock:
    """A manually advanced wall clock."""

    def __init__(self) -> None:
        self.now = 2000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _store(
    wall_clock: _FakeWallClock | None = None,
    **kwargs: object,
) -> ConversationStore:
    """Build a store with deterministic session ids (``s0``, ``s1``, …)."""
    ids = count()
    return ConversationStore(
        wall_clock=wall_clock or _FakeWallClock(),
        session_factory=lambda: f"s{next(ids)}",
        **kwargs,  # type: ignore[arg-type]
    )


# -- core session tests -------------------------------------------------


def test_first_message_starts_fresh_conversation() -> None:
    """A brand-new session returns empty history."""
    store = _store()

    session_id, history = store.begin("s0")

    assert session_id == "s0"
    assert history == []


def test_consecutive_messages_share_session_and_accumulate_history() -> None:
    """Messages recorded into the same session accumulate and are replayed."""
    store = _store()

    session_id, history = store.begin("s0")
    assert history == []
    store.record("s0", None, "hello", "hi there")

    session_id_2, history_2 = store.begin("s0")

    assert session_id_2 == session_id
    assert history_2 == [("hello", "hi there")]


def test_history_trimmed_to_max_turns() -> None:
    """History keeps only the most recent ``max_history_turns`` turns."""
    store = _store(max_history_turns=2)

    store.begin("s0")
    for i in range(4):
        store.record("s0", None, f"q{i}", f"a{i}")

    _, history = store.begin("s0")
    assert history == [("q2", "a2"), ("q3", "a3")]


def test_separate_sessions() -> None:
    """Different session ids keep separate histories."""
    store = _store()

    store.begin("sa")
    store.record("sa", None, "qa", "aa")
    _, hb = store.begin("sb")

    assert hb == []
    _, ha = store.begin("sa")
    assert ha == [("qa", "aa")]


def test_lru_eviction_bounds_session_count() -> None:
    """Tracking more than ``max_conversations`` sessions evicts the oldest."""
    store = _store(max_conversations=2)

    store.begin("s0")
    store.record("s0", None, "qa", "aa")
    store.begin("s1")
    store.begin("s2")  # evicts the least-recently-used (s0)

    # s0 was evicted — begin recreates it with empty history.
    _, history = store.begin("s0")
    assert history == []


def test_new_session_id_is_unique() -> None:
    """Each ``new_session_id`` call returns a distinct id."""
    store = _store()

    assert store.new_session_id() != store.new_session_id()


def test_returned_history_is_a_copy() -> None:
    """Mutating the returned history must not corrupt stored state."""
    store = _store()

    store.begin("s0")
    store.record("s0", None, "q", "a")
    _, history = store.begin("s0")
    history.append(("injected", "evil"))

    _, history_again = store.begin("s0")
    assert history_again == [("q", "a")]


def test_default_max_history_turns_is_50() -> None:
    """The default cap matches the acceptance criterion of 50 most recent."""
    store = ConversationStore()
    assert store._max_history_turns == 50


# -- owner / multi-session tests ----------------------------------------


def test_list_sessions_returns_owner_sessions() -> None:
    """``list_sessions`` returns all sessions for an owner, sorted by last_active."""
    wall_clock = _FakeWallClock()
    store = _store(wall_clock=wall_clock)

    sid1 = cast(str, store.create_session("c1")["session_id"])
    wall_clock.advance(10)
    sid2 = cast(str, store.create_session("c1")["session_id"])

    sessions, active = store.list_sessions("c1")
    assert len(sessions) == 2
    assert active == sid2  # most recently created is active
    sids = [cast(str, s["session_id"]) for s in sessions]
    assert sids == [sid2, sid1]  # sorted by last_active descending


def test_list_sessions_lazy_creates_default() -> None:
    """``list_sessions`` for an unknown owner lazily creates a default session."""
    store = _store()

    sessions, active = store.list_sessions("new-owner")
    assert len(sessions) == 1
    assert cast(str, sessions[0]["session_id"]) == active
    assert sessions[0]["title"] == "New chat"
    assert sessions[0]["turn_count"] == 0


def test_create_session_marks_active() -> None:
    """``create_session`` returns metadata and marks the session active."""
    store = _store()

    meta = store.create_session("c1")
    sid = cast(str, meta["session_id"])
    _, active = store.list_sessions("c1")
    assert active == sid


def test_record_updates_owner_active() -> None:
    """Recording into a session with an owner_id updates the owner's active session."""
    store = _store()

    sid1 = cast(str, store.create_session("c1")["session_id"])
    sid2 = cast(str, store.create_session("c1")["session_id"])
    # active is sid2 after the second create_session
    _, active = store.list_sessions("c1")
    assert active == sid2

    # record into sid1 with owner_id="c1" makes sid1 active
    store.record(sid1, "c1", "q", "a")
    _, active = store.list_sessions("c1")
    assert active == sid1


def test_record_derives_title_from_first_message() -> None:
    """The first user message in a session becomes the session title."""
    store = _store()

    sid = cast(str, store.create_session("c1")["session_id"])
    store.record(sid, "c1", "Hello world, this is a test", "reply")

    sessions, _ = store.list_sessions("c1")
    assert sessions[0]["title"] == "Hello world, this is a test"


def test_record_for_owner_writes_to_active_session() -> None:
    """``record_for_owner`` records into the owner's active session."""
    store = _store()

    sid = cast(str, store.create_session("c1")["session_id"])
    store.record_for_owner("c1", "hello", "hi")

    assert store.history(sid) == [("hello", "hi")]


def test_two_sessions_independent_history() -> None:
    """Two sessions under the same owner keep independent histories."""
    store = _store()

    sid1 = cast(str, store.create_session("c1")["session_id"])
    sid2 = cast(str, store.create_session("c1")["session_id"])

    store.record(sid1, "c1", "q1", "a1")
    store.record(sid2, "c1", "q2", "a2")

    assert store.history(sid1) == [("q1", "a1")]
    assert store.history(sid2) == [("q2", "a2")]


# -- on-disk persistence -------------------------------------------------


def test_persist_writes_to_file_on_record() -> None:
    """record() writes the full store state to the persist file."""
    wall_clock = _FakeWallClock()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        persist_path = Path(f.name)

    try:
        store = _store(wall_clock=wall_clock, persist_path=persist_path)
        sid = cast(str, store.create_session("c1")["session_id"])
        store.record(sid, "c1", "hello", "hi there")

        raw = json.loads(persist_path.read_text(encoding="utf-8"))
        assert "c1" in raw
        assert raw["c1"]["active_session_id"] == sid
        assert raw["c1"]["sessions"][0]["turns"] == [["hello", "hi there"]]
    finally:
        persist_path.unlink(missing_ok=True)


def test_load_legacy_format_migrates() -> None:
    """Legacy ``{client_id: {session_id, turns}}`` format is migrated on load."""
    data = {
        "c1": {
            "session_id": "old-sid",
            "turns": [["hello", "hi"], ["how", "fine"]],
        }
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f)
        persist_path = Path(f.name)

    try:
        store = _store(persist_path=persist_path)

        sessions, active = store.list_sessions("c1")
        assert active == "old-sid"
        assert len(sessions) == 1
        assert store.history("old-sid") == [("hello", "hi"), ("how", "fine")]
    finally:
        persist_path.unlink(missing_ok=True)


def test_load_current_format_roundtrips() -> None:
    """The current owner→sessions format round-trips through load."""
    data = {
        "c1": {
            "active_session_id": "s1",
            "sessions": [
                {
                    "session_id": "s1",
                    "title": "Test Chat",
                    "last_active": 2000.0,
                    "turn_count": 2,
                    "turns": [["q1", "a1"], ["q2", "a2"]],
                }
            ],
        }
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f)
        persist_path = Path(f.name)

    try:
        store = _store(persist_path=persist_path)

        sessions, active = store.list_sessions("c1")
        assert active == "s1"
        assert len(sessions) == 1
        assert sessions[0]["title"] == "Test Chat"
        assert sessions[0]["turn_count"] == 2
        assert store.history("s1") == [("q1", "a1"), ("q2", "a2")]
    finally:
        persist_path.unlink(missing_ok=True)


def test_persist_roundtrip_preserves_all_metadata() -> None:
    """A store persisted and reloaded preserves all session metadata."""
    wall_clock = _FakeWallClock()

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        persist_path = Path(f.name)

    try:
        store = _store(wall_clock=wall_clock, persist_path=persist_path)
        sid = cast(str, store.create_session("c1")["session_id"])
        store.record(sid, "c1", "First message", "First reply")
        wall_clock.advance(10)
        store.record(sid, "c1", "Second message", "Second reply")

        # Reload into a fresh store using the same persist file.
        store2 = _store(wall_clock=wall_clock, persist_path=persist_path)

        sessions, active = store2.list_sessions("c1")
        assert active == sid
        assert len(sessions) == 1
        s = sessions[0]
        assert s["session_id"] == sid
        assert s["title"] == "First message"
        assert s["turn_count"] == 2
        assert s["last_active"] == wall_clock.now
        assert store2.history(sid) == [
            ("First message", "First reply"),
            ("Second message", "Second reply"),
        ]
    finally:
        persist_path.unlink(missing_ok=True)


def test_load_missing_file_is_graceful() -> None:
    """A missing persist file is not an error — store starts empty."""
    store = _store(persist_path=Path("/nonexistent/conversations.json"))
    session_id, history = store.begin("s0")
    assert history == []


def test_load_trims_to_max_history_turns() -> None:
    """On load, turns beyond max_history_turns are trimmed."""
    data = {
        "c1": {
            "active_session_id": "s",
            "sessions": [
                {
                    "session_id": "s",
                    "title": "Test",
                    "last_active": 2000.0,
                    "turn_count": 3,
                    "turns": [["q0", "a0"], ["q1", "a1"], ["q2", "a2"]],
                }
            ],
        }
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f)
        persist_path = Path(f.name)

    try:
        store = _store(max_history_turns=2, persist_path=persist_path)
        _, history = store.begin("s")
        assert history == [("q1", "a1"), ("q2", "a2")]
    finally:
        persist_path.unlink(missing_ok=True)


def test_load_malformed_json_is_graceful() -> None:
    """Malformed persist file is logged and ignored — store starts empty."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        f.write("not json {")
        persist_path = Path(f.name)

    try:
        store = _store(persist_path=persist_path)
        _, history = store.begin("s0")
        assert history == []
    finally:
        persist_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------------


def test_delete_session_non_active_removes_it() -> None:
    """Deleting a non-active session removes it; active is unchanged."""
    store = _store()
    a = str(store.create_session("o1")["session_id"])
    b = str(store.create_session("o1")["session_id"])  # b becomes active

    result = store.delete_session("o1", a)

    assert result["deleted"] is True
    assert result["active_session_id"] == b
    sessions, active = store.list_sessions("o1")
    assert [s["session_id"] for s in sessions] == [b]
    assert active == b
    assert store.history(a) == []


def test_delete_active_session_reassigns_to_most_recent() -> None:
    """Deleting the active session promotes the most-recently-active remainder."""
    clock = _FakeWallClock()
    store = _store(wall_clock=clock)
    a = str(store.create_session("o1")["session_id"])
    clock.advance(10)
    b = str(store.create_session("o1")["session_id"])  # active, newest
    clock.advance(10)
    # Touch a so it is the most-recently-active of the remaining after b is gone.
    store.record(a, "o1", "hi", "there")

    result = store.delete_session("o1", b)

    assert result["deleted"] is True
    assert result["active_session_id"] == a


def test_delete_last_session_creates_fresh_active() -> None:
    """Deleting the only session yields a fresh empty active session."""
    store = _store()
    only = str(store.create_session("o1")["session_id"])

    result = store.delete_session("o1", only)

    assert result["deleted"] is True
    new_active = result["active_session_id"]
    assert new_active != only
    sessions, active = store.list_sessions("o1")
    assert active == new_active
    assert len(sessions) == 1
    assert sessions[0]["turn_count"] == 0


def test_delete_session_unknown_is_noop() -> None:
    """Deleting an unknown owner/session returns deleted=False."""
    store = _store()
    a = str(store.create_session("o1")["session_id"])

    assert store.delete_session("nobody", "whatever")["deleted"] is False
    assert store.delete_session("o1", "not-owned")["deleted"] is False
    # The real session is untouched.
    sessions, _ = store.list_sessions("o1")
    assert [s["session_id"] for s in sessions] == [a]


# -- close session tests --------------------------------------------------


def test_close_session_marks_session_closed() -> None:
    """Closing a session sets its closed flag and preserves all data."""
    store = _store()
    sid = str(store.create_session("owner-1")["session_id"])
    store.record(sid, "owner-1", "hello", "hi there")

    result = store.close_session("owner-1", sid)
    assert result == {"closed": True}
    assert store.is_session_closed(sid) is True

    # History and metadata are preserved.
    sessions, _ = store.list_sessions("owner-1")
    s = sessions[0]
    assert s["session_id"] == sid
    assert s["turn_count"] == 1
    assert s["closed"] is True

    history = store.history(sid)
    assert history == [("hello", "hi there")]


def test_close_session_unknown_owner_returns_closed_false() -> None:
    """Closing a session for an unknown owner returns closed=False."""
    store = _store()
    store.create_session("owner-1")

    result = store.close_session("nobody", "whatever")
    assert result == {"closed": False, "reason": "session not found"}
    assert store.is_session_closed("whatever") is False


def test_close_session_unknown_session_returns_closed_false() -> None:
    """Closing a session not owned by the owner returns closed=False."""
    store = _store()
    store.create_session("owner-1")

    result = store.close_session("owner-1", "not-mine")
    assert result == {"closed": False, "reason": "session not found"}


def test_close_session_is_idempotent() -> None:
    """Closing an already-closed session succeeds and is a no-op."""
    store = _store()
    sid = str(store.create_session("owner-1")["session_id"])

    assert store.close_session("owner-1", sid) == {"closed": True}
    assert store.close_session("owner-1", sid) == {"closed": True}
    assert store.is_session_closed(sid) is True


def test_is_session_closed_unknown_session_returns_false() -> None:
    """An unknown/never-created session is treated as not closed."""
    store = _store()
    assert store.is_session_closed("ghost") is False


def test_close_session_persists_closed_flag(tmp_path: Path) -> None:
    """The closed flag survives a persist→load round-trip."""
    path = tmp_path / "conversations.json"
    store1 = _store(persist_path=path)
    sid = str(store1.create_session("owner-1")["session_id"])
    store1.close_session("owner-1", sid)

    # Load into a fresh store.
    store2 = _store(persist_path=path)
    assert store2.is_session_closed(sid) is True
    sessions, _ = store2.list_sessions("owner-1")
    assert sessions[0]["closed"] is True


# -- compaction (in place) + legacy continuation routing ------------------


def test_compact_session_is_in_place():
    """Compaction keeps the session id and the full UI transcript."""
    store = _store()
    sid = str(store.create_session("owner-1")["session_id"])
    store.record(sid, "owner-1", "q1", "a1")
    store.record(sid, "owner-1", "q2", "a2")

    meta = store.compact_session("owner-1", sid, "the summary")

    assert meta["session_id"] == sid  # same session
    session = store.get_session(sid)
    assert session is not None
    assert session.compacted_summary == "the summary"
    assert session.compacted_turn_index == 2
    assert session.compacted_into is None  # legacy pointer never set anymore
    # UI transcript untouched; owner still has exactly one session.
    assert store.history(sid) == [("q1", "a1"), ("q2", "a2")]
    sessions, active = store.list_sessions("owner-1")
    assert [s["session_id"] for s in sessions] == [sid]
    assert active == sid


def test_agent_history_replaces_compacted_turns_with_summary():
    """agent_history returns the summary turn plus post-compaction turns."""
    store = _store()
    sid = str(store.create_session("owner-1")["session_id"])
    store.record(sid, "owner-1", "q1", "a1")
    store.compact_session("owner-1", sid, "sum of q1")
    store.record(sid, "owner-1", "q2", "a2")

    history = store.agent_history(sid)
    assert len(history) == 2
    assert history[0][0] == ""
    assert "sum of q1" in history[0][1]
    assert history[1] == ("q2", "a2")
    # begin() serves the same agent view.
    _, begin_history = store.begin(sid)
    assert begin_history == history


def test_compaction_marker_survives_history_trim():
    """Trimming old turns keeps the marker aligned (never re-covers turns)."""
    store = _store(max_history_turns=3)
    sid = str(store.create_session("owner-1")["session_id"])
    store.record(sid, "owner-1", "q1", "a1")
    store.record(sid, "owner-1", "q2", "a2")
    store.compact_session("owner-1", sid, "sum")  # covers q1..q2 (index 2)
    store.record(sid, "owner-1", "q3", "a3")
    store.record(sid, "owner-1", "q4", "a4")  # trims q1 → index shifts to 1

    session = store.get_session(sid)
    assert session is not None
    assert session.turns == [("q2", "a2"), ("q3", "a3"), ("q4", "a4")]
    assert session.compacted_turn_index == 1
    history = store.agent_history(sid)
    assert "sum" in history[0][1]
    assert history[1:] == [("q3", "a3"), ("q4", "a4")]


def test_compacted_state_survives_persist_round_trip(tmp_path: Path) -> None:
    """Summary + marker survive a persist→load round-trip."""
    path = tmp_path / "conversations.json"
    store1 = _store(persist_path=path)
    sid = str(store1.create_session("owner-1")["session_id"])
    store1.record(sid, "owner-1", "q1", "a1")
    store1.compact_session("owner-1", sid, "the summary")
    store1.record(sid, "owner-1", "q2", "a2")

    store2 = _store(persist_path=path)
    history = store2.agent_history(sid)
    assert "the summary" in history[0][1]
    assert history[1:] == [("q2", "a2")]


def test_resolve_session_follows_legacy_compaction_chain() -> None:
    """Legacy compacted_into chains (from the old design) still reroute."""
    store = _store()
    a = str(store.create_session("owner-1")["session_id"])
    b = str(store.create_session("owner-1")["session_id"])
    c = str(store.create_session("owner-1")["session_id"])
    store.get_session(a).compacted_into = b  # type: ignore[union-attr]
    store.get_session(b).compacted_into = c  # type: ignore[union-attr]

    assert store.resolve_session(a) == c
    assert store.resolve_session(b) == c
    assert store.resolve_session(c) == c
    assert store.resolve_session("ghost") == "ghost"


def test_resolve_session_guards_against_cycles() -> None:
    """A (corrupt) compacted_into cycle terminates instead of hanging."""
    store = _store()
    a = str(store.create_session("owner-1")["session_id"])
    b = str(store.create_session("owner-1")["session_id"])
    store.get_session(a).compacted_into = b  # type: ignore[union-attr]
    store.get_session(b).compacted_into = a  # type: ignore[union-attr]

    assert store.resolve_session(a) in {a, b}

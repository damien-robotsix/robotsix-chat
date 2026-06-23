"""Tests for :class:`ConversationStore` — per-client history with idle reset."""

from __future__ import annotations

import json
import tempfile
from itertools import count
from pathlib import Path

import pytest

from robotsix_chat.chat.conversation import ConversationStore


class _FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _store(clock: _FakeClock, **kwargs: object) -> ConversationStore:
    """Build a store with deterministic session ids (``s0``, ``s1``, …)."""
    ids = count()
    return ConversationStore(
        clock=clock,
        session_factory=lambda: f"s{next(ids)}",
        **kwargs,  # type: ignore[arg-type]
    )


def test_first_message_starts_fresh_conversation() -> None:
    """A brand-new client gets a fresh session id and empty history."""
    clock = _FakeClock()
    store = _store(clock)

    session_id, history = store.begin("client-a")

    assert session_id == "s0"
    assert history == []


def test_consecutive_messages_share_session_and_accumulate_history() -> None:
    """Messages within the idle window share a session and replay prior turns."""
    clock = _FakeClock()
    store = _store(clock)

    session_id, history = store.begin("client-a")
    assert history == []
    store.record("client-a", "hello", "hi there")

    clock.advance(60)  # a minute later — same conversation
    session_id_2, history_2 = store.begin("client-a")

    assert session_id_2 == session_id  # same trace session
    assert history_2 == [("hello", "hi there")]


def test_idle_gap_resets_to_new_conversation() -> None:
    """Past the idle window, the next message starts a new session + history."""
    clock = _FakeClock()
    store = _store(clock, idle_reset_seconds=1800)

    first_session, _ = store.begin("client-a")
    store.record("client-a", "hello", "hi there")

    clock.advance(1801)  # just past the 30-minute window
    new_session, history = store.begin("client-a")

    assert new_session != first_session  # a new trace session
    assert history == []  # prior turns dropped


def test_within_window_after_record_does_not_reset() -> None:
    """Idle is measured from the last activity, including the last record()."""
    clock = _FakeClock()
    store = _store(clock, idle_reset_seconds=100)

    store.begin("client-a")
    clock.advance(90)
    store.record("client-a", "q", "a")  # refreshes last activity
    clock.advance(90)  # 180s since begin, but only 90s since record

    session_id, history = store.begin("client-a")
    assert history == [("q", "a")]  # not reset
    assert session_id == "s0"


def test_history_trimmed_to_max_turns() -> None:
    """History keeps only the most recent ``max_history_turns`` turns."""
    clock = _FakeClock()
    store = _store(clock, max_history_turns=2)

    store.begin("client-a")
    for i in range(4):
        store.record("client-a", f"q{i}", f"a{i}")

    _, history = store.begin("client-a")
    assert history == [("q2", "a2"), ("q3", "a3")]  # only the last two


def test_separate_clients_have_independent_conversations() -> None:
    """Different client ids keep separate sessions and histories."""
    clock = _FakeClock()
    store = _store(clock)

    sa, _ = store.begin("client-a")
    store.record("client-a", "qa", "aa")
    sb, hb = store.begin("client-b")

    assert sb != sa
    assert hb == []
    _, ha = store.begin("client-a")
    assert ha == [("qa", "aa")]


def test_lru_eviction_bounds_tracked_clients() -> None:
    """Tracking more than ``max_conversations`` clients evicts the oldest."""
    clock = _FakeClock()
    store = _store(clock, max_conversations=2)

    store.begin("client-a")
    store.record("client-a", "qa", "aa")
    store.begin("client-b")
    store.begin("client-c")  # evicts the least-recently-used (client-a)

    # client-a was evicted — record() is a no-op and begin() starts fresh.
    store.record("client-a", "late", "drop")
    _, history = store.begin("client-a")
    assert history == []


def test_new_session_id_is_unique() -> None:
    """Each ``new_session_id`` call returns a distinct id."""
    clock = _FakeClock()
    store = _store(clock)

    assert store.new_session_id() != store.new_session_id()


def test_returned_history_is_a_copy() -> None:
    """Mutating the returned history must not corrupt stored state."""
    clock = _FakeClock()
    store = _store(clock)

    store.begin("client-a")
    store.record("client-a", "q", "a")
    _, history = store.begin("client-a")
    history.append(("injected", "evil"))

    _, history_again = store.begin("client-a")
    assert history_again == [("q", "a")]


@pytest.mark.parametrize("client_turns", [1, 5, 20])
def test_default_idle_window_is_thirty_minutes(client_turns: int) -> None:
    """A real-default store keeps turns under 30 min and resets just past it."""
    clock = _FakeClock()
    store = ConversationStore(clock=clock)  # defaults: 1800s

    store.begin("c")
    for i in range(client_turns):
        store.record("c", f"q{i}", f"a{i}")

    clock.advance(1799)
    _, kept = store.begin("c")
    assert kept  # still the same conversation

    clock.advance(1801)
    _, reset = store.begin("c")
    assert reset == []


# -- on-disk persistence -------------------------------------------------


def test_persist_writes_to_file_on_record() -> None:
    """record() writes the full store state to the persist file."""
    clock = _FakeClock()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        persist_path = Path(f.name)

    try:
        store = _store(clock, persist_path=persist_path)
        store.begin("c1")
        store.record("c1", "hello", "hi there")

        raw = json.loads(persist_path.read_text(encoding="utf-8"))
        assert "c1" in raw
        assert raw["c1"]["turns"] == [["hello", "hi there"]]
    finally:
        persist_path.unlink(missing_ok=True)


def test_load_restores_history_from_disk() -> None:
    """On init, previously persisted conversations are loaded back."""
    data = {
        "c1": {
            "session_id": "saved-session",
            "turns": [["q1", "a1"], ["q2", "a2"]],
        }
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f)
        persist_path = Path(f.name)

    try:
        clock = _FakeClock()
        store = _store(clock, persist_path=persist_path)
        session_id, history = store.begin("c1")
        assert history == [("q1", "a1"), ("q2", "a2")]
    finally:
        persist_path.unlink(missing_ok=True)


def test_load_missing_file_is_graceful() -> None:
    """A missing persist file is not an error — store starts empty."""
    clock = _FakeClock()
    store = _store(clock, persist_path=Path("/nonexistent/conversations.json"))
    session_id, history = store.begin("c1")
    assert history == []


def test_load_trims_to_max_history_turns() -> None:
    """On load, turns beyond max_history_turns are trimmed."""
    data = {
        "c1": {
            "session_id": "s",
            "turns": [["q0", "a0"], ["q1", "a1"], ["q2", "a2"]],
        }
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f)
        persist_path = Path(f.name)

    try:
        clock = _FakeClock()
        store = _store(clock, max_history_turns=2, persist_path=persist_path)
        _, history = store.begin("c1")
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
        clock = _FakeClock()
        store = _store(clock, persist_path=persist_path)
        _, history = store.begin("c1")
        assert history == []
    finally:
        persist_path.unlink(missing_ok=True)


def test_persist_preserves_idle_reset_behaviour() -> None:
    """Idle reset still works after loading from disk."""
    data = {
        "c1": {
            "session_id": "old-session",
            "turns": [["q1", "a1"]],
        }
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f)
        persist_path = Path(f.name)

    try:
        clock = _FakeClock()
        store = _store(clock, idle_reset_seconds=100, persist_path=persist_path)

        # Loaded conversation should be active right after load.
        sid1, hist1 = store.begin("c1")
        assert hist1 == [("q1", "a1")]

        # Advance past idle window — conversation resets.
        clock.advance(101)
        sid2, hist2 = store.begin("c1")
        assert sid2 != sid1
        assert hist2 == []
    finally:
        persist_path.unlink(missing_ok=True)


def test_default_max_history_turns_is_50() -> None:
    """The default cap matches the acceptance criterion of 50 most recent."""
    clock = _FakeClock()
    store = ConversationStore(clock=clock)
    assert store._max_history_turns == 50

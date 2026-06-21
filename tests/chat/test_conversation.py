"""Tests for :class:`ConversationStore` — per-client history with idle reset."""

from __future__ import annotations

from itertools import count

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
    clock = _FakeClock()
    store = _store(clock)

    session_id, history = store.begin("client-a")

    assert session_id == "s0"
    assert history == []


def test_consecutive_messages_share_session_and_accumulate_history() -> None:
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
    clock = _FakeClock()
    store = _store(clock, max_history_turns=2)

    store.begin("client-a")
    for i in range(4):
        store.record("client-a", f"q{i}", f"a{i}")

    _, history = store.begin("client-a")
    assert history == [("q2", "a2"), ("q3", "a3")]  # only the last two


def test_separate_clients_have_independent_conversations() -> None:
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

"""Tests for the pending-questions store."""

from __future__ import annotations

from typing import Any

import pytest

from robotsix_chat.chat.events import (
    SSE_PENDING_QUESTION_ANSWERED_TYPE,
    SSE_PENDING_QUESTION_THREAD_MESSAGE_TYPE,
    EventBus,
)
from robotsix_chat.pending_questions.store import (
    PendingQuestion,
    PendingQuestionsStore,
)


def _setup_store_with_bus(
    session_id: str = "sess-1", text: str = "Q1"
) -> tuple[EventBus, PendingQuestionsStore, PendingQuestion, list[dict[str, Any]], Any]:
    """Create a store with a real EventBus, add a question, and subscribe.

    Returns (bus, store, entry, frames, q).
    """
    bus = EventBus()
    store = PendingQuestionsStore(event_bus=bus)
    entry = store.add(session_id, text)
    frames: list[dict[str, Any]] = []
    q = bus.subscribe(session_id)
    return bus, store, entry, frames, q


def test_add_returns_entry_with_id() -> None:
    """Adding a question returns a populated PendingQuestion."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "What is your name?")
    assert entry.question_id
    assert len(entry.question_id) == 12
    assert entry.text == "What is your name?"
    assert entry.detail == ""
    assert entry.status == "pending"
    assert entry.session_id == "sess-1"
    # The initial question text is also appended as an assistant thread message.
    assert len(entry.thread) == 1
    assert entry.thread[0].role == "assistant"
    assert entry.thread[0].text == "What is your name?"


def test_add_stores_detail() -> None:
    """The detail field is preserved."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Question", "Some detail")
    assert entry.detail == "Some detail"


def test_add_raises_on_empty_text() -> None:
    """Empty text raises ValueError."""
    store = PendingQuestionsStore()
    with pytest.raises(ValueError, match="question text must not be empty"):
        store.add("sess-1", "")


def test_add_raises_on_whitespace_text() -> None:
    """Whitespace-only text raises ValueError."""
    store = PendingQuestionsStore()
    with pytest.raises(ValueError):
        store.add("sess-1", "   ")


def test_list_for_session_returns_entries() -> None:
    """list_for_session returns all entries for a session."""
    store = PendingQuestionsStore()
    store.add("sess-1", "Q1")
    store.add("sess-1", "Q2")
    entries = store.list_for_session("sess-1")
    assert len(entries) == 2
    assert entries[0].text == "Q1"
    assert entries[1].text == "Q2"


def test_list_for_session_isolation() -> None:
    """Sessions are isolated from each other."""
    store = PendingQuestionsStore()
    store.add("sess-1", "Q1")
    store.add("sess-2", "Q2")
    assert len(store.list_for_session("sess-1")) == 1
    assert len(store.list_for_session("sess-2")) == 1
    assert len(store.list_for_session("sess-3")) == 0


def test_get_finds_by_id() -> None:
    """get() finds a question by its id."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    found = store.get(entry.question_id)
    assert found is not None
    assert found.question_id == entry.question_id


def test_get_returns_none_for_unknown() -> None:
    """get() returns None for unknown ids."""
    store = PendingQuestionsStore()
    assert store.get("nonexistent") is None


def test_update_modifies_fields() -> None:
    """update() changes text, detail, and status."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Original", "Original detail")
    updated = store.update(
        entry.question_id, text="Updated", detail="New detail", status="answered"
    )
    assert updated is not None
    assert updated.text == "Updated"
    assert updated.detail == "New detail"
    assert updated.status == "answered"


def test_update_partial() -> None:
    """update() leaves unset fields unchanged."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Original", "Original detail")
    updated = store.update(entry.question_id, text="Updated")
    assert updated is not None
    assert updated.text == "Updated"
    assert updated.detail == "Original detail"
    assert updated.status == "pending"


def test_update_unknown_returns_none() -> None:
    """update() on an unknown id returns None."""
    store = PendingQuestionsStore()
    assert store.update("nonexistent", text="X") is None


def test_remove_deletes_entry() -> None:
    """remove() deletes the entry and returns it."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    removed = store.remove(entry.question_id)
    assert removed is not None
    assert removed.question_id == entry.question_id
    assert len(store.list_for_session("sess-1")) == 0


def test_remove_unknown_returns_none() -> None:
    """remove() on an unknown id returns None."""
    store = PendingQuestionsStore()
    result = store.remove("nonexistent")
    assert result is None


def test_wall_clock_override() -> None:
    """The wall_clock parameter controls created_at."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1", wall_clock=12345.0)
    assert entry.created_at == 12345.0


# ---------------------------------------------------------------------------
# answer()
# ---------------------------------------------------------------------------


def test_answer_sets_answered_status() -> None:
    """answer() sets status='answered' and stores the answer text."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "What is your name?")
    result = store.answer(entry.question_id, "Damien")
    assert result is not None
    assert result.status == "answered"
    assert result.answer == "Damien"
    assert result.answered_at > 0


def test_answer_leaves_entry_retrievable() -> None:
    """After answer(), entry is still retrievable via get() and list_for_session()."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    store.answer(entry.question_id, "A1")

    found = store.get(entry.question_id)
    assert found is not None
    assert found.answer == "A1"
    assert found.status == "answered"

    entries = store.list_for_session("sess-1")
    assert len(entries) == 1
    assert entries[0].answer == "A1"


def test_answer_unknown_returns_none() -> None:
    """answer() on an unknown id returns None."""
    store = PendingQuestionsStore()
    result = store.answer("nonexistent", "answer")
    assert result is None


def test_answer_strips_whitespace() -> None:
    """answer() strips whitespace from the answer text."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    result = store.answer(entry.question_id, "  trimmed  ")
    assert result is not None
    assert result.answer == "trimmed"


def test_answer_wall_clock_override() -> None:
    """The wall_clock parameter controls answered_at."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1", wall_clock=100.0)
    result = store.answer(entry.question_id, "A1", wall_clock=200.0)
    assert result is not None
    assert result.answered_at == 200.0


def test_answer_publishes_frame() -> None:
    """With an event bus wired, answer() publishes a pending_question_answered frame."""
    bus, store, entry, frames, q = _setup_store_with_bus()

    store.answer(entry.question_id, "A1")

    # Drain the queue without blocking; the frames are already published.
    while not q.empty():
        frames.append(q.get_nowait())

    bus.unsubscribe("sess-1", q)

    answered_frames = [
        f for f in frames if f.get("type") == SSE_PENDING_QUESTION_ANSWERED_TYPE
    ]
    assert len(answered_frames) == 1
    assert answered_frames[0]["question_id"] == entry.question_id
    assert answered_frames[0]["answer"] == "A1"
    assert answered_frames[0]["status"] == "answered"


# ---------------------------------------------------------------------------
# append_to_thread() / get_thread()
# ---------------------------------------------------------------------------


def test_append_to_thread_adds_message() -> None:
    """append_to_thread() appends a message and returns the entry."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    # add() now also appends the question as an assistant thread message.
    assert len(entry.thread) == 1  # the initial question
    result = store.append_to_thread(entry.question_id, "assistant", "Hello")
    assert result is not None
    assert len(result.thread) == 2
    assert result.thread[1].role == "assistant"
    assert result.thread[1].text == "Hello"
    assert result.thread[1].timestamp > 0


def test_append_to_thread_multiple_messages() -> None:
    """Multiple messages are appended in order."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    store.append_to_thread(entry.question_id, "user", "msg1")
    store.append_to_thread(entry.question_id, "assistant", "msg2")
    store.append_to_thread(entry.question_id, "user", "msg3")

    # add() already pushed the question as thread[0]; we appended 3 more.
    assert len(entry.thread) == 4
    assert entry.thread[1].text == "msg1"
    assert entry.thread[2].text == "msg2"
    assert entry.thread[3].text == "msg3"
    assert entry.thread[1].role == "user"
    assert entry.thread[2].role == "assistant"


def test_append_to_thread_unknown_returns_none() -> None:
    """append_to_thread() on an unknown id returns None."""
    store = PendingQuestionsStore()
    result = store.append_to_thread("nonexistent", "user", "msg")
    assert result is None


def test_append_to_thread_strips_whitespace() -> None:
    """append_to_thread() strips whitespace from message text."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    store.append_to_thread(entry.question_id, "user", "  trimmed  ")
    # Index 1 is the newly appended user message (index 0 is the question).
    assert entry.thread[1].text == "trimmed"


def test_append_to_thread_wall_clock_override() -> None:
    """The wall_clock parameter controls message timestamp."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1", wall_clock=100.0)
    store.append_to_thread(entry.question_id, "user", "msg", wall_clock=200.0)
    # Index 0 is the question (timestamp=100.0), index 1 is the new message.
    assert entry.thread[1].timestamp == 200.0


def test_append_to_thread_publishes_frame() -> None:
    """With an event bus wired, append_to_thread() publishes a frame."""
    bus, store, entry, frames, q = _setup_store_with_bus()

    store.append_to_thread(entry.question_id, "assistant", "Hello")

    while not q.empty():
        frames.append(q.get_nowait())

    bus.unsubscribe("sess-1", q)

    thread_frames = [
        f for f in frames if f.get("type") == SSE_PENDING_QUESTION_THREAD_MESSAGE_TYPE
    ]
    assert len(thread_frames) == 1
    assert thread_frames[0]["question_id"] == entry.question_id
    assert thread_frames[0]["role"] == "assistant"
    assert thread_frames[0]["text"] == "Hello"
    assert thread_frames[0]["timestamp"] > 0


def test_get_thread_returns_copy() -> None:
    """get_thread() returns a copy of thread messages, or None for unknown."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    store.append_to_thread(entry.question_id, "user", "m1")
    store.append_to_thread(entry.question_id, "assistant", "m2")

    thread = store.get_thread(entry.question_id)
    assert thread is not None
    # add() already appends the question, so we have 3 messages.
    assert len(thread) == 3
    assert thread[1].text == "m1"
    assert thread[2].text == "m2"

    # Mutating the returned list does not affect the store.
    thread.pop()
    assert len(entry.thread) == 3


def test_get_thread_unknown_returns_none() -> None:
    """get_thread() on an unknown id returns None."""
    store = PendingQuestionsStore()
    result = store.get_thread("nonexistent")
    assert result is None


def test_get_thread_empty() -> None:
    """get_thread() returns the initial question when no extra messages exist."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    thread = store.get_thread(entry.question_id)
    assert thread is not None
    # add() appends the question text as an assistant thread message.
    assert len(thread) == 1
    assert thread[0].role == "assistant"
    assert thread[0].text == "Q1"


def test_append_to_thread_deduplicates_identical_message() -> None:
    """append_to_thread() skips messages that are already in the thread.

    When the same role+text is appended twice, the second call is a no-op
    and no duplicate frame is published.  This prevents double-posting
    when, for example, the agent calls ``append_to_pending_question_thread``
    during ``_process_thread_message`` and the background task also tries
    to post the same reply.
    """
    bus, store, entry, frames, q = _setup_store_with_bus()

    # Drain the add() frames (pending_question_added + thread message).
    while not q.empty():
        q.get_nowait()

    # First append — should succeed.
    result = store.append_to_thread(entry.question_id, "assistant", "Hello")
    assert result is not None
    assert len(entry.thread) == 2  # initial question + "Hello"
    assert entry.thread[1].role == "assistant"
    assert entry.thread[1].text == "Hello"

    # Second append with identical role+text — should be a no-op.
    result2 = store.append_to_thread(entry.question_id, "assistant", "Hello")
    assert result2 is not None  # returns the entry, not None
    assert len(entry.thread) == 2  # still only 2 messages

    # Only one thread_message frame should have been published (for the first append).
    while not q.empty():
        frames.append(q.get_nowait())

    bus.unsubscribe("sess-1", q)

    thread_frames = [
        f for f in frames if f.get("type") == SSE_PENDING_QUESTION_THREAD_MESSAGE_TYPE
    ]
    assert len(thread_frames) == 1
    assert thread_frames[0]["text"] == "Hello"


def test_append_to_thread_different_role_not_duplicate() -> None:
    """Messages with the same text but different roles are not duplicates."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    # add() already appends the question as assistant; that's thread[0].

    store.append_to_thread(entry.question_id, "assistant", "Same text")
    store.append_to_thread(entry.question_id, "user", "Same text")

    # Both should be appended because the roles differ.
    assert len(entry.thread) == 3
    assert entry.thread[1].role == "assistant"
    assert entry.thread[1].text == "Same text"
    assert entry.thread[2].role == "user"
    assert entry.thread[2].text == "Same text"


def test_append_to_thread_whitespace_dedup() -> None:
    """Deduplication is whitespace-insensitive (text is stripped before comparison)."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")

    store.append_to_thread(entry.question_id, "assistant", "  Hello  ")
    store.append_to_thread(entry.question_id, "assistant", "Hello")

    # Second append should be a no-op because stripped texts match.
    assert len(entry.thread) == 2


def test_thread_messages_ordered_oldest_first() -> None:
    """Thread messages are emitted in chronological order (oldest → newest).

    The initial question (added by ``add()``), any follow-ups (via
    ``append_to_thread()``), and answers (via ``answer()``) must all appear
    in the thread in ascending timestamp order so the frontend can render
    them oldest-at-top / newest-at-bottom.
    """
    store = PendingQuestionsStore()

    # Create a question at t=10.
    entry = store.add("sess-1", "Q1", wall_clock=10.0)

    # Append a follow-up at t=20.
    store.append_to_thread(entry.question_id, "assistant", "Follow-up", wall_clock=20.0)

    # Answer at t=30.
    store.answer(entry.question_id, "A1", wall_clock=30.0)

    # Append another follow-up at t=25 (between answer and previous).
    store.append_to_thread(entry.question_id, "user", "Mid follow-up", wall_clock=25.0)

    # Answer again at t=40.
    store.answer(entry.question_id, "A2", wall_clock=40.0)

    thread = entry.thread
    # In the raw store list, messages are in insertion order (not necessarily
    # sorted).  The public get_thread() returns them sorted by timestamp.
    assert len(thread) == 5

    sorted_thread = store.get_thread(entry.question_id)
    assert sorted_thread is not None
    assert [m.timestamp for m in sorted_thread] == [10.0, 20.0, 25.0, 30.0, 40.0]

    # Verify roles.
    assert sorted_thread[0].role == "assistant" and sorted_thread[0].text == "Q1"
    assert sorted_thread[1].role == "assistant" and sorted_thread[1].text == "Follow-up"
    assert sorted_thread[2].role == "user" and sorted_thread[2].text == "Mid follow-up"
    assert sorted_thread[3].role == "user" and sorted_thread[3].text == "A1"
    assert sorted_thread[4].role == "user" and sorted_thread[4].text == "A2"

    # Verify the store-level ordering via list_for_session (most-recently-added
    # questions come last — single entry here).
    entries = store.list_for_session("sess-1")
    assert len(entries) == 1
    assert entries[0].question_id == entry.question_id

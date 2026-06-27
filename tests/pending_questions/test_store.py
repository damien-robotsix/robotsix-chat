"""Tests for the pending-questions store."""

from __future__ import annotations

import pytest

from robotsix_chat.pending_questions.store import PendingQuestionsStore


def test_add_returns_entry_with_id():
    """Adding a question returns a populated PendingQuestion."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "What is your name?")
    assert entry.question_id
    assert len(entry.question_id) == 12
    assert entry.text == "What is your name?"
    assert entry.detail == ""
    assert entry.status == "pending"
    assert entry.session_id == "sess-1"
    assert entry.thread == []


def test_add_stores_detail():
    """The detail field is preserved."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Question", "Some detail")
    assert entry.detail == "Some detail"


def test_add_raises_on_empty_text():
    """Empty text raises ValueError."""
    store = PendingQuestionsStore()
    with pytest.raises(ValueError, match="question text must not be empty"):
        store.add("sess-1", "")


def test_add_raises_on_whitespace_text():
    """Whitespace-only text raises ValueError."""
    store = PendingQuestionsStore()
    with pytest.raises(ValueError):
        store.add("sess-1", "   ")


def test_list_for_session_returns_entries():
    """list_for_session returns all entries for a session."""
    store = PendingQuestionsStore()
    store.add("sess-1", "Q1")
    store.add("sess-1", "Q2")
    entries = store.list_for_session("sess-1")
    assert len(entries) == 2
    assert entries[0].text == "Q1"
    assert entries[1].text == "Q2"


def test_list_for_session_isolation():
    """Sessions are isolated from each other."""
    store = PendingQuestionsStore()
    store.add("sess-1", "Q1")
    store.add("sess-2", "Q2")
    assert len(store.list_for_session("sess-1")) == 1
    assert len(store.list_for_session("sess-2")) == 1
    assert len(store.list_for_session("sess-3")) == 0


def test_get_finds_by_id():
    """get() finds a question by its id."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    found = store.get(entry.question_id)
    assert found is not None
    assert found.question_id == entry.question_id


def test_get_returns_none_for_unknown():
    """get() returns None for unknown ids."""
    store = PendingQuestionsStore()
    assert store.get("nonexistent") is None


def test_update_modifies_fields():
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


def test_update_partial():
    """update() leaves unset fields unchanged."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Original", "Original detail")
    updated = store.update(entry.question_id, text="Updated")
    assert updated is not None
    assert updated.text == "Updated"
    assert updated.detail == "Original detail"
    assert updated.status == "pending"


def test_update_unknown_returns_none():
    """update() on an unknown id returns None."""
    store = PendingQuestionsStore()
    assert store.update("nonexistent", text="X") is None


def test_remove_deletes_entry():
    """remove() deletes the entry and returns it."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    removed = store.remove(entry.question_id)
    assert removed is not None
    assert removed.question_id == entry.question_id
    assert len(store.list_for_session("sess-1")) == 0


def test_remove_unknown_returns_none():
    """remove() on an unknown id returns None."""
    store = PendingQuestionsStore()
    result = store.remove("nonexistent")
    assert result is None


def test_wall_clock_override():
    """The wall_clock parameter controls created_at."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1", wall_clock=12345.0)
    assert entry.created_at == 12345.0


# ---------------------------------------------------------------------------
# answer()
# ---------------------------------------------------------------------------


def test_answer_sets_answered_status():
    """answer() sets status='answered' and stores the answer text."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "What is your name?")
    result = store.answer(entry.question_id, "Damien")
    assert result is not None
    assert result.status == "answered"
    assert result.answer == "Damien"
    assert result.answered_at > 0


def test_answer_leaves_entry_retrievable():
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


def test_answer_unknown_returns_none():
    """answer() on an unknown id returns None."""
    store = PendingQuestionsStore()
    result = store.answer("nonexistent", "answer")
    assert result is None


def test_answer_strips_whitespace():
    """answer() strips whitespace from the answer text."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    result = store.answer(entry.question_id, "  trimmed  ")
    assert result is not None
    assert result.answer == "trimmed"


def test_answer_wall_clock_override():
    """The wall_clock parameter controls answered_at."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1", wall_clock=100.0)
    result = store.answer(entry.question_id, "A1", wall_clock=200.0)
    assert result is not None
    assert result.answered_at == 200.0


def test_answer_publishes_frame():
    """With an event bus wired, answer() publishes a pending_question_answered frame."""
    from robotsix_chat.chat.events import EventBus

    bus = EventBus()
    store = PendingQuestionsStore(event_bus=bus)
    entry = store.add("sess-1", "Q1")

    frames: list[dict[str, object]] = []
    q = bus.subscribe("sess-1")

    store.answer(entry.question_id, "A1")

    # Drain the queue without blocking; the frames are already published.
    while not q.empty():
        frames.append(q.get_nowait())

    bus.unsubscribe("sess-1", q)

    answered_frames = [
        f for f in frames if f.get("type") == "pending_question_answered"
    ]
    assert len(answered_frames) == 1
    assert answered_frames[0]["question_id"] == entry.question_id
    assert answered_frames[0]["answer"] == "A1"
    assert answered_frames[0]["status"] == "answered"


# ---------------------------------------------------------------------------
# append_to_thread() / get_thread()
# ---------------------------------------------------------------------------


def test_append_to_thread_adds_message():
    """append_to_thread() appends a message and returns the entry."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    result = store.append_to_thread(entry.question_id, "assistant", "Hello")
    assert result is not None
    assert len(result.thread) == 1
    assert result.thread[0].role == "assistant"
    assert result.thread[0].text == "Hello"
    assert result.thread[0].timestamp > 0


def test_append_to_thread_multiple_messages():
    """Multiple messages are appended in order."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    store.append_to_thread(entry.question_id, "user", "msg1")
    store.append_to_thread(entry.question_id, "assistant", "msg2")
    store.append_to_thread(entry.question_id, "user", "msg3")

    assert len(entry.thread) == 3
    assert entry.thread[0].text == "msg1"
    assert entry.thread[1].text == "msg2"
    assert entry.thread[2].text == "msg3"
    assert entry.thread[0].role == "user"
    assert entry.thread[1].role == "assistant"


def test_append_to_thread_unknown_returns_none():
    """append_to_thread() on an unknown id returns None."""
    store = PendingQuestionsStore()
    result = store.append_to_thread("nonexistent", "user", "msg")
    assert result is None


def test_append_to_thread_strips_whitespace():
    """append_to_thread() strips whitespace from message text."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    store.append_to_thread(entry.question_id, "user", "  trimmed  ")
    assert entry.thread[0].text == "trimmed"


def test_append_to_thread_wall_clock_override():
    """The wall_clock parameter controls message timestamp."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1", wall_clock=100.0)
    store.append_to_thread(entry.question_id, "user", "msg", wall_clock=200.0)
    assert entry.thread[0].timestamp == 200.0


def test_append_to_thread_publishes_frame():
    """With an event bus wired, append_to_thread() publishes a frame."""
    from robotsix_chat.chat.events import EventBus

    bus = EventBus()
    store = PendingQuestionsStore(event_bus=bus)
    entry = store.add("sess-1", "Q1")

    frames: list[dict[str, object]] = []
    q = bus.subscribe("sess-1")

    store.append_to_thread(entry.question_id, "assistant", "Hello")

    while not q.empty():
        frames.append(q.get_nowait())

    bus.unsubscribe("sess-1", q)

    thread_frames = [
        f for f in frames if f.get("type") == "pending_question_thread_message"
    ]
    assert len(thread_frames) == 1
    assert thread_frames[0]["question_id"] == entry.question_id
    assert thread_frames[0]["role"] == "assistant"
    assert thread_frames[0]["text"] == "Hello"
    assert thread_frames[0]["timestamp"] > 0


def test_get_thread_returns_copy():
    """get_thread() returns a copy of thread messages, or None for unknown."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    store.append_to_thread(entry.question_id, "user", "m1")
    store.append_to_thread(entry.question_id, "assistant", "m2")

    thread = store.get_thread(entry.question_id)
    assert thread is not None
    assert len(thread) == 2
    assert thread[0].text == "m1"
    assert thread[1].text == "m2"

    # Mutating the returned list does not affect the store.
    thread.pop()
    assert len(entry.thread) == 2


def test_get_thread_unknown_returns_none():
    """get_thread() on an unknown id returns None."""
    store = PendingQuestionsStore()
    result = store.get_thread("nonexistent")
    assert result is None


def test_get_thread_empty():
    """get_thread() returns an empty list when no messages exist."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1")
    thread = store.get_thread(entry.question_id)
    assert thread == []

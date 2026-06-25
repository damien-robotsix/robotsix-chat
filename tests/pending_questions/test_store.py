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
    assert store.remove("nonexistent") is None


def test_wall_clock_override():
    """The wall_clock parameter controls created_at."""
    store = PendingQuestionsStore()
    entry = store.add("sess-1", "Q1", wall_clock=12345.0)
    assert entry.created_at == 12345.0

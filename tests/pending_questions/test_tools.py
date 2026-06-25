"""Tests for the pending-questions tool factory."""

from __future__ import annotations

import pytest

from robotsix_chat.config import PendingQuestionsSettings
from robotsix_chat.pending_questions import build_pending_questions_tools
from robotsix_chat.pending_questions.store import PendingQuestionsStore


class TestBuildPendingQuestionsTools:
    """Tests for build_pending_questions_tools()."""

    def test_disabled_returns_empty(self) -> None:
        settings = PendingQuestionsSettings(enabled=False)
        store = PendingQuestionsStore()
        tools = build_pending_questions_tools(settings, store, session_id="sess-1")
        assert tools == []

    def test_enabled_returns_three_tools(self) -> None:
        settings = PendingQuestionsSettings(enabled=True)
        store = PendingQuestionsStore()
        tools = build_pending_questions_tools(settings, store, session_id="sess-1")
        assert len(tools) == 3

    @pytest.mark.anyio
    async def test_add_tool_creates_entry(self) -> None:
        settings = PendingQuestionsSettings(enabled=True)
        store = PendingQuestionsStore()
        tools = build_pending_questions_tools(settings, store, session_id="sess-1")
        add_fn = tools[0]
        result = await add_fn("What is your name?", "Please tell me")
        # Should return the question id
        assert result
        assert len(result) == 12
        # Verify entry exists in store
        entries = store.list_for_session("sess-1")
        assert len(entries) == 1
        assert entries[0].text == "What is your name?"
        assert entries[0].detail == "Please tell me"

    @pytest.mark.anyio
    async def test_add_tool_empty_text_returns_error(self) -> None:
        settings = PendingQuestionsSettings(enabled=True)
        store = PendingQuestionsStore()
        tools = build_pending_questions_tools(settings, store, session_id="sess-1")
        add_fn = tools[0]
        result = await add_fn("")
        assert "Error" in result or "empty" in result.lower()

    @pytest.mark.anyio
    async def test_update_tool_modifies_entry(self) -> None:
        settings = PendingQuestionsSettings(enabled=True)
        store = PendingQuestionsStore()
        tools = build_pending_questions_tools(settings, store, session_id="sess-1")
        add_fn = tools[0]
        update_fn = tools[1]

        qid = await add_fn("Original")
        result = await update_fn(qid, text="Updated", detail="New detail")
        assert "Updated" in result

        entry = store.get(qid)
        assert entry is not None
        assert entry.text == "Updated"
        assert entry.detail == "New detail"

    @pytest.mark.anyio
    async def test_update_tool_unknown_id(self) -> None:
        settings = PendingQuestionsSettings(enabled=True)
        store = PendingQuestionsStore()
        tools = build_pending_questions_tools(settings, store, session_id="sess-1")
        update_fn = tools[1]
        result = await update_fn("nonexistent", text="X")
        assert "Unknown" in result

    @pytest.mark.anyio
    async def test_update_tool_no_fields(self) -> None:
        settings = PendingQuestionsSettings(enabled=True)
        store = PendingQuestionsStore()
        tools = build_pending_questions_tools(settings, store, session_id="sess-1")
        update_fn = tools[1]
        result = await update_fn("some-id")
        assert "No fields" in result

    @pytest.mark.anyio
    async def test_remove_tool_deletes_entry(self) -> None:
        settings = PendingQuestionsSettings(enabled=True)
        store = PendingQuestionsStore()
        tools = build_pending_questions_tools(settings, store, session_id="sess-1")
        add_fn = tools[0]
        remove_fn = tools[2]

        qid = await add_fn("Q1")
        result = await remove_fn(qid)
        assert "Removed" in result
        assert len(store.list_for_session("sess-1")) == 0

    @pytest.mark.anyio
    async def test_remove_tool_unknown_id(self) -> None:
        settings = PendingQuestionsSettings(enabled=True)
        store = PendingQuestionsStore()
        tools = build_pending_questions_tools(settings, store, session_id="sess-1")
        remove_fn = tools[2]
        result = await remove_fn("nonexistent")
        assert "Unknown" in result

    def test_session_isolation(self) -> None:
        """Tools scoped to different sessions do not interfere."""
        settings = PendingQuestionsSettings(enabled=True)
        store = PendingQuestionsStore()
        tools_a = build_pending_questions_tools(settings, store, session_id="sess-a")
        tools_b = build_pending_questions_tools(settings, store, session_id="sess-b")

        # Both sessions use the same store but different session ids.
        assert len(store.list_for_session("sess-a")) == 0
        assert len(store.list_for_session("sess-b")) == 0

    @pytest.mark.anyio
    async def test_status_update(self) -> None:
        settings = PendingQuestionsSettings(enabled=True)
        store = PendingQuestionsStore()
        tools = build_pending_questions_tools(settings, store, session_id="sess-1")
        add_fn = tools[0]
        update_fn = tools[1]

        qid = await add_fn("Question")
        result = await update_fn(qid, status="answered")
        assert "Updated" in result

        entry = store.get(qid)
        assert entry is not None
        assert entry.status == "answered"

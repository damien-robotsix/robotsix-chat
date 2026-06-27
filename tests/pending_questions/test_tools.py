"""Tests for the pending-questions tool factory."""

from __future__ import annotations

import pytest

from robotsix_chat.config import PendingQuestionsSettings
from robotsix_chat.pending_questions import build_pending_questions_tools
from robotsix_chat.pending_questions.store import PendingQuestionsStore


def test_disabled_returns_empty():
    """Disabled settings produce an empty tool list."""
    settings = PendingQuestionsSettings(enabled=False)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    assert tools == []


def test_enabled_returns_seven_tools():
    """Enabled settings produce seven tools.

    Includes add, update, remove, list, get, append_thread, get_thread.
    """
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    assert len(tools) == 7


@pytest.mark.anyio
async def test_add_tool_creates_entry():
    """The add tool creates a pending question in the store."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn = tools[0]
    result = await add_fn("What is your name?", "Please tell me")
    assert result
    assert len(result) == 12
    entries = store.list_for_session("sess-1")
    assert len(entries) == 1
    assert entries[0].text == "What is your name?"
    assert entries[0].detail == "Please tell me"


@pytest.mark.anyio
async def test_add_tool_empty_text_returns_error():
    """The add tool returns an error for empty text."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn = tools[0]
    result = await add_fn("")
    assert "Error" in result or "empty" in result.lower()


@pytest.mark.anyio
async def test_update_tool_modifies_entry():
    """The update tool changes an existing question's fields."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn, update_fn = tools[0], tools[1]

    qid = await add_fn("Original")
    result = await update_fn(qid, text="Updated", detail="New detail")
    assert "Updated" in result

    entry = store.get(qid)
    assert entry is not None
    assert entry.text == "Updated"
    assert entry.detail == "New detail"


@pytest.mark.anyio
async def test_update_tool_unknown_id():
    """The update tool returns an error for unknown ids."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    update_fn = tools[1]
    result = await update_fn("nonexistent", text="X")
    assert "Unknown" in result


@pytest.mark.anyio
async def test_update_tool_no_fields():
    """The update tool returns an error when no fields are supplied."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    update_fn = tools[1]
    result = await update_fn("some-id")
    assert "No fields" in result


@pytest.mark.anyio
async def test_remove_tool_deletes_entry():
    """The remove tool deletes a question from the store."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn, remove_fn = tools[0], tools[2]

    qid = await add_fn("Q1")
    result = await remove_fn(qid)
    assert "Removed" in result
    assert len(store.list_for_session("sess-1")) == 0


@pytest.mark.anyio
async def test_remove_tool_unknown_id():
    """The remove tool returns an error for unknown ids."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    remove_fn = tools[2]
    result = await remove_fn("nonexistent")
    assert "Unknown" in result


def test_session_isolation():
    """Tools scoped to different sessions target the correct session."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    # Just verify the tools are created; session routing tested via add/remove.
    tools_a = build_pending_questions_tools(settings, store, session_id="sess-a")
    tools_b = build_pending_questions_tools(settings, store, session_id="sess-b")
    assert len(tools_a) == 7
    assert len(tools_b) == 7
    assert len(store.list_for_session("sess-a")) == 0
    assert len(store.list_for_session("sess-b")) == 0


@pytest.mark.anyio
async def test_status_update():
    """The update tool can change the status field."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn, update_fn = tools[0], tools[1]

    qid = await add_fn("Question")
    result = await update_fn(qid, status="answered")
    assert "Updated" in result

    entry = store.get(qid)
    assert entry is not None
    assert entry.status == "answered"


@pytest.mark.anyio
async def test_list_tool_returns_all_questions():
    """The list tool returns every pending question for the session."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn, list_fn = tools[0], tools[3]

    qid1 = await add_fn("Q1")
    qid2 = await add_fn("Q2", "detail two")
    result = await list_fn()
    assert qid1 in result
    assert qid2 in result
    assert "Q1" in result
    assert "Q2" in result
    assert "detail two" in result
    assert "[pending]" in result


@pytest.mark.anyio
async def test_list_tool_empty_session():
    """The list tool returns a clear message when there are no questions."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    list_fn = tools[3]
    result = await list_fn()
    assert "No pending questions" in result


@pytest.mark.anyio
async def test_list_tool_session_isolation():
    """The list tool only returns questions for its own session."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools_a = build_pending_questions_tools(settings, store, session_id="sess-a")
    tools_b = build_pending_questions_tools(settings, store, session_id="sess-b")

    add_a, list_a = tools_a[0], tools_a[3]
    add_b, list_b = tools_b[0], tools_b[3]

    qid_a = await add_a("Q-A")
    qid_b = await add_b("Q-B")

    result_a = await list_a()
    assert qid_a in result_a
    assert qid_b not in result_a

    result_b = await list_b()
    assert qid_b in result_b
    assert qid_a not in result_b


@pytest.mark.anyio
async def test_get_tool_returns_question():
    """The get tool returns a single question by id."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn, get_fn = tools[0], tools[4]

    qid = await add_fn("What is your name?", "need name")
    result = await get_fn(qid)
    assert qid in result
    assert "What is your name?" in result
    assert "need name" in result
    assert "pending" in result
    # Not answered — answer line should not appear.
    assert "answer:" not in result


@pytest.mark.anyio
async def test_get_tool_includes_answer_after_answer():
    """After store.answer(), the get tool includes the answer in its output."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn, get_fn = tools[0], tools[4]

    qid = await add_fn("Q1")
    store.answer(qid, "my answer")
    result = await get_fn(qid)
    assert "answered" in result
    assert "answer: my answer" in result


@pytest.mark.anyio
async def test_remove_after_answer_still_works():
    """remove_pending_question still closes the question after it has been answered."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn, remove_fn = tools[0], tools[2]

    qid = await add_fn("Q1")
    store.answer(qid, "A1")
    result = await remove_fn(qid)
    assert "Removed" in result
    assert len(store.list_for_session("sess-1")) == 0


@pytest.mark.anyio
async def test_get_tool_unknown_id():
    """The get tool returns an error for unknown ids."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    get_fn = tools[4]
    result = await get_fn("nonexistent")
    assert "Unknown" in result


# ---------------------------------------------------------------------------
# Thread tools — append_to_pending_question_thread / get_pending_question_thread
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_append_thread_tool_adds_message():
    """The append_thread tool adds an assistant message to the thread."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn, append_fn = tools[0], tools[5]

    qid = await add_fn("Q1")
    result = await append_fn(qid, "Follow-up context")
    assert "Appended" in result

    entry = store.get(qid)
    assert entry is not None
    assert len(entry.thread) == 1
    assert entry.thread[0].role == "assistant"
    assert entry.thread[0].text == "Follow-up context"


@pytest.mark.anyio
async def test_append_thread_tool_empty_text():
    """The append_thread tool returns an error for empty text."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn, append_fn = tools[0], tools[5]

    qid = await add_fn("Q1")
    result = await append_fn(qid, "")
    assert "Error" in result or "empty" in result.lower()


@pytest.mark.anyio
async def test_append_thread_tool_unknown_id():
    """The append_thread tool returns an error for unknown ids."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    append_fn = tools[5]
    result = await append_fn("nonexistent", "msg")
    assert "Unknown" in result


@pytest.mark.anyio
async def test_get_thread_tool_returns_messages():
    """The get_thread tool returns formatted thread messages."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn, append_fn, get_thread_fn = tools[0], tools[5], tools[6]

    qid = await add_fn("Q1")
    await append_fn(qid, "First message")
    store.append_to_thread(qid, "user", "User reply")

    result = await get_thread_fn(qid)
    assert "[ASSISTANT] First message" in result
    assert "[USER] User reply" in result


@pytest.mark.anyio
async def test_get_thread_tool_empty():
    """The get_thread tool returns a clear message for an empty thread."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    add_fn, get_thread_fn = tools[0], tools[6]

    qid = await add_fn("Q1")
    result = await get_thread_fn(qid)
    assert "No thread messages" in result


@pytest.mark.anyio
async def test_get_thread_tool_unknown_id():
    """The get_thread tool returns an error for unknown ids."""
    settings = PendingQuestionsSettings(enabled=True)
    store = PendingQuestionsStore()
    tools = build_pending_questions_tools(settings, store, session_id="sess-1")
    get_thread_fn = tools[6]
    result = await get_thread_fn("nonexistent")
    assert "Unknown" in result

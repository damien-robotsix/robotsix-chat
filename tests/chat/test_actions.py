"""Tests for the per-turn actions log (:mod:`robotsix_chat.chat.actions`)."""

from __future__ import annotations

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from robotsix_chat.chat.actions import (
    MAX_ACTION_ENTRIES,
    MAX_ACTION_ENTRY_CHARS,
    actions_from_messages,
    collect_actions,
    current_actions,
    format_action,
    record_action,
)


def test_format_action_renders_compact_one_liner() -> None:
    """Args and result are flattened to one line and joined with an arrow."""
    entry = format_action(
        "create_ticket",
        {"title": "Volume file-write\ncapability", "board": "chat"},
        {"ticket_id": "20260830T144100Z-volume-file-write-04d8"},
    )
    assert entry.startswith("create_ticket(")
    assert "\n" not in entry
    assert "20260830T144100Z-volume-file-write-04d8" in entry
    assert " -> " in entry


def test_format_action_marks_errors_and_caps_length() -> None:
    """Errors are prefixed; an oversized entry is truncated to the cap."""
    entry = format_action("http_get", "x" * 500, "y" * 500, is_error=True)
    assert "ERROR" in entry
    assert len(entry) <= MAX_ACTION_ENTRY_CHARS


def test_format_action_without_result_has_no_arrow() -> None:
    """A call whose result never arrived is rendered as the bare call."""
    assert format_action("restart_component", "chat") == "restart_component(chat)"


def test_record_action_is_noop_without_collector() -> None:
    """Outside :func:`collect_actions` nothing is collected and nothing raises."""
    assert current_actions() is None
    record_action("create_ticket", "x", "y")
    assert current_actions() is None


def test_collect_actions_captures_entries_and_skips_bookkeeping() -> None:
    """Entries land in the yielded list; memory recall is not an action."""
    with collect_actions() as entries:
        record_action("recall_memory", "hello")
        record_action("spawn_subsession", {"kind": "task"}, "sub-42")
        record_action("", "ignored")
    assert entries == ['spawn_subsession({"kind": "task"}) -> sub-42']
    assert current_actions() is None


def test_record_action_folds_overflow_into_marker() -> None:
    """Past the cap, further calls collapse into one ``… (+N more)`` marker."""
    with collect_actions() as entries:
        for i in range(MAX_ACTION_ENTRIES + 3):
            record_action("tool", str(i))
    assert len(entries) == MAX_ACTION_ENTRIES + 1
    assert entries[-1] == "… (+3 more actions)"


def test_actions_from_messages_pairs_calls_with_returns() -> None:
    """pydantic-ai tool-call / tool-return parts become paired entries."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="file it")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="create_ticket",
                    args={"title": "Volume file-write"},
                    tool_call_id="c1",
                ),
                ToolCallPart(tool_name="merge_pr", args={"pr": 812}, tool_call_id="c2"),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="create_ticket",
                    content={"ticket_id": "T-04d8"},
                    tool_call_id="c1",
                ),
                RetryPromptPart(
                    content="PR not mergeable", tool_name="merge_pr", tool_call_id="c2"
                ),
            ]
        ),
        ModelResponse(parts=[TextPart(content="Done.")]),
    ]
    entries = actions_from_messages(messages)
    assert len(entries) == 2
    assert entries[0].startswith("create_ticket(") and "T-04d8" in entries[0]
    assert entries[1].startswith("merge_pr(") and "ERROR" in entries[1]


def test_actions_from_messages_is_empty_without_tool_calls() -> None:
    """A text-only run (or an unrecognised result) yields no entries."""
    assert actions_from_messages([ModelResponse(parts=[TextPart(content="hi")])]) == []
    assert actions_from_messages([object()]) == []

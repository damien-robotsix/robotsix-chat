"""Tests for the writable knowledge base (store + tools).

Covers :class:`KnowledgeStore` persistence and the five
:func:`build_knowledge_tools` closures, following the patterns from
``tests/refdocs/test_refdocs.py`` and ``tests/mill/test_mill.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from robotsix_chat.config import KnowledgeSettings
from robotsix_chat.knowledge import build_knowledge_tools
from robotsix_chat.knowledge.store import KnowledgeStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixed_clock(iso: str) -> Callable[[], datetime]:
    """Return a callable that always returns the given UTC datetime."""

    def _clock() -> datetime:
        return datetime.fromisoformat(iso)

    return _clock


# ---------------------------------------------------------------------------
# build_knowledge_tools — gating
# ---------------------------------------------------------------------------


def test_build_knowledge_tools_disabled() -> None:
    """Disabled knowledge returns no tools."""
    assert build_knowledge_tools(KnowledgeSettings(enabled=False)) == []


def test_build_knowledge_tools_returns_six_tools() -> None:
    """Enabled knowledge returns exactly six named tools."""
    tools = build_knowledge_tools(KnowledgeSettings())
    names = {t.__name__ for t in tools}
    assert names == {
        "add_knowledge_note",
        "append_to_knowledge_note",
        "update_knowledge_note",
        "list_knowledge_notes",
        "search_knowledge_notes",
        "read_knowledge_note",
    }


def test_build_knowledge_tools_disabled_via_settings() -> None:
    """Passing enabled=False to KnowledgeSettings returns []."""
    settings = KnowledgeSettings(enabled=False)
    assert build_knowledge_tools(settings) == []


# ---------------------------------------------------------------------------
# KnowledgeStore — basic CRUD
# ---------------------------------------------------------------------------


def test_add_creates_entry_with_timestamps(tmp_path: Path) -> None:
    """add() creates an entry with topic, content, and deterministic timestamps."""
    clock = _fixed_clock("2025-07-15T10:30:00+00:00")
    store = KnowledgeStore(tmp_path / "k.json", clock=clock)

    entry = store.add("config", "Server port is 8000")

    assert entry.topic == "config"
    assert entry.content == "Server port is 8000"
    assert entry.created_at == "2025-07-15T10:30:00+00:00"
    assert entry.updated_at == "2025-07-15T10:30:00+00:00"
    assert len(entry.id) == 32  # uuid4().hex


def test_append_concatenates_and_bumps_updated_at(tmp_path: Path) -> None:
    """append() concatenates content and bumps updated_at only."""
    clock = _fixed_clock("2025-07-15T10:00:00+00:00")
    store = KnowledgeStore(tmp_path / "k.json", clock=clock)

    entry = store.add("topic", "line1.")
    assert entry.updated_at == "2025-07-15T10:00:00+00:00"

    # Advance clock.
    store._clock = _fixed_clock("2025-07-15T11:00:00+00:00")
    updated = store.append(entry.id, "\nline2.")

    assert updated.id == entry.id
    assert updated.content == "line1.\nline2."
    assert updated.created_at == "2025-07-15T10:00:00+00:00"  # unchanged
    assert updated.updated_at == "2025-07-15T11:00:00+00:00"  # bumped


def test_update_replaces_content(tmp_path: Path) -> None:
    """update() replaces content entirely."""
    clock = _fixed_clock("2025-07-15T10:00:00+00:00")
    store = KnowledgeStore(tmp_path / "k.json", clock=clock)

    entry = store.add("topic", "old")

    store._clock = _fixed_clock("2025-07-15T12:00:00+00:00")
    updated = store.update(entry.id, "new")

    assert updated.content == "new"
    assert updated.updated_at == "2025-07-15T12:00:00+00:00"


def test_list_filters_by_topic(tmp_path: Path) -> None:
    """list() returns all entries, with optional case-insensitive topic filter."""
    store = KnowledgeStore(tmp_path / "k.json")
    e1 = store.add("config", "a")
    e2 = store.add("config", "b")
    store.add("python", "c")  # third entry, different topic

    all_entries = store.list()
    assert len(all_entries) == 3

    config_entries = store.list("config")
    assert len(config_entries) == 2
    assert {e.id for e in config_entries} == {e1.id, e2.id}

    # case-insensitive
    config_upper = store.list("CONFIG")
    assert len(config_upper) == 2

    no_match = store.list("nonexistent")
    assert no_match == []


def test_list_empty_topic_returns_all(tmp_path: Path) -> None:
    """Empty string topic returns all entries (the default parameter)."""
    store = KnowledgeStore(tmp_path / "k.json")
    store.add("a", "1")
    store.add("b", "2")
    assert len(store.list("")) == 2
    assert len(store.list()) == 2


def test_read_returns_content(tmp_path: Path) -> None:
    """get() returns the entry for a known id."""
    store = KnowledgeStore(tmp_path / "k.json")
    entry = store.add("topic", "hello world")
    assert store.get(entry.id) is entry


# ---------------------------------------------------------------------------
# KnowledgeStore — search
# ---------------------------------------------------------------------------


def test_search_empty_query_returns_empty(tmp_path: Path) -> None:
    """An empty or whitespace-only query returns an empty list."""
    store = KnowledgeStore(tmp_path / "k.json")
    store.add("config", "some content")
    assert store.search("") == []
    assert store.search("   ") == []


def test_search_no_matches_returns_empty(tmp_path: Path) -> None:
    """A query with no matches returns an empty list."""
    store = KnowledgeStore(tmp_path / "k.json")
    store.add("config", "some content")
    assert store.search("nonexistent") == []


def test_search_matches_topic(tmp_path: Path) -> None:
    """search() finds notes by topic (case-insensitive)."""
    store = KnowledgeStore(tmp_path / "k.json")
    e1 = store.add("Config", "content A")
    store.add("Deploy", "content B")
    store.add("Python", "content C")

    results = store.search("config")
    assert len(results) == 1
    assert results[0].id == e1.id

    # case-insensitive
    results_upper = store.search("CONFIG")
    assert len(results_upper) == 1
    assert results_upper[0].id == e1.id


def test_search_matches_content(tmp_path: Path) -> None:
    """search() finds notes by content (case-insensitive)."""
    store = KnowledgeStore(tmp_path / "k.json")
    e1 = store.add("topic-a", "contains the word DIAGNOSTIC here")
    store.add("topic-b", "something else")
    store.add("topic-c", "unrelated")

    results = store.search("diagnostic")
    assert len(results) == 1
    assert results[0].id == e1.id


def test_search_ranking(tmp_path: Path) -> None:
    """Results are ranked: exact topic > topic contains > content contains."""
    store = KnowledgeStore(tmp_path / "k.json")
    e_exact = store.add("deploy", "some content")
    e_topic_contains = store.add("deployment-guide", "some content")
    e_content = store.add("misc", "discusses deploy strategies")

    results = store.search("deploy")
    assert len(results) == 3
    assert results[0].id == e_exact.id
    assert results[1].id == e_topic_contains.id
    assert results[2].id == e_content.id


def test_search_multiple_same_score(tmp_path: Path) -> None:
    """Multiple notes with the same score are all returned."""
    store = KnowledgeStore(tmp_path / "k.json")
    store.add("config", "mentions deploy in content")
    store.add("deploy-notes", "deploy stuff")
    store.add("deploy-logs", "more deploy info")

    results = store.search("deploy")
    # Two topic-contains matches + one content match
    assert len(results) == 3


# ---------------------------------------------------------------------------
# Error handling — missing id
# ---------------------------------------------------------------------------


def test_append_unknown_id_returns_error_entry(tmp_path: Path) -> None:
    """append() on an unknown id returns an entry with id='error'."""
    store = KnowledgeStore(tmp_path / "k.json")
    result = store.append("nonexistent", "stuff")
    assert result.id == "error"
    assert "nonexistent" in result.content.lower()


def test_update_unknown_id_returns_error_entry(tmp_path: Path) -> None:
    """update() on an unknown id returns an entry with id='error'."""
    store = KnowledgeStore(tmp_path / "k.json")
    result = store.update("nonexistent", "stuff")
    assert result.id == "error"
    assert "nonexistent" in result.content.lower()


def test_get_unknown_id_returns_none(tmp_path: Path) -> None:
    """get() on an unknown id returns None."""
    store = KnowledgeStore(tmp_path / "k.json")
    assert store.get("nonexistent") is None


# ---------------------------------------------------------------------------
# Tool-level error strings (should not raise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_knowledge_note_unknown_id_returns_error_string(
    tmp_path: Path,
) -> None:
    """The read tool returns a clear error string for unknown ids."""
    tools = build_knowledge_tools(KnowledgeSettings(path=str(tmp_path / "k.json")))
    read_tool = [t for t in tools if t.__name__ == "read_knowledge_note"][0]

    result = await read_tool("nonexistent")
    assert isinstance(result, str)
    assert "nonexistent" in result.lower()
    assert "error" in result.lower() or "no knowledge note" in result.lower()


@pytest.mark.asyncio
async def test_append_knowledge_note_unknown_id_returns_error_string(
    tmp_path: Path,
) -> None:
    """The append tool returns a clear error string for unknown ids."""
    tools = build_knowledge_tools(KnowledgeSettings(path=str(tmp_path / "k.json")))
    append_tool = [t for t in tools if t.__name__ == "append_to_knowledge_note"][0]

    result = await append_tool("nonexistent", "more")
    assert isinstance(result, str)
    assert "nonexistent" in result.lower()


@pytest.mark.asyncio
async def test_update_knowledge_note_unknown_id_returns_error_string(
    tmp_path: Path,
) -> None:
    """The update tool returns a clear error string for unknown ids."""
    tools = build_knowledge_tools(KnowledgeSettings(path=str(tmp_path / "k.json")))
    update_tool = [t for t in tools if t.__name__ == "update_knowledge_note"][0]

    result = await update_tool("nonexistent", "new content")
    assert isinstance(result, str)
    assert "nonexistent" in result.lower()


# ---------------------------------------------------------------------------
# Tool integration — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_read_knowledge_note(tmp_path: Path) -> None:
    """The tool closures wire add → read correctly."""
    tools = build_knowledge_tools(KnowledgeSettings(path=str(tmp_path / "k.json")))
    add_tool = [t for t in tools if t.__name__ == "add_knowledge_note"][0]
    read_tool = [t for t in tools if t.__name__ == "read_knowledge_note"][0]

    add_result = await add_tool("test-topic", "note content")
    assert "Created knowledge note" in add_result
    # Extract the id from the result string.
    note_id = add_result.split()[3]  # "Created knowledge note <id> (topic: ...)"

    read_result = await read_tool(note_id)
    assert read_result == "note content"


@pytest.mark.asyncio
async def test_list_knowledge_notes_shows_entries(tmp_path: Path) -> None:
    """The list tool returns formatted entries."""
    tools = build_knowledge_tools(KnowledgeSettings(path=str(tmp_path / "k.json")))
    add_tool = [t for t in tools if t.__name__ == "add_knowledge_note"][0]
    list_tool = [t for t in tools if t.__name__ == "list_knowledge_notes"][0]

    await add_tool("config", "some config note")
    result = await list_tool()

    assert "config" in result
    assert "some config note" in result


@pytest.mark.asyncio
async def test_list_knowledge_notes_empty_returns_message(tmp_path: Path) -> None:
    """The list tool returns a clear message when there are no notes."""
    tools = build_knowledge_tools(KnowledgeSettings(path=str(tmp_path / "k.json")))
    list_tool = [t for t in tools if t.__name__ == "list_knowledge_notes"][0]

    result = await list_tool()
    assert "No knowledge notes found" in result


@pytest.mark.asyncio
async def test_search_knowledge_notes_finds_matches(tmp_path: Path) -> None:
    """The search tool returns formatted results for matching notes."""
    tools = build_knowledge_tools(KnowledgeSettings(path=str(tmp_path / "k.json")))
    add_tool = [t for t in tools if t.__name__ == "add_knowledge_note"][0]
    search_tool = [t for t in tools if t.__name__ == "search_knowledge_notes"][0]

    await add_tool("config", "server port is 8000")
    await add_tool("deploy", "deployed to production")

    result = await search_tool("config")
    assert "server port is 8000" in result
    assert "deploy" not in result  # only config topic matches


@pytest.mark.asyncio
async def test_search_knowledge_notes_no_matches(tmp_path: Path) -> None:
    """The search tool returns a clear message when no notes match."""
    tools = build_knowledge_tools(KnowledgeSettings(path=str(tmp_path / "k.json")))
    add_tool = [t for t in tools if t.__name__ == "add_knowledge_note"][0]
    search_tool = [t for t in tools if t.__name__ == "search_knowledge_notes"][0]

    await add_tool("config", "some content")
    result = await search_tool("nonexistent")
    assert "No knowledge notes found matching" in result


@pytest.mark.asyncio
async def test_search_knowledge_notes_searches_content(tmp_path: Path) -> None:
    """The search tool finds notes by content, not just topic."""
    tools = build_knowledge_tools(KnowledgeSettings(path=str(tmp_path / "k.json")))
    add_tool = [t for t in tools if t.__name__ == "add_knowledge_note"][0]
    search_tool = [t for t in tools if t.__name__ == "search_knowledge_notes"][0]

    await add_tool("misc", "empty-diff bug fix was merged in commit abc123")

    result = await search_tool("empty-diff")
    assert "empty-diff" in result
    assert "abc123" in result


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


def test_persistence_round_trip(tmp_path: Path) -> None:
    """A new KnowledgeStore over the same path sees prior entries."""
    path = tmp_path / "k.json"

    store1 = KnowledgeStore(path)
    e = store1.add("topic", "persisted content")

    # Open a new store over the same path.
    store2 = KnowledgeStore(path)
    loaded = store2.get(e.id)
    assert loaded is not None
    assert loaded.topic == "topic"
    assert loaded.content == "persisted content"
    assert loaded.created_at == e.created_at
    assert loaded.updated_at == e.updated_at


def test_load_missing_file_starts_empty(tmp_path: Path) -> None:
    """A missing file is treated as an empty store (no crash)."""
    store = KnowledgeStore(tmp_path / "nonexistent.json")
    assert store.list() == []


def test_load_empty_file_starts_empty(tmp_path: Path) -> None:
    """An empty file is treated as an empty store."""
    path = tmp_path / "k.json"
    path.write_text("", encoding="utf-8")
    store = KnowledgeStore(path)
    assert store.list() == []


def test_load_corrupt_file_starts_empty(tmp_path: Path) -> None:
    """A corrupt (non-JSON) file is treated as empty."""
    path = tmp_path / "k.json"
    path.write_text("this is not json", encoding="utf-8")
    store = KnowledgeStore(path)
    assert store.list() == []


def test_load_non_list_json_starts_empty(tmp_path: Path) -> None:
    """A JSON file that is not a list is treated as empty."""
    path = tmp_path / "k.json"
    path.write_text('{"not": "a list"}', encoding="utf-8")
    store = KnowledgeStore(path)
    assert store.list() == []


def test_load_malformed_entry_skipped(tmp_path: Path) -> None:
    """A JSON array with a non-dict entry is silently skipped."""
    path = tmp_path / "k.json"
    path.write_text(
        json.dumps(
            [
                "just a string, not a dict",
                {
                    "id": "abc123",
                    "topic": "good",
                    "content": "valid entry",
                    "created_at": "2025-01-01T00:00:00+00:00",
                    "updated_at": "2025-01-01T00:00:00+00:00",
                },
            ]
        ),
        encoding="utf-8",
    )
    store = KnowledgeStore(path)
    entries = store.list()
    assert len(entries) == 1
    assert entries[0].id == "abc123"


# ---------------------------------------------------------------------------
# Snippet truncation in list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_knowledge_notes_truncates_long_content(tmp_path: Path) -> None:
    """The list tool truncates content snippets with a … marker."""
    tools = build_knowledge_tools(KnowledgeSettings(path=str(tmp_path / "k.json")))
    add_tool = [t for t in tools if t.__name__ == "add_knowledge_note"][0]
    list_tool = [t for t in tools if t.__name__ == "list_knowledge_notes"][0]

    long_content = "x" * 500  # longer than _LIST_SNIPPET_LENGTH (200)
    await add_tool("test", long_content)

    result = await list_tool()
    # The snippet should be truncated.
    assert "…" in result
    # The full long content should NOT appear.
    assert long_content not in result

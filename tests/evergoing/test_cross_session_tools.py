"""Tests for the evergoing cross-session awareness tools.

Covers the tool factory (:func:`build_cross_session_tools`) behaviour —
``list_sessions`` / ``create_session`` / ``close_session`` — plus their
wiring into the main chat agent's per-request tool factory when
``evergoing.enabled`` is set.
"""

from __future__ import annotations

import json
from itertools import count
from typing import Any

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.server.app import _build_request_tools_factory
from robotsix_chat.config import Settings
from robotsix_chat.config.models import EvergoingSettings
from robotsix_chat.evergoing import (
    build_cross_session_tools,
    load_cross_session_skill,
)

_OWNER = "operator"


def _store() -> ConversationStore:
    """Build a store with deterministic session ids (``s0``, ``s1``, …)."""
    ids = count()
    return ConversationStore(session_factory=lambda: f"s{next(ids)}")


def _tool_map(store: ConversationStore, session_id: str) -> dict[str, Any]:
    tools = build_cross_session_tools(conversation_store=store, session_id=session_id)
    return {t.__name__: t for t in tools}


def test_builder_exposes_the_three_tools() -> None:
    store = _store()
    tools = build_cross_session_tools(conversation_store=store, session_id="s0")
    assert sorted(t.__name__ for t in tools) == [
        "close_session",
        "create_session",
        "list_sessions",
    ]


@pytest.mark.asyncio
async def test_list_sessions_enumerates_owner_sessions() -> None:
    store = _store()
    # Lazily create the owner's default session; that is the caller session.
    _, caller = store.list_sessions(_OWNER)
    tools = _tool_map(store, caller)

    result = json.loads(await tools["list_sessions"]())
    assert result["caller_session_id"] == caller
    assert result["active_session_id"] == caller
    session_ids = {s["session_id"] for s in result["sessions"]}
    assert caller in session_ids


@pytest.mark.asyncio
async def test_create_session_spawns_under_same_owner() -> None:
    store = _store()
    _, caller = store.list_sessions(_OWNER)
    tools = _tool_map(store, caller)

    result = json.loads(await tools["create_session"]())
    assert result["created"] is True
    new_id = result["session"]["session_id"]
    assert new_id != caller
    # The new session is owned by the caller's owner.
    assert store.owner_for_session(new_id) == _OWNER


@pytest.mark.asyncio
async def test_close_session_closes_another_session() -> None:
    store = _store()
    _, caller = store.list_sessions(_OWNER)
    other = store.create_session(_OWNER)["session_id"]
    tools = _tool_map(store, caller)

    result = json.loads(await tools["close_session"](other))
    assert result["closed"] is True
    assert store.is_session_closed(other) is True


@pytest.mark.asyncio
async def test_close_session_refuses_self() -> None:
    store = _store()
    _, caller = store.list_sessions(_OWNER)
    tools = _tool_map(store, caller)

    result = json.loads(await tools["close_session"](caller))
    assert result["closed"] is False
    assert "evergoing" in result["reason"]
    assert store.is_session_closed(caller) is False


@pytest.mark.asyncio
async def test_close_session_unknown_session_reports_not_found() -> None:
    store = _store()
    _, caller = store.list_sessions(_OWNER)
    tools = _tool_map(store, caller)

    result = json.loads(await tools["close_session"]("does-not-exist"))
    assert result["closed"] is False


@pytest.mark.asyncio
async def test_tools_report_error_when_caller_has_no_owner() -> None:
    store = _store()
    tools = _tool_map(store, "orphan-session")

    listed = json.loads(await tools["list_sessions"]())
    assert listed["sessions"] == []
    assert "error" in listed

    created = json.loads(await tools["create_session"]())
    assert "error" in created

    closed = json.loads(await tools["close_session"]("anything"))
    assert closed["closed"] is False


def test_request_factory_wires_cross_session_tools_when_enabled() -> None:
    store = _store()
    settings = Settings(evergoing=EvergoingSettings(enabled=True))
    factory = _build_request_tools_factory(
        settings, None, None, conversation_store=store
    )
    assert factory is not None
    names = {getattr(t, "__name__", "") for t in factory("s0")}
    assert {"list_sessions", "create_session", "close_session"} <= names


def test_request_factory_omits_cross_session_tools_when_disabled() -> None:
    store = _store()
    settings = Settings(evergoing=EvergoingSettings(enabled=False))
    factory = _build_request_tools_factory(
        settings, None, None, conversation_store=store
    )
    # No other per-request factories are configured here, so a disabled
    # evergoing feature yields no factory at all.
    if factory is not None:
        names = {getattr(t, "__name__", "") for t in factory("s0")}
        assert "list_sessions" not in names


def test_skill_doc_is_non_empty_and_names_the_tools() -> None:
    body = load_cross_session_skill()
    assert body.strip()
    for name in ("list_sessions", "create_session", "close_session"):
        assert name in body

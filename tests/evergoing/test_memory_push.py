"""Tests for the summary → memory-component push pipeline.

Covers the :class:`~robotsix_chat.memory_push.MemoryPush` client payload
contract (stable per-session ``document_id`` + ``update_mode="replace"`` —
the dedup story), the scheduler pushing after a compaction, the
close-of-conversation ``finalize_session`` path, and the strict
best-effort behavior (a memory outage never breaks the pass).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from robotsix_chat.chat.conversation import OPERATOR_OWNER, ConversationStore
from robotsix_chat.evergoing import EvergoingSummaryScheduler
from robotsix_chat.memory_push import MemoryPush, session_document_id

from .test_scheduler import _CountingAgent, _store_with_evergoing

MEMORY_URL = "http://memory:8080"

pytestmark = pytest.mark.asyncio


async def _drain_background_tasks() -> None:
    """Let fire-and-forget push tasks run to completion."""
    for _ in range(10):
        await asyncio.sleep(0)


class _RecordingPush(MemoryPush):
    """MemoryPush that records pushes instead of doing HTTP."""

    def __init__(self) -> None:
        super().__init__(MEMORY_URL)
        self.pushes: list[dict[str, object]] = []

    async def push_session_summary(
        self,
        *,
        owner_id: str,
        session_id: str,
        title: str,
        summary: str,
        final: bool = False,
    ) -> bool:
        self.pushes.append(
            {
                "owner_id": owner_id,
                "session_id": session_id,
                "title": title,
                "summary": summary,
                "final": final,
            }
        )
        return True


# ---------------------------------------------------------------------------
# MemoryPush client — payload contract
# ---------------------------------------------------------------------------


@respx.mock
async def test_push_payload_carries_replace_document_semantics() -> None:
    route = respx.post(f"{MEMORY_URL}/remember").mock(
        return_value=httpx.Response(201, json={"stored": True})
    )
    push = MemoryPush(MEMORY_URL)
    ok = await push.push_session_summary(
        owner_id="operator",
        session_id="abc123",
        title="Fleet work",
        summary="Summary text.",
    )
    assert ok is True
    body = json.loads(route.calls[0].request.content)
    assert body["document_id"] == session_document_id("abc123") == "chat-session-abc123"
    assert body["update_mode"] == "replace"
    assert body["owner_id"] == "operator"
    assert body["tags"] == ["chat-session-summary"]
    assert "rolling summary" in body["context"]


@respx.mock
async def test_push_final_flag_changes_context_only() -> None:
    route = respx.post(f"{MEMORY_URL}/remember").mock(
        return_value=httpx.Response(201, json={"stored": True})
    )
    push = MemoryPush(MEMORY_URL)
    await push.push_session_summary(
        owner_id="operator",
        session_id="abc123",
        title="Fleet work",
        summary="Final text.",
        final=True,
    )
    body = json.loads(route.calls[0].request.content)
    assert "final summary" in body["context"]
    # Same document, still replace — the last summary simply wins.
    assert body["document_id"] == "chat-session-abc123"
    assert body["update_mode"] == "replace"


@respx.mock
async def test_push_failure_is_swallowed() -> None:
    respx.post(f"{MEMORY_URL}/remember").mock(side_effect=httpx.ConnectError("down"))
    push = MemoryPush(MEMORY_URL)
    ok = await push.push_session_summary(
        owner_id="operator", session_id="s", title="t", summary="text"
    )
    assert ok is False


@respx.mock
async def test_push_rejection_is_swallowed() -> None:
    respx.post(f"{MEMORY_URL}/remember").mock(
        return_value=httpx.Response(502, json={"detail": "engine unreachable"})
    )
    push = MemoryPush(MEMORY_URL)
    ok = await push.push_session_summary(
        owner_id="operator", session_id="s", title="t", summary="text"
    )
    assert ok is False


async def test_empty_summary_is_not_pushed() -> None:
    push = MemoryPush(MEMORY_URL)
    ok = await push.push_session_summary(
        owner_id="operator", session_id="s", title="t", summary="   "
    )
    assert ok is False


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


async def test_compaction_pushes_summary_to_memory() -> None:
    store, sid = _store_with_evergoing(turns=8)
    agent = _CountingAgent("SUMMARY TEXT")
    push = _RecordingPush()
    scheduler = EvergoingSummaryScheduler(
        interval_seconds=3600,
        store=store,
        agent=agent,
        keep_recent_runs=5,
        memory_push=push,
    )
    result = await scheduler.run_once()
    await _drain_background_tasks()
    sessions = result["sessions"]
    assert isinstance(sessions, list) and sessions[0]["compacted"] is True
    assert len(push.pushes) == 1
    p = push.pushes[0]
    assert p["session_id"] == sid
    assert p["owner_id"] == OPERATOR_OWNER
    assert p["summary"] == "SUMMARY TEXT"
    assert p["final"] is False


async def test_no_push_when_gate_skips() -> None:
    store, _sid = _store_with_evergoing(turns=3)  # at most keep_recent_runs
    agent = _CountingAgent("SUMMARY TEXT")
    push = _RecordingPush()
    scheduler = EvergoingSummaryScheduler(
        interval_seconds=3600,
        store=store,
        agent=agent,
        keep_recent_runs=5,
        memory_push=push,
    )
    await scheduler.run_once()
    await _drain_background_tasks()
    assert push.pushes == []


async def test_no_push_without_memory_push_configured() -> None:
    store, _sid = _store_with_evergoing(turns=8)
    agent = _CountingAgent("SUMMARY TEXT")
    scheduler = EvergoingSummaryScheduler(
        interval_seconds=3600, store=store, agent=agent, keep_recent_runs=5
    )
    result = await scheduler.run_once()
    sessions = result["sessions"]
    assert isinstance(sessions, list) and sessions[0]["compacted"] is True


async def test_finalize_session_pushes_full_summary() -> None:
    store, sid = _store_with_evergoing(turns=4)
    agent = _CountingAgent("FINAL SUMMARY")
    push = _RecordingPush()
    scheduler = EvergoingSummaryScheduler(
        interval_seconds=3600,
        store=store,
        agent=agent,
        keep_recent_runs=5,
        memory_push=push,
    )
    ok = await scheduler.finalize_session(sid)
    await _drain_background_tasks()
    assert ok is True
    assert len(push.pushes) == 1
    assert push.pushes[0]["final"] is True
    assert push.pushes[0]["summary"] == "FINAL SUMMARY"
    # The finalize summariser saw the fresh runs the periodic gate skipped.
    assert agent.calls >= 1


async def test_finalize_unknown_session_is_noop() -> None:
    store = ConversationStore()
    agent = _CountingAgent("X")
    push = _RecordingPush()
    scheduler = EvergoingSummaryScheduler(
        interval_seconds=3600,
        store=store,
        agent=agent,
        keep_recent_runs=5,
        memory_push=push,
    )
    ok = await scheduler.finalize_session("missing")
    assert ok is False
    assert push.pushes == []

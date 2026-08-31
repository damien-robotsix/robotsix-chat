"""Tests for the evergoing trim scheduler + activation wiring.

Covers the scheduler's new-input gate (skip → zero LLM calls), the
agent-driven trim call, in-flight-turn safety, and the create-on-boot
activation path — not just the store primitives.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from robotsix_chat.chat.conversation import OPERATOR_OWNER, ConversationStore
from robotsix_chat.chat.server.app import create_app
from robotsix_chat.config.models import EvergoingSettings
from robotsix_chat.evergoing import EvergoingTrimScheduler


class _CountingAgent:
    """ChatAgent that records how many times ``stream`` was invoked."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
        trace_metadata: dict[str, str] | None = None,
        trace_name: str | None = None,
        model_level: int | None = None,
    ) -> AsyncIterator[str]:
        self.calls += 1
        yield self.reply


def _store_with_evergoing(turns: int) -> tuple[ConversationStore, str]:
    store = ConversationStore()
    meta = store.ensure_evergoing_session(OPERATOR_OWNER)
    sid = str(meta["session_id"])
    for i in range(turns):
        store.record(sid, OPERATOR_OWNER, f"user {i}", f"assistant {i}")
    return store, sid


def test_run_once_skips_when_no_sessions() -> None:
    store = ConversationStore()
    agent = _CountingAgent("SUBJECT_CHANGED: yes\nDROP_LEADING: 1")
    scheduler = EvergoingTrimScheduler(60.0, store, agent, keep_min_recent=2)

    result = asyncio.run(scheduler.run_once())

    assert result["sessions"] == []
    assert agent.calls == 0  # no LLM call


def _one(result: dict[str, object]) -> dict[str, object]:
    sessions = result["sessions"]
    assert isinstance(sessions, list) and len(sessions) == 1
    return sessions[0]


def test_run_once_trims_ordinary_sessions_too() -> None:
    """The scheduler is the single trim mechanism for ALL sessions."""
    store = ConversationStore()
    sid = str(store.create_session("owner-x")["session_id"])
    for i in range(4):
        store.record(sid, "owner-x", f"user {i}", f"assistant {i}")
    agent = _CountingAgent("SUBJECT_CHANGED: yes\nDROP_LEADING: 2")
    scheduler = EvergoingTrimScheduler(
        60.0, store, agent, keep_min_recent=2, min_fresh_turns=1
    )

    result = asyncio.run(scheduler.run_once())

    audit = _one(result)
    assert audit["session_id"] == sid
    assert audit["trimmed"] is True
    assert len(store.history(sid)) == 2


def test_fresh_turns_gate_skips_without_watermark_advance() -> None:
    """Below min_fresh_turns: no LLM call, and turns keep accumulating."""
    store, sid = _store_with_evergoing(turns=2)
    agent = _CountingAgent("SUBJECT_CHANGED: yes\nDROP_LEADING: 2")
    scheduler = EvergoingTrimScheduler(
        60.0, store, agent, keep_min_recent=1, min_fresh_turns=3
    )

    result = asyncio.run(scheduler.run_once())

    audit = _one(result)
    assert audit["reason"] == "below min_fresh_turns"
    assert agent.calls == 0
    # Watermark NOT advanced: the session still counts as having new input.
    assert store.has_new_input_since_trim(sid) is True
    # One more turn opens the gate.
    store.record(sid, OPERATOR_OWNER, "user 2", "assistant 2")
    result = asyncio.run(scheduler.run_once())
    assert agent.calls == 1


def test_run_once_skips_when_no_new_input() -> None:
    store, sid = _store_with_evergoing(turns=4)
    # Advance the trim watermark so there is no "new input".
    store.trim_session(sid, 0, keep_min_recent=2)
    assert store.has_new_input_since_trim(sid) is False

    agent = _CountingAgent("SUBJECT_CHANGED: yes\nDROP_LEADING: 2")
    scheduler = EvergoingTrimScheduler(60.0, store, agent, keep_min_recent=2)

    result = asyncio.run(scheduler.run_once())

    assert result["sessions"] == []
    assert agent.calls == 0  # the gate ran BEFORE any LLM call


def test_run_once_trims_on_new_input() -> None:
    store, sid = _store_with_evergoing(turns=4)
    agent = _CountingAgent("SUBJECT_CHANGED: yes\nDROP_LEADING: 2")
    scheduler = EvergoingTrimScheduler(
        60.0, store, agent, keep_min_recent=2, min_fresh_turns=1
    )

    result = asyncio.run(scheduler.run_once())
    result = _one(result)

    assert agent.calls == 1
    assert result["trimmed"] is True
    assert result["turns_trimmed"] == 2
    assert result["decided_subject_change"] is True
    # UI transcript now reflects the post-trim history.
    assert len(store.history(sid)) == 2
    # Watermark advanced: the next no-input pass is skipped.
    assert store.has_new_input_since_trim(sid) is False


def test_run_once_keeps_context_when_subject_unchanged() -> None:
    store, sid = _store_with_evergoing(turns=4)
    agent = _CountingAgent("SUBJECT_CHANGED: no\nDROP_LEADING: 3")
    scheduler = EvergoingTrimScheduler(
        60.0, store, agent, keep_min_recent=2, min_fresh_turns=1
    )

    result = asyncio.run(scheduler.run_once())
    result = _one(result)

    assert agent.calls == 1
    assert result["trimmed"] is False
    assert result["turns_trimmed"] == 0
    assert len(store.history(sid)) == 4  # nothing dropped


def test_in_flight_turn_never_trimmed() -> None:
    store, sid = _store_with_evergoing(turns=3)
    # Model over-asks; clamp must keep >= keep_min_recent recent turns.
    agent = _CountingAgent("SUBJECT_CHANGED: yes\nDROP_LEADING: 99")
    scheduler = EvergoingTrimScheduler(
        60.0, store, agent, keep_min_recent=2, min_fresh_turns=1
    )

    result = asyncio.run(scheduler.run_once())
    result = _one(result)

    assert result["turns_trimmed"] == 1  # 3 turns - keep_min_recent(2)
    assert len(store.history(sid)) == 2


def test_create_app_activates_evergoing_on_boot() -> None:
    store = ConversationStore()
    agent = _CountingAgent("SUBJECT_CHANGED: no\nDROP_LEADING: 0")

    app = create_app(
        agent,
        conversation_store=store,
        evergoing_settings=EvergoingSettings(enabled=True),
    )

    # Exactly one evergoing session exists and the scheduler is wired.
    sid = store.evergoing_session_id()
    assert sid is not None
    assert app.state.evergoing_scheduler is not None
    assert isinstance(app.state.evergoing_scheduler, EvergoingTrimScheduler)


def test_create_app_no_evergoing_session_by_default() -> None:
    store = ConversationStore()
    agent = _CountingAgent("")

    app = create_app(agent, conversation_store=store)

    assert store.evergoing_session_id() is None
    # The trim scheduler wiring no longer depends on the evergoing flag —
    # it exists whenever a summary agent does.
    assert not isinstance(app.state.evergoing_scheduler, str)


def test_trim_retires_overtaken_legacy_summary() -> None:
    """A trim that passes the compacted range drops the stale summary.

    Regression: legacy compacted summaries ratcheted forward with every trim
    (compacted_turn_index = max(...)), so the UI showed an ever-growing
    "summary of earlier exchanges" block with never-updated content.
    """
    store, sid = _store_with_evergoing(turns=6)
    session = store.get_session(sid)
    assert session is not None
    session.compacted_summary = "stale summary"
    session.compacted_turn_index = 2

    store.trim_session(sid, 4, keep_min_recent=1)

    session = store.get_session(sid)
    assert session is not None
    assert session.compacted_summary is None
    assert session.compacted_turn_index == 4


def test_trim_below_summary_keeps_it() -> None:
    store, sid = _store_with_evergoing(turns=6)
    session = store.get_session(sid)
    assert session is not None
    session.compacted_summary = "still-covering summary"
    session.compacted_turn_index = 5

    store.trim_session(sid, 2, keep_min_recent=1)

    session = store.get_session(sid)
    assert session is not None
    assert session.compacted_summary == "still-covering summary"
    assert session.compacted_turn_index == 5

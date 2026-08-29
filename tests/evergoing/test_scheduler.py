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


def test_run_once_skips_when_no_evergoing_session() -> None:
    store = ConversationStore()
    agent = _CountingAgent("SUBJECT_CHANGED: yes\nDROP_LEADING: 1")
    scheduler = EvergoingTrimScheduler(60.0, store, agent, keep_min_recent=2)

    result = asyncio.run(scheduler.run_once())

    assert result["trimmed"] is False
    assert result["reason"] == "no evergoing session"
    assert agent.calls == 0  # no LLM call


def test_run_once_skips_when_no_new_input() -> None:
    store, sid = _store_with_evergoing(turns=4)
    # Advance the trim watermark so there is no "new input".
    store.trim_session(sid, 0, keep_min_recent=2)
    assert store.has_new_input_since_trim(sid) is False

    agent = _CountingAgent("SUBJECT_CHANGED: yes\nDROP_LEADING: 2")
    scheduler = EvergoingTrimScheduler(60.0, store, agent, keep_min_recent=2)

    result = asyncio.run(scheduler.run_once())

    assert result["trimmed"] is False
    assert result["reason"] == "no new input since last trim"
    assert agent.calls == 0  # the gate ran BEFORE any LLM call


def test_run_once_trims_on_new_input() -> None:
    store, sid = _store_with_evergoing(turns=4)
    agent = _CountingAgent("SUBJECT_CHANGED: yes\nDROP_LEADING: 2")
    scheduler = EvergoingTrimScheduler(60.0, store, agent, keep_min_recent=2)

    result = asyncio.run(scheduler.run_once())

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
    scheduler = EvergoingTrimScheduler(60.0, store, agent, keep_min_recent=2)

    result = asyncio.run(scheduler.run_once())

    assert agent.calls == 1
    assert result["trimmed"] is False
    assert result["turns_trimmed"] == 0
    assert len(store.history(sid)) == 4  # nothing dropped


def test_in_flight_turn_never_trimmed() -> None:
    store, sid = _store_with_evergoing(turns=3)
    # Model over-asks; clamp must keep >= keep_min_recent recent turns.
    agent = _CountingAgent("SUBJECT_CHANGED: yes\nDROP_LEADING: 99")
    scheduler = EvergoingTrimScheduler(60.0, store, agent, keep_min_recent=2)

    result = asyncio.run(scheduler.run_once())

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


def test_create_app_evergoing_disabled_by_default() -> None:
    store = ConversationStore()
    agent = _CountingAgent("")

    app = create_app(agent, conversation_store=store)

    assert store.evergoing_session_id() is None
    assert app.state.evergoing_scheduler is None

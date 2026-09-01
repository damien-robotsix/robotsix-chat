"""Tests for the evergoing summary scheduler + activation wiring.

Covers the scheduler's deterministic gate (new-input skip → zero LLM
calls; at most ``keep_recent_runs`` fresh runs → skip without watermark
advance), the summarise-not-drop behavior (last runs kept verbatim and
excluded from the summariser input), and the create-on-boot activation
path — not just the store primitives.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from robotsix_chat.chat.conversation import OPERATOR_OWNER, ConversationStore
from robotsix_chat.chat.server.app import create_app
from robotsix_chat.config.models import EvergoingSettings
from robotsix_chat.evergoing import EvergoingSummaryScheduler


class _CountingAgent:
    """ChatAgent that records prompts and how often ``stream`` was invoked."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.prompts: list[str] = []

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
        self.prompts.append(message)
        yield self.reply


def _store_with_evergoing(turns: int) -> tuple[ConversationStore, str]:
    store = ConversationStore()
    meta = store.ensure_evergoing_session(OPERATOR_OWNER)
    sid = str(meta["session_id"])
    for i in range(turns):
        store.record(sid, OPERATOR_OWNER, f"user {i}", f"assistant {i}")
    return store, sid


def _one(result: dict[str, object]) -> dict[str, object]:
    sessions = result["sessions"]
    assert isinstance(sessions, list) and len(sessions) == 1
    return sessions[0]


def test_run_once_skips_when_no_sessions() -> None:
    store = ConversationStore()
    agent = _CountingAgent("a summary")
    scheduler = EvergoingSummaryScheduler(60.0, store, agent)

    result = asyncio.run(scheduler.run_once())

    assert result["sessions"] == []
    assert agent.calls == 0  # no LLM call


def test_gate_skips_at_keep_recent_runs_without_watermark_advance() -> None:
    """With exactly keep_recent_runs fresh runs: no LLM call, runs accumulate."""
    store, sid = _store_with_evergoing(turns=5)
    agent = _CountingAgent("a summary")
    scheduler = EvergoingSummaryScheduler(60.0, store, agent, keep_recent_runs=5)

    result = asyncio.run(scheduler.run_once())

    audit = _one(result)
    assert audit["compacted"] is False
    assert audit["reason"] == "at most keep_recent_runs fresh"
    assert agent.calls == 0
    # Watermark NOT advanced: the session still counts as having new input.
    assert store.has_new_input_since_trim(sid) is True
    # One more run opens the gate.
    store.record(sid, OPERATOR_OWNER, "user 5", "assistant 5")
    result = asyncio.run(scheduler.run_once())
    assert agent.calls == 1
    assert _one(result)["compacted"] is True


def test_compaction_keeps_last_runs_verbatim_and_out_of_summary() -> None:
    """Everything BEFORE the last keep_recent_runs runs is summarised.

    The recent runs stay verbatim and are never shown to the summariser.
    """
    store, sid = _store_with_evergoing(turns=8)
    agent = _CountingAgent("a summary of the early runs")
    scheduler = EvergoingSummaryScheduler(60.0, store, agent, keep_recent_runs=5)

    result = asyncio.run(scheduler.run_once())

    audit = _one(result)
    assert audit["compacted"] is True
    assert audit["turns_folded"] == 3
    # Summariser saw the folded turns only — never the recent five.
    assert agent.calls == 1
    prompt = agent.prompts[0]
    assert "user 2" in prompt
    assert "user 3" not in prompt and "assistant 7" not in prompt
    # Replay = summary + last 5 runs verbatim; UI transcript untouched.
    replay = store.agent_history(sid)
    assert len(replay) == 6
    assert "a summary of the early runs" in replay[0][1]
    assert replay[1] == ("user 3", "assistant 3")
    assert len(store.history(sid)) == 8
    # Watermark advanced: the next no-input pass is skipped.
    assert store.has_new_input_since_trim(sid) is False
    result = asyncio.run(scheduler.run_once())
    assert result["sessions"] == []
    assert agent.calls == 1


def test_previous_summary_is_folded_into_the_new_one() -> None:
    store, sid = _store_with_evergoing(turns=8)
    session = store.get_session(sid)
    assert session is not None
    session.compacted_summary = "earlier summary content"
    session.compacted_turn_index = 2

    agent = _CountingAgent("merged summary")
    scheduler = EvergoingSummaryScheduler(60.0, store, agent, keep_recent_runs=5)
    result = asyncio.run(scheduler.run_once())

    assert _one(result)["compacted"] is True
    # The old summary rides into the summariser as the synthetic lead turn.
    assert "earlier summary content" in agent.prompts[0]
    session = store.get_session(sid)
    assert session is not None
    assert session.compacted_summary == "merged summary"
    assert session.compacted_turn_index == 3


def test_summary_failure_leaves_session_untouched() -> None:
    store, sid = _store_with_evergoing(turns=7)
    agent = _CountingAgent("")  # summariser failure → empty text
    scheduler = EvergoingSummaryScheduler(60.0, store, agent, keep_recent_runs=5)

    result = asyncio.run(scheduler.run_once())

    audit = _one(result)
    assert audit["compacted"] is False
    assert audit["reason"] == "summary generation failed"
    session = store.get_session(sid)
    assert session is not None
    assert session.compacted_summary is None
    # Watermark NOT advanced — the next pass retries.
    assert store.has_new_input_since_trim(sid) is True


def test_compact_session_cover_until_index() -> None:
    """The explicit snapshot boundary wins over keep_recent_turns."""
    store, sid = _store_with_evergoing(turns=6)

    store.compact_session("", sid, "snap summary", cover_until_index=3)

    session = store.get_session(sid)
    assert session is not None
    assert session.compacted_turn_index == 3
    assert session.compacted_summary == "snap summary"
    # Monotonic: a later compaction can never uncover turns.
    store.compact_session("", sid, "older-window summary", cover_until_index=1)
    session = store.get_session(sid)
    assert session is not None
    assert session.compacted_turn_index == 3


def test_legacy_settings_keys_are_dropped() -> None:
    """Configs pinning the removed trim fields must still load (extra=forbid)."""
    settings = EvergoingSettings.model_validate(
        {"keep_min_recent": 2, "min_fresh_turns": 3}
    )

    assert settings.keep_recent_runs == 5


def test_create_app_activates_evergoing_on_boot() -> None:
    store = ConversationStore()
    agent = _CountingAgent("a summary")

    app = create_app(
        agent,
        conversation_store=store,
        evergoing_settings=EvergoingSettings(enabled=True),
    )

    # Exactly one evergoing session exists and the scheduler is wired.
    sid = store.evergoing_session_id()
    assert sid is not None
    assert app.state.evergoing_scheduler is not None
    assert isinstance(app.state.evergoing_scheduler, EvergoingSummaryScheduler)


def test_create_app_no_evergoing_session_by_default() -> None:
    store = ConversationStore()
    agent = _CountingAgent("")

    app = create_app(agent, conversation_store=store)

    assert store.evergoing_session_id() is None
    # The scheduler wiring no longer depends on the evergoing flag —
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

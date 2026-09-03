"""Tests for the per-session ``escalate_model`` tool."""

from __future__ import annotations

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import SSE_SESSION_MODEL_TYPE
from robotsix_chat.config.constants import FRONTIER_MODEL_LEVEL
from robotsix_chat.llm.escalation import build_escalation_tools


class _RecordingSink:
    """EventSink double that records published frames."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, dict[str, object]]] = []

    def publish(self, session_id: str, frame: dict[str, object]) -> None:
        self.frames.append((session_id, frame))

    def publish_all(self, frame: dict[str, object]) -> None:
        self.frames.append(("*", frame))


def _store_with_session() -> tuple[ConversationStore, str]:
    store = ConversationStore()
    sessions, _ = store.list_sessions("owner")
    return store, str(sessions[0]["session_id"])


class TestToolExposure:
    """The tool is only offered when escalating is actually possible."""

    def test_offered_below_the_frontier_tier(self) -> None:
        """A session on the configured tier can still escalate."""
        store, sid = _store_with_session()
        tools = build_escalation_tools(
            conversation_store=store, session_id=sid, configured_level=2
        )
        assert [t.__name__ for t in tools] == ["escalate_model"]

    def test_withheld_at_the_frontier_tier(self) -> None:
        """Nothing to escalate to — don't spend a tool slot advertising it."""
        store, sid = _store_with_session()
        tools = build_escalation_tools(
            conversation_store=store,
            session_id=sid,
            configured_level=FRONTIER_MODEL_LEVEL,
        )
        assert tools == []

    def test_withheld_once_already_escalated(self) -> None:
        """A second escalation is a no-op, so the tool disappears."""
        store, sid = _store_with_session()
        store.set_model_level(sid, FRONTIER_MODEL_LEVEL)
        tools = build_escalation_tools(
            conversation_store=store, session_id=sid, configured_level=2
        )
        assert tools == []

    def test_withheld_without_a_store(self) -> None:
        """No store means the pin cannot be persisted."""
        assert (
            build_escalation_tools(
                conversation_store=None, session_id="s", configured_level=2
            )
            == []
        )


class TestEscalation:
    """Calling the tool pins the session and announces the change."""

    @pytest.mark.asyncio
    async def test_pins_the_session_and_persists(self) -> None:
        """The level is stored, so the next turn picks it up."""
        store, sid = _store_with_session()
        (escalate,) = build_escalation_tools(
            conversation_store=store, session_id=sid, configured_level=2
        )

        assert store.get_model_level(sid) is None
        result = await escalate(reason="cannot resolve the lock ordering")

        assert store.get_model_level(sid) == FRONTIER_MODEL_LEVEL
        assert "claude-fable-5" in result
        # The agent must not promise a better answer for the current turn.
        assert "NEXT" in result

    @pytest.mark.asyncio
    async def test_publishes_a_session_model_frame(self) -> None:
        """The UI badge updates without waiting for a session-list refetch."""
        store, sid = _store_with_session()
        sink = _RecordingSink()
        (escalate,) = build_escalation_tools(
            conversation_store=store,
            session_id=sid,
            configured_level=2,
            event_sink=sink,
        )

        await escalate(reason="needs deeper reasoning")

        assert len(sink.frames) == 1
        published_sid, frame = sink.frames[0]
        assert published_sid == sid
        assert frame["type"] == SSE_SESSION_MODEL_TYPE
        assert frame["model_level"] == FRONTIER_MODEL_LEVEL
        assert frame["model_name"] == "claude-fable-5"
        assert frame["escalated"] is True
        assert frame["reason"] == "needs deeper reasoning"

    @pytest.mark.asyncio
    async def test_unknown_session_does_not_raise(self) -> None:
        """A deleted session degrades to a message, not an exception."""
        store, sid = _store_with_session()
        (escalate,) = build_escalation_tools(
            conversation_store=store, session_id=sid, configured_level=2
        )
        store._sessions.pop(sid, None)

        result = await escalate(reason="whatever")
        assert "Escalation failed" in result


class TestPersistence:
    """The pin survives a store round-trip and shows up in metadata."""

    def test_round_trips_through_metadata(self) -> None:
        """``list_sessions`` carries the level so the API can annotate it."""
        store, sid = _store_with_session()
        store.set_model_level(sid, FRONTIER_MODEL_LEVEL)

        sessions, _ = store.list_sessions("owner")
        entry = next(s for s in sessions if s["session_id"] == sid)
        assert entry["model_level"] == FRONTIER_MODEL_LEVEL

    def test_unset_level_is_none(self) -> None:
        """An un-escalated session reports None, not a guessed default."""
        store, sid = _store_with_session()
        sessions, _ = store.list_sessions("owner")
        entry = next(s for s in sessions if s["session_id"] == sid)
        assert entry["model_level"] is None

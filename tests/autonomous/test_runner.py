"""Tests for the AutonomousRunner lifecycle and auto-continue logic."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_chat.autonomous.models import AutonomousSession, AutonomousState
from robotsix_chat.autonomous.runner import AutonomousRunner
from robotsix_chat.chat.conversation import (
    OPERATOR_OWNER,
    ConversationStore,
)
from robotsix_chat.chat.events import SSE_AGENT_MESSAGE_TYPE, SSE_AUTONOMOUS_TOKEN_TYPE


def _make_run_serializer() -> MagicMock:
    """Return a run serializer mock usable as an async context manager."""
    run_serializer = MagicMock()
    run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
    run_serializer.for_owner.return_value.__aexit__ = AsyncMock()
    return run_serializer


def _make_settings(**autonomous: object) -> MagicMock:
    """Return a mock Settings with sensible autonomous defaults."""
    settings = MagicMock()
    settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
    settings.autonomous.continue_interval_seconds = 0
    settings.autonomous.max_idle_auto_turns = 5
    settings.autonomous.sessions = []
    for key, value in autonomous.items():
        setattr(settings.autonomous, key, value)
    return settings


def _make_definition(name: str, prompt: str = "") -> SimpleNamespace:
    """Return a mock autonomous session definition matching config shapes."""
    return SimpleNamespace(
        name=name,
        prompt=prompt,
        trigger_type=SimpleNamespace(value="periodic"),
        trigger_interval_seconds=45.0,
        max_auto_turns=20,
        enabled=True,
        self_refine=False,
        self_refine_require_approval=False,
    )


class TestAutonomousRunnerSessionRegistry:
    """Session creation, lookup, and ownership tests."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    def test_create_session(self) -> None:
        """Creating a session registers it and returns correct metadata."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq = runner.create_session("owner1")
        assert aq.owner_id == "owner1"
        assert aq.state is AutonomousState.executing
        assert runner.is_autonomous(aq.session_id)
        assert runner.get_state(aq.session_id) is AutonomousState.executing

    def test_create_session_with_id(self) -> None:
        """A custom session_id is honoured."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq = runner.create_session("owner1", session_id="custom-id")
        assert aq.session_id == "custom-id"
        assert aq.owner_id == "owner1"

    def test_unknown_session(self) -> None:
        """All lookups return None/False for unregistered sessions."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        assert not runner.is_autonomous("nonexistent")
        assert runner.get_state("nonexistent") is None
        assert runner.get_session("nonexistent") is None
        assert runner.owner_for_session("nonexistent") is None


class TestCompletionMarkerDetection:
    """Completion marker detection and state transition tests."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    def _runner(self, *, registry: MagicMock | None = None) -> AutonomousRunner:
        store = ConversationStore()
        return AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
            subsession_registry=registry,
        )

    def test_completion_marker_transitions_to_completed(self) -> None:
        """Completion marker moves state to completed."""
        runner = self._runner()
        aq = runner.create_session("owner1")
        reply = "All done!\n\n---AUTONOMOUS COMPLETE---"
        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        assert new_state is AutonomousState.completed
        assert aq.state is AutonomousState.completed

    def test_no_marker_no_transition(self) -> None:
        """Reply without markers leaves state unchanged."""
        runner = self._runner()
        aq = runner.create_session("owner1")
        reply = "Working on it..."
        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        assert new_state is None
        assert aq.state is AutonomousState.executing

    def test_unknown_session_returns_none(self) -> None:
        """Marker scan on unknown session returns None."""
        runner = self._runner()
        result = runner.check_reply_for_markers("unknown", "---AUTONOMOUS COMPLETE---")
        assert result is None

    def test_completion_suppressed_when_active_subsessions(self) -> None:
        """Completion marker is ignored when non-periodic subsessions are running."""
        reg = MagicMock()
        reg.list_for_owner.return_value = [
            SimpleNamespace(is_active=True, kind="task"),
        ]
        runner = self._runner(registry=reg)
        aq = runner.create_session("owner1")
        reply = "All done!\n\n---AUTONOMOUS COMPLETE---"

        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        assert new_state is None
        assert aq.state is AutonomousState.executing
        assert aq.completion_suppressed is True

    def test_completion_not_suppressed_with_only_periodic_subsessions(self) -> None:
        """Completion is NOT suppressed when only periodic monitors are active."""
        reg = MagicMock()
        reg.list_for_owner.return_value = [
            SimpleNamespace(is_active=True, kind="periodic"),
        ]
        runner = self._runner(registry=reg)
        aq = runner.create_session("owner1")
        reply = "All done!\n\n---AUTONOMOUS COMPLETE---"

        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        assert new_state is AutonomousState.completed
        assert aq.state is AutonomousState.completed


class TestAutoContinue:
    """The merged kickoff + continuation loop runs until completion."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_initial_turn_uses_standard_prompt_then_closes(self) -> None:
        """First turn runs the standard kickoff prompt and closes on the marker."""
        store = ConversationStore()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _stream(message, *args, **kwargs):
            captured_message.append(str(message))
            yield "Work done.\n---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()

        aq = runner.create_session("owner1", schedule_kickoff=False)
        await runner._auto_continue(aq.session_id)

        assert (
            captured_message[0]
            == "Begin a new autonomous session and work it to completion."
        )
        assert aq.state is AutonomousState.completed
        assert aq.auto_turn_count == 1
        turns = store.history(aq.session_id)
        assert len(turns) == 1

    @pytest.mark.asyncio
    async def test_initial_turn_uses_custom_prompt(self) -> None:
        """The session definition's custom prompt drives the first turn."""
        store = ConversationStore()
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition("default", prompt="Run the nightly triage now."),
        ]
        run_serializer = _make_run_serializer()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _stream(message, *args, **kwargs):
            captured_message.append(str(message))
            yield "Triage complete.\n---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()

        owner_id = runner.owner_id_for_definition("default")
        aq = runner.create_session(owner_id, schedule_kickoff=False)
        await runner._auto_continue(aq.session_id)

        assert captured_message[0] == "Run the nightly triage now."
        assert aq.state is AutonomousState.completed

    @pytest.mark.asyncio
    async def test_auto_continue_continues_until_completion(self) -> None:
        """Subsequent turns run Continue until the agent emits the marker."""
        store = ConversationStore()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _stream(message, *args, **kwargs):
            captured_message.append(str(message))
            if len(captured_message) == 1:
                yield "Step one done."
            else:
                yield "All done.\n---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()

        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.auto_turn_count = 1
        await runner._auto_continue(aq.session_id)

        assert captured_message[0] == "Continue."
        assert aq.state is AutonomousState.completed
        assert aq.auto_turn_count == 3

    @pytest.mark.asyncio
    async def test_max_turns_closes_session(self) -> None:
        """When max_auto_turns is reached the session closes."""
        store = ConversationStore()
        settings = _make_settings()
        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=_make_run_serializer(),
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.auto_turn_count = 20

        await runner._auto_continue(aq.session_id)

        assert aq.state is AutonomousState.completed

    @pytest.mark.asyncio
    async def test_auto_continue_stops_on_completed(self) -> None:
        """_auto_continue exits immediately for a completed session."""
        store = ConversationStore()
        agent = MagicMock()
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=_make_run_serializer(),
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.completed

        await runner._auto_continue(aq.session_id)

        assert agent.stream.call_count == 0

    @pytest.mark.asyncio
    async def test_completion_suppressed_feedback_message(self) -> None:
        """When completion_suppressed is set, the next Continue includes a notice."""
        store = ConversationStore()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(message, *args, **kwargs):
            captured_message.append(str(message))
            yield "---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.auto_turn_count = 1
        aq.completion_suppressed = True

        await runner._auto_continue(aq.session_id)

        assert len(captured_message) >= 1
        assert "previous completion marker was ignored" in captured_message[0]
        assert "pending subsessions (task / user_chat)" in captured_message[0]
        assert "list_subsessions" in captured_message[0]
        assert aq.completion_suppressed is False

    @pytest.mark.asyncio
    async def test_idle_cap_closes_session(self) -> None:
        """N consecutive no-change turns close the session."""
        store = ConversationStore()
        settings = _make_settings(max_idle_auto_turns=2)
        run_serializer = _make_run_serializer()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _noop_stream(*args, **kwargs):
            yield "NO_CHANGE"
            return

        agent.stream.side_effect = _noop_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.auto_turn_count = 0
        aq.consecutive_no_change = 1

        await runner._auto_continue(aq.session_id)

        assert aq.state is AutonomousState.completed

    @pytest.mark.asyncio
    async def test_no_change_reply_not_published_to_event_sink(self) -> None:
        """A NO_CHANGE reply is recorded but NOT published to the event sink."""
        store = ConversationStore()
        settings = _make_settings(max_idle_auto_turns=5)
        run_serializer = _make_run_serializer()
        event_sink = MagicMock()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _noop_stream(*args, **kwargs):
            yield "NO_CHANGE"
            return

        agent.stream.side_effect = _noop_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
            event_sink=event_sink,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.auto_turn_count = 0
        aq.consecutive_no_change = 0

        await runner._auto_continue(aq.session_id)

        agent_msg_calls = [
            c
            for c in event_sink.publish.call_args_list
            if c[0][1].get("type") == SSE_AGENT_MESSAGE_TYPE
        ]
        assert agent_msg_calls == []
        assert aq.consecutive_no_change == 5
        assert aq.state is AutonomousState.completed


class TestAgentFactoryLoopSafety:
    """The agent factory is called via asyncio.to_thread to avoid loop blocking."""

    @pytest.mark.asyncio
    async def test_factory_runs_within_auto_continue(self) -> None:
        """The factory is invoked once for the autonomous turn."""
        store = ConversationStore()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _stream(*args, **kwargs):
            yield "---AUTONOMOUS COMPLETE---"

        agent.stream.return_value = _stream()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)

        await runner._auto_continue(aq.session_id)

        assert agent.stream.call_count == 1


class TestConversationStoreRegistration:
    """Autonomous sessions are registered in the conversation store."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    def test_create_session_registers_in_conversation_store(self) -> None:
        """A created session appears in the owner's store listing."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)

        sessions, _active = store.list_sessions("owner1")
        session_ids = [s["session_id"] for s in sessions]
        assert aq.session_id in session_ids
        assert runner.is_autonomous(aq.session_id)

    def test_create_session_persists_owner_link(self) -> None:
        """The owner→session link is persisted to disk."""
        with tempfile.TemporaryDirectory() as td:
            persist = Path(td) / "conversations.json"
            store = ConversationStore(persist_path=persist)
            runner = AutonomousRunner(
                settings=_make_settings(),
                conversation_store=store,
                agent_factory=MagicMock(),
                run_serializer=MagicMock(),
            )
            aq = runner.create_session("owner1", schedule_kickoff=False)

            raw = json.loads(persist.read_text())
            assert OPERATOR_OWNER in raw
            persisted_ids = {s["session_id"] for s in raw[OPERATOR_OWNER]["sessions"]}
            assert aq.session_id in persisted_ids

    @pytest.mark.asyncio
    async def test_resume_reconciles_orphaned_session(self) -> None:
        """Resume re-registers a session missing from the conversation store."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        runner._auto_continue = AsyncMock()

        runner._sessions["orphan-1"] = AutonomousSession(
            session_id="orphan-1",
            owner_id="owner-x",
            state=AutonomousState.executing,
        )
        assert store.get_session("orphan-1") is None

        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)
        await asyncio.sleep(0)

        sessions, _active = store.list_sessions("owner-x")
        session_ids = [s["session_id"] for s in sessions]
        assert "orphan-1" in session_ids


class TestAutonomousEventStreaming:
    """Live SSE token publishing and transcript recording during autonomous turns."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_auto_continue_publishes_tokens_to_event_sink(self) -> None:
        """Streamed tokens and the final reply are published to the sink."""
        store = ConversationStore()
        event_sink = MagicMock()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _token_stream(*args, **kwargs):
            yield "Hello"
            yield " "
            yield "world!\n---AUTONOMOUS COMPLETE---"

        agent.stream.return_value = _token_stream()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
            event_sink=event_sink,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)

        await runner._auto_continue(aq.session_id)

        token_calls = [
            c
            for c in event_sink.publish.call_args_list
            if c[0][1].get("type") == SSE_AUTONOMOUS_TOKEN_TYPE
        ]
        assert len(token_calls) == 3
        assert token_calls[0][0][1]["token"] == "Hello"
        assert token_calls[1][0][1]["token"] == " "
        assert token_calls[2][0][1]["token"] == "world!\n---AUTONOMOUS COMPLETE---"

        agent_msg_calls = [
            c
            for c in event_sink.publish.call_args_list
            if c[0][1].get("type") == SSE_AGENT_MESSAGE_TYPE
        ]
        assert len(agent_msg_calls) == 1
        assert (
            agent_msg_calls[0][0][1]["text"]
            == "Hello world!\n---AUTONOMOUS COMPLETE---"
        )

    @pytest.mark.asyncio
    async def test_auto_continue_records_to_store(self) -> None:
        """The autonomous turn is recorded in the conversation history."""
        store = ConversationStore()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _token_stream(*args, **kwargs):
            yield "Plan text"
            yield "\n---AUTONOMOUS COMPLETE---"

        agent.stream.return_value = _token_stream()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)

        await runner._auto_continue(aq.session_id)

        turns = store.history(aq.session_id)
        assert len(turns) >= 1
        user_msg, asst_msg = turns[0]
        assert "Begin a new autonomous session" in user_msg
        assert "Plan text" in asst_msg


class TestCreateSessionSingleSessionInvariant:
    """create_session refuses to create a second open session for the same owner."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    def test_create_session_returns_existing_when_open_exists(self) -> None:
        """An existing open session is returned unchanged."""
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=ConversationStore(),
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq1 = runner.create_session("owner1")
        assert aq1.state is AutonomousState.executing

        aq2 = runner.create_session("owner1")
        assert aq2.session_id == aq1.session_id
        assert aq2.state is AutonomousState.executing

    def test_create_session_allows_new_when_existing_is_completed(self) -> None:
        """A completed session does not block a fresh session."""
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=ConversationStore(),
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq1 = runner.create_session("owner1")
        aq1.state = AutonomousState.completed

        aq2 = runner.create_session("owner1")
        assert aq2.session_id != aq1.session_id
        assert aq2.state is AutonomousState.executing


class TestResumeSessionsNonBlocking:
    """resume_sessions schedules background work instead of blocking startup."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_resume_completed_leaves_as_is(self) -> None:
        """Completed sessions are left untouched on resume."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=_make_run_serializer(),
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.completed

        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)

        assert runner.get_session(aq.session_id).state is AutonomousState.completed

    @pytest.mark.asyncio
    async def test_resume_executing_schedules_auto_continue(self) -> None:
        """Executing sessions schedule auto-continue on resume."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=_make_run_serializer(),
        )
        runner.create_session("owner1", schedule_kickoff=False)
        runner._auto_continue = AsyncMock()

        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)
        await asyncio.sleep(0)

        assert runner._auto_continue.call_count >= 1

    @pytest.mark.asyncio
    async def test_resume_empty_store_bootstraps_session(self) -> None:
        """An empty store auto-starts one session for the preset."""
        store = ConversationStore()
        settings = _make_settings()
        settings.autonomous.sessions = [_make_definition("default")]
        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=_make_run_serializer(),
        )
        assert len(runner._sessions) == 0

        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)

        assert len(runner._sessions) == 1
        new_aq = next(iter(runner._sessions.values()))
        assert new_aq.owner_id == "autonomous"
        assert new_aq.state is AutonomousState.executing

    @pytest.mark.asyncio
    async def test_resume_multiple_periodic_presets_fire_at_startup(self) -> None:
        """Every enabled preset bootstraps immediately at startup."""
        store = ConversationStore()
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition(name)
            for name in ("cost-review", "mail-check", "release-review")
        ]
        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=_make_run_serializer(),
        )
        runner._auto_continue = AsyncMock()

        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)

        assert runner.definition_count == 3
        assert len(runner._sessions) == 3
        for name in ("cost-review", "mail-check", "release-review"):
            owner_id = runner.owner_id_for_definition(name)
            matching = [
                aq for aq in runner._sessions.values() if aq.owner_id == owner_id
            ]
            assert len(matching) == 1
            assert matching[0].definition_name == name
            assert matching[0].state is AutonomousState.executing


class TestRestartContextInjection:
    """Restart-context messages are injected when resuming after a restart."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_restart_injects_system_restarted(self) -> None:
        """is_restart prepends a SYSTEM RESTARTED notice."""
        store = ConversationStore()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        captured_prompt: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(prompt, *args, **kwargs):
            captured_prompt.append(str(prompt))
            yield "---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)

        await runner._auto_continue(aq.session_id, is_restart=True)

        assert len(captured_prompt) == 1
        assert "SYSTEM RESTARTED" in captured_prompt[0]
        assert "resuming an existing autonomous session" in captured_prompt[0]

    @pytest.mark.asyncio
    async def test_no_restart_has_no_system_restarted(self) -> None:
        """A fresh run has no SYSTEM RESTARTED notice."""
        store = ConversationStore()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        captured_prompt: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(prompt, *args, **kwargs):
            captured_prompt.append(str(prompt))
            yield "---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)

        await runner._auto_continue(aq.session_id, is_restart=False)

        assert len(captured_prompt) == 1
        assert "SYSTEM RESTARTED" not in captured_prompt[0]

    @pytest.mark.asyncio
    async def test_restart_board_unchanged_injects_no_change(self, monkeypatch) -> None:
        """An unchanged board on resume injects a NO_CHANGE instruction."""
        store = ConversationStore()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        monkeypatch.setattr(
            AutonomousRunner,
            "_mail_board_digest",
            AsyncMock(return_value="digest-123"),
        )

        captured_prompt: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(prompt, *args, **kwargs):
            captured_prompt.append(str(prompt))
            yield "---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.last_board_digest = "digest-123"

        await runner._auto_continue(aq.session_id, is_restart=True)

        assert len(captured_prompt) == 1
        assert "SYSTEM RESTARTED" in captured_prompt[0]
        assert "BOARD UNCHANGED" in captured_prompt[0]
        assert "NO_CHANGE" in captured_prompt[0]
        assert "Begin a new autonomous session" not in captured_prompt[0]
        assert aq.last_board_digest == "digest-123"

    @pytest.mark.asyncio
    async def test_restart_board_unchanged_drops_custom_prompt(
        self, monkeypatch
    ) -> None:
        """An unchanged board on resume drops the custom kickoff prompt."""
        store = ConversationStore()
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition("default", prompt="Run the nightly triage now."),
        ]
        run_serializer = _make_run_serializer()

        monkeypatch.setattr(
            AutonomousRunner,
            "_mail_board_digest",
            AsyncMock(return_value="digest-123"),
        )

        captured_prompt: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(prompt, *args, **kwargs):
            captured_prompt.append(str(prompt))
            yield "---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()
        owner_id = runner.owner_id_for_definition("default")
        aq = runner.create_session(owner_id, schedule_kickoff=False)
        aq.last_board_digest = "digest-123"

        await runner._auto_continue(aq.session_id, is_restart=True)

        assert len(captured_prompt) == 1
        assert "SYSTEM RESTARTED" in captured_prompt[0]
        assert "BOARD UNCHANGED" in captured_prompt[0]
        assert "NO_CHANGE" in captured_prompt[0]
        assert "Run the nightly triage now." not in captured_prompt[0]

    @pytest.mark.asyncio
    async def test_restart_board_changed_stores_new_digest(self, monkeypatch) -> None:
        """A changed board on resume skips NO_CHANGE and stores the new digest."""
        store = ConversationStore()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        monkeypatch.setattr(
            AutonomousRunner,
            "_mail_board_digest",
            AsyncMock(return_value="new-digest"),
        )

        captured_prompt: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(prompt, *args, **kwargs):
            captured_prompt.append(str(prompt))
            yield "---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.last_board_digest = "old-digest"

        await runner._auto_continue(aq.session_id, is_restart=True)

        assert len(captured_prompt) == 1
        assert "SYSTEM RESTARTED" in captured_prompt[0]
        assert "BOARD UNCHANGED" not in captured_prompt[0]
        assert "NO_CHANGE" not in captured_prompt[0]
        assert aq.last_board_digest == "new-digest"

    @pytest.mark.asyncio
    async def test_mail_board_digest_disabled_returns_none(self) -> None:
        """A disabled (or mock) mail integration produces no board digest."""
        runner = AutonomousRunner(
            settings=MagicMock(),
            conversation_store=ConversationStore(),
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        assert await runner._mail_board_digest() is None

    @pytest.mark.asyncio
    async def test_mail_board_digest_hashes_board_content(self, monkeypatch) -> None:
        """Enabled mail integration hashes the raw board-content response."""
        settings = MagicMock()
        settings.mail.enabled = True

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=ConversationStore(),
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )

        fake_client = MagicMock()
        fake_client.board_content = AsyncMock(return_value='{"columns": []}')
        monkeypatch.setattr(
            "robotsix_chat.mail.client.MailClient", lambda _settings: fake_client
        )

        digest = await runner._mail_board_digest()
        assert digest == hashlib.sha256(b'{"columns": []}').hexdigest()


class TestAutoContinueThrottleAndSubsessionGate:
    """Throttle interval, subsession gate blocking, and timeout fallback."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @staticmethod
    def _make_subsession_info(is_active: bool = True) -> MagicMock:
        info = MagicMock()
        info.is_active = is_active
        info.kind = "task"
        return info

    @staticmethod
    def _make_registry(*subsessions: MagicMock) -> MagicMock:
        reg = MagicMock()
        reg.list_for_owner.return_value = list(subsessions)
        return reg

    @pytest.mark.asyncio
    async def test_throttle_waits_interval(self) -> None:
        """_wait_before_continue sleeps at least the configured interval."""
        store = ConversationStore()
        settings = _make_settings(continue_interval_seconds=0.05)

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
            subsession_registry=self._make_registry(),
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)

        start = time.monotonic()
        await runner._wait_before_continue(aq.session_id)
        elapsed = time.monotonic() - start

        assert elapsed >= 0.04

    @pytest.mark.asyncio
    async def test_subsession_gate_blocks_while_active(self) -> None:
        """The gate keeps waiting while an active subsession exists."""
        store = ConversationStore()
        settings = _make_settings(continue_interval_seconds=0.01)

        active_info = self._make_subsession_info(is_active=True)
        registry = self._make_registry(active_info)

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
            subsession_registry=registry,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)

        async def _resolve_after_delay() -> None:
            await asyncio.sleep(0.1)
            active_info.is_active = False

        async def _run() -> float:
            start = time.monotonic()
            await runner._wait_before_continue(aq.session_id)
            return time.monotonic() - start

        elapsed = await asyncio.gather(_run(), _resolve_after_delay())
        elapsed = elapsed[0]

        assert elapsed >= 0.1
        assert registry.list_for_owner.call_count >= 1

    @pytest.mark.asyncio
    async def test_subsession_gate_timeout_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate eventually returns when a subsession stays active."""
        store = ConversationStore()
        timeout = 0.2
        monkeypatch.setattr(
            "robotsix_chat.autonomous.runner._PENDING_SUBSESSION_WAIT_TIMEOUT",
            timeout,
        )
        settings = _make_settings(continue_interval_seconds=0.01)

        registry = self._make_registry(self._make_subsession_info(is_active=True))

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
            subsession_registry=registry,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)

        start = time.monotonic()
        await runner._wait_before_continue(aq.session_id)
        elapsed = time.monotonic() - start

        assert elapsed >= 0.15

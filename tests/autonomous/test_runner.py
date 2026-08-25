"""Tests for the AutonomousRunner lifecycle and single-prompt logic."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest.mock
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


def _make_definition(
    name: str,
    prompt: str = "",
    *,
    trigger_type: str = "periodic",
    trigger_interval_seconds: float = 45.0,
    model_level: int | None = None,
    max_runs: int = 0,
) -> SimpleNamespace:
    """Return a mock autonomous session definition matching config shapes."""
    return SimpleNamespace(
        name=name,
        prompt=prompt,
        trigger_type=SimpleNamespace(value=trigger_type),
        trigger_interval_seconds=trigger_interval_seconds,
        max_auto_turns=20,
        model_level=model_level,
        max_runs=max_runs,
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
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
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
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
        )

    def _runner(self) -> AutonomousRunner:
        store = ConversationStore()
        return AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
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

    def test_completion_marker_closes_without_subsession_gate(self) -> None:
        """The completion marker closes the session without a subsession gate."""
        runner = self._runner()
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
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
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
            agent_factory=lambda _ml=None: agent,
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
            agent_factory=lambda _ml=None: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()

        owner_id = runner.owner_id_for_definition("default")
        aq = runner.create_session(owner_id, schedule_kickoff=False)
        await runner._auto_continue(aq.session_id)

        assert captured_message[0] == "Run the nightly triage now."
        assert aq.state is AutonomousState.completed

    @pytest.mark.asyncio
    async def test_single_prompt_does_not_continue(self) -> None:
        """The runner sends exactly one prompt and never a Continue nudge."""
        store = ConversationStore()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _stream(message, *args, **kwargs):
            captured_message.append(str(message))
            yield "Step one done."  # no completion marker

        agent.stream.side_effect = _stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda _ml=None: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()

        aq = runner.create_session("owner1", schedule_kickoff=False)
        await runner._auto_continue(aq.session_id)

        assert captured_message == [
            "Begin a new autonomous session and work it to completion."
        ]
        assert agent.stream.call_count == 1
        assert aq.auto_turn_count == 1
        assert aq.state is AutonomousState.executing

    @pytest.mark.asyncio
    async def test_single_prompt_closes_on_completion_marker(self) -> None:
        """A single prompt that emits the completion marker closes the session."""
        store = ConversationStore()
        settings = _make_settings()
        run_serializer = _make_run_serializer()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _stream(*args, **kwargs):
            yield "All done.\n---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda _ml=None: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()

        aq = runner.create_session("owner1", schedule_kickoff=False)
        await runner._auto_continue(aq.session_id)

        assert agent.stream.call_count == 1
        assert aq.state is AutonomousState.completed
        assert aq.auto_turn_count == 1

    @pytest.mark.asyncio
    async def test_auto_continue_stops_on_completed(self) -> None:
        """_auto_continue exits immediately for a completed session."""
        store = ConversationStore()
        agent = MagicMock()
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=lambda _ml=None: agent,
            run_serializer=_make_run_serializer(),
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.completed

        await runner._auto_continue(aq.session_id)

        assert agent.stream.call_count == 0

    @pytest.mark.asyncio
    async def test_no_change_reply_not_published_to_event_sink(self) -> None:
        """A NO_CHANGE reply is recorded but NOT published to the event sink."""
        store = ConversationStore()
        settings = _make_settings()
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
            agent_factory=lambda _ml=None: agent,
            run_serializer=run_serializer,
            event_sink=event_sink,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)

        await runner._auto_continue(aq.session_id)

        agent_msg_calls = [
            c
            for c in event_sink.publish.call_args_list
            if c[0][1].get("type") == SSE_AGENT_MESSAGE_TYPE
        ]
        assert agent_msg_calls == []
        assert aq.auto_turn_count == 1
        # No completion marker and no continue loop — the session stays open.
        assert aq.state is AutonomousState.executing


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
            agent_factory=lambda _ml=None: agent,
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
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
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
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
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
            agent_factory=lambda _ml=None: agent,
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
            agent_factory=lambda _ml=None: agent,
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
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
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
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
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
    async def test_resume_executing_does_not_reprompt(self) -> None:
        """Executing sessions are left as-is — no synthetic re-prompt."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=_make_settings(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=_make_run_serializer(),
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        runner._auto_continue = AsyncMock()

        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)
        await asyncio.sleep(0)

        assert runner._auto_continue.call_count == 0
        assert runner.get_session(aq.session_id).state is AutonomousState.executing

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


class TestPersistedSchedulerState:
    """Per-preset scheduler state survives restarts and gates re-triggering."""

    @pytest.fixture
    def persist_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point the runner's persist paths at a per-test temp directory."""
        monkeypatch.setattr(
            "robotsix_chat.autonomous.runner.AUTONOMOUS_PERSIST_PATH",
            str(tmp_path / "autonomous_sessions.json"),
        )
        monkeypatch.setattr(
            "robotsix_chat.autonomous.runner.AUTONOMOUS_SCHEDULER_PERSIST_PATH",
            str(tmp_path / "autonomous_scheduler_state.json"),
        )
        return tmp_path

    def _build(self, settings: MagicMock) -> AutonomousRunner:
        """Build a runner with auto-continue mocked to avoid live agent calls."""
        runner = AutonomousRunner(
            settings=settings,
            conversation_store=ConversationStore(),
            agent_factory=MagicMock(),
            run_serializer=_make_run_serializer(),
        )
        runner._auto_continue = AsyncMock()  # type: ignore[method-assign]
        return runner

    @pytest.mark.asyncio
    async def test_restart_within_interval_does_not_retrigger_weekly_preset(
        self, persist_paths: Path
    ) -> None:
        """A weekly preset is not re-picked across restarts within its interval."""
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition("release-review", trigger_interval_seconds=604800.0)
        ]
        runner = self._build(settings)
        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)
        active_id = runner.active_session_id_for_definition("release-review")
        assert active_id is not None
        aq = runner._sessions[active_id]
        runner.check_reply_for_markers(aq.session_id, "---AUTONOMOUS COMPLETE---")
        assert aq.state is AutonomousState.completed

        # Two restarts: the completed session persists, but neither restart
        # spawns a *new* executing session because the interval hasn't elapsed.
        for _ in range(2):
            restarted = self._build(settings)
            await asyncio.wait_for(restarted.resume_sessions(), timeout=0.5)
            assert restarted.active_session_id_for_definition("release-review") is None

    @pytest.mark.asyncio
    async def test_interval_elapsed_across_restart_fires_once(
        self, persist_paths: Path
    ) -> None:
        """A preset due because its interval elapsed while down fires exactly once."""
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition("release-review", trigger_interval_seconds=45.0)
        ]
        (persist_paths / "autonomous_scheduler_state.json").write_text(
            json.dumps(
                {
                    "release-review": {
                        "last_run_at": time.time() - 90.0,
                        "last_completed_at": time.time() - 60.0,
                        "last_outcome": "completed",
                        "last_session_id": "previous-run",
                    }
                }
            )
        )
        runner = self._build(settings)
        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)

        assert len(runner._sessions) == 1
        aq = next(iter(runner._sessions.values()))
        assert aq.definition_name == "release-review"
        assert aq.state is AutonomousState.executing
        assert aq.session_id != "previous-run"

        # A second ensure call in the same process does not add another run.
        runner.ensure_all_active_sessions()
        assert len(runner._sessions) == 1

    @pytest.mark.asyncio
    async def test_delayed_fire_when_interval_elapses_while_running(
        self, persist_paths: Path
    ) -> None:
        """A not-yet-due preset fires exactly once when its interval elapses."""
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition("release-review", trigger_interval_seconds=0.5)
        ]
        first = self._build(settings)
        await asyncio.wait_for(first.resume_sessions(), timeout=2.0)
        active_id = first.active_session_id_for_definition("release-review")
        assert active_id is not None
        aq = first._sessions[active_id]
        first.check_reply_for_markers(aq.session_id, "---AUTONOMOUS COMPLETE---")
        assert aq.state is AutonomousState.completed

        restarted = self._build(settings)
        await asyncio.wait_for(restarted.resume_sessions(), timeout=2.0)
        # The completed session from the previous run is loaded from disk,
        # but no *new* executing session has been spawned — the preset is not
        # yet due.
        assert restarted.active_session_id_for_definition("release-review") is None

        await asyncio.sleep(1.0)
        active_id = restarted.active_session_id_for_definition("release-review")
        assert active_id is not None
        fired = restarted._sessions[active_id]
        assert fired.state is AutonomousState.executing
        assert fired.definition_name == "release-review"


class TestNoContinuationInjection:
    """The single-prompt run never injects Continue or restart notices."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_single_prompt_has_no_synthetic_nudges(self) -> None:
        """No 'Continue.' or 'SYSTEM RESTARTED' text reaches the agent."""
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
            agent_factory=lambda _ml=None: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()
        aq = runner.create_session("owner1", schedule_kickoff=False)

        await runner._auto_continue(aq.session_id)

        assert len(captured_prompt) == 1
        assert "Continue." not in captured_prompt[0]
        assert "SYSTEM RESTARTED" not in captured_prompt[0]
        assert "Begin a new autonomous session" in captured_prompt[0]


class TestScheduleGovernsSpawning:
    """A preset that is not due must stay closed.

    ``ensure_active_session`` used to spawn unconditionally — the
    "auto-restart always" guarantee — which silently overrode
    ``trigger_interval_seconds``.  Closing a daily preset restarted it
    within seconds, and every server restart re-ran all of them.  Observed
    2026-08-17: three autonomous sessions live at once (default,
    cost-review, release-review) where one was expected.
    """

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )
        monkeypatch.setattr(AutonomousRunner, "_schedule_background", MagicMock())

    def _runner(self) -> AutonomousRunner:
        return AutonomousRunner(
            settings=_make_settings(sessions=[_make_definition("default")]),
            conversation_store=ConversationStore(),
            agent_factory=MagicMock(),
            run_serializer=_make_run_serializer(),
        )

    def test_never_run_preset_starts_immediately(self) -> None:
        """With no recorded schedule a preset bootstraps at once."""
        runner = self._runner()
        assert runner.ensure_active_session("autonomous") is not None

    def test_not_due_preset_does_not_spawn(self) -> None:
        """A preset whose interval has not elapsed stays closed."""
        runner = self._runner()
        aq = runner.ensure_active_session("autonomous")
        assert aq is not None
        # Complete it: that schedules the next run one interval out.
        runner._mark_completed(aq.session_id, aq)
        runner.forget_session(aq.session_id)

        # This is the path the close/delete endpoints take.
        assert runner.ensure_active_session("autonomous") is None

    def test_force_bypasses_the_schedule(self) -> None:
        """The restart timer and an explicit 'run now' still fire."""
        runner = self._runner()
        aq = runner.ensure_active_session("autonomous")
        assert aq is not None
        runner._mark_completed(aq.session_id, aq)
        runner.forget_session(aq.session_id)

        assert runner.ensure_active_session("autonomous", force=True) is not None

    def test_due_preset_spawns_again(self) -> None:
        """Once the interval has elapsed the preset runs again."""
        runner = self._runner()
        aq = runner.ensure_active_session("autonomous")
        assert aq is not None
        runner._mark_completed(aq.session_id, aq)
        runner.forget_session(aq.session_id)

        runner._next_fire["autonomous"] = time.time() - 1.0
        assert runner.ensure_active_session("autonomous") is not None

    def test_existing_open_session_is_returned_not_duplicated(self) -> None:
        """An already-open session short-circuits the due check."""
        runner = self._runner()
        first = runner.ensure_active_session("autonomous")
        second = runner.ensure_active_session("autonomous")
        assert first is not None
        assert second is first


class TestSchedulePersistence:
    """The next-fire schedule must survive a restart.

    Without it the runner had no memory of when a preset last ran, so every
    boot re-fired all of them — a daily job could run several times in
    minutes across a few restarts.
    """

    def test_next_fire_round_trips_through_disk(self) -> None:
        """A persisted schedule is reloaded and still suppresses spawning."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autonomous_sessions.json"

            def _build() -> AutonomousRunner:
                runner = AutonomousRunner(
                    settings=_make_settings(sessions=[_make_definition("default")]),
                    conversation_store=ConversationStore(),
                    agent_factory=MagicMock(),
                    run_serializer=_make_run_serializer(),
                )
                runner._persist_path = path
                return runner

            first = _build()
            first._next_fire["autonomous"] = time.time() + 10_000
            first._save_sessions()

            second = _build()
            second._next_fire.clear()
            second._sessions = second._load_sessions()
            assert second._next_fire.get("autonomous") is not None
            assert second.ensure_active_session("autonomous") is None

    def test_legacy_flat_store_still_loads(self) -> None:
        """A pre-schedule store (bare session map) is read without loss."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autonomous_sessions.json"
            path.write_text(
                json.dumps(
                    {
                        "sess-1": {
                            "session_id": "sess-1",
                            "owner_id": "autonomous",
                            "state": "executing",
                            "auto_turn_count": 2,
                            "definition_name": "default",
                        }
                    }
                )
            )
            runner = AutonomousRunner(
                settings=_make_settings(sessions=[_make_definition("default")]),
                conversation_store=ConversationStore(),
                agent_factory=MagicMock(),
                run_serializer=_make_run_serializer(),
            )
            runner._persist_path = path
            loaded = runner._load_sessions()
            assert "sess-1" in loaded
            assert loaded["sess-1"].auto_turn_count == 2


class TestLegacyNextFireDoesNotStarvePresets:
    """A legacy ``next_fire`` map must not veto the schedule that owns firing.

    Two stores record the schedule: ``autonomous_scheduler_state.json``
    (per-preset last-run state, consulted by ``ensure_all_active_sessions``)
    and the older ``next_fire`` map inside ``autonomous_sessions.json``
    (consulted by ``_is_due``).  An install upgraded across the introduction
    of the former has a populated ``next_fire`` and no scheduler state, and
    the two disagreed: startup computed "due, fire now" from the missing
    state while ``_is_due`` refused from the stale map, so nothing fired and
    nothing was rescheduled.  Observed in production 2026-08-20, where
    cost-review had gone dormant with ``next_fire`` a day out.
    """

    @pytest.fixture
    def persist_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point the runner's persist paths at a per-test temp directory."""
        monkeypatch.setattr(
            "robotsix_chat.autonomous.runner.AUTONOMOUS_PERSIST_PATH",
            str(tmp_path / "autonomous_sessions.json"),
        )
        monkeypatch.setattr(
            "robotsix_chat.autonomous.runner.AUTONOMOUS_SCHEDULER_PERSIST_PATH",
            str(tmp_path / "autonomous_scheduler_state.json"),
        )
        return tmp_path

    def _build(self, settings: MagicMock) -> AutonomousRunner:
        """Build a runner with auto-continue mocked to avoid live agent calls."""
        runner = AutonomousRunner(
            settings=settings,
            conversation_store=ConversationStore(),
            agent_factory=MagicMock(),
            run_serializer=_make_run_serializer(),
        )
        runner._auto_continue = AsyncMock()  # type: ignore[method-assign]
        return runner

    def _write_legacy_next_fire(self, persist_paths: Path, owner: str, ts: float):
        """Persist a sessions file carrying only a legacy ``next_fire`` entry."""
        (persist_paths / "autonomous_sessions.json").write_text(
            json.dumps({"sessions": {}, "next_fire": {owner: ts}})
        )

    @pytest.mark.asyncio
    async def test_pending_next_fire_is_rearmed_after_restart(
        self, persist_paths: Path
    ) -> None:
        """A preset whose fire is still pending re-arms and fires when due.

        The starvation case: the in-process timer that owned the next fire
        died with the previous process, and the only surviving record is the
        legacy ``next_fire`` map.  Startup must recover a real schedule from
        it rather than reading the preset as never-run and then being vetoed
        by that same map.
        """
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition("cost-review", trigger_interval_seconds=1.0)
        ]
        # Mid-interval: fire is still ~0.5s away, and no scheduler state
        # exists — exactly the upgraded-install shape.
        self._write_legacy_next_fire(
            persist_paths, "autonomous:cost-review", time.time() + 0.5
        )
        runner = self._build(settings)
        await asyncio.wait_for(runner.resume_sessions(), timeout=2.0)

        # Not due yet, so nothing has fired — but a re-arm is pending.
        assert runner.active_session_id_for_definition("cost-review") is None
        assert runner._auto_tasks

        await asyncio.sleep(1.5)
        assert runner.active_session_id_for_definition("cost-review") is not None

    @pytest.mark.asyncio
    async def test_seeding_keeps_a_recently_run_preset_dormant(
        self, persist_paths: Path
    ) -> None:
        """Seeding preserves the schedule: a recent run stays dormant.

        The complement of the test above — forcing the fire must not turn
        into firing everything on every boot, which is the over-firing this
        schedule was introduced to stop.
        """
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition("cost-review", trigger_interval_seconds=86400.0)
        ]
        # next_fire = last_completed_at + interval, so a next_fire nearly a
        # full interval out implies the run completed moments ago.
        self._write_legacy_next_fire(
            persist_paths, "autonomous:cost-review", time.time() + 86000.0
        )
        runner = self._build(settings)
        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)

        assert runner.active_session_id_for_definition("cost-review") is None
        # The implied completion time was recovered into the new store, so the
        # next boot does not read this preset as never-run.
        assert runner._scheduler_state["cost-review"]["last_completed_at"] > 0


class TestOperatorTurnHoldsSessionOpen:
    """A session the operator is chatting with must not be replaced.

    An autonomous run finishes in a couple of minutes but its card stays on
    screen for the rest of ``trigger_interval_seconds``, so the session the
    operator opens and talks to is almost always one already marked
    ``completed``.  Nothing used to move it back out of that state — the
    marker scan only ever closes — so the next restart fire retired the live
    conversation (deleting its history) and dropped a fresh session in its
    place.  Observed 2026-08-23: the operator was mid-conversation when the
    card was swapped underneath them.
    """

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
        )
        monkeypatch.setattr(AutonomousRunner, "_schedule_background", MagicMock())

    def _runner(self, store: ConversationStore | None = None) -> AutonomousRunner:
        return AutonomousRunner(
            settings=_make_settings(sessions=[_make_definition("default")]),
            conversation_store=store or ConversationStore(),
            agent_factory=MagicMock(),
            run_serializer=_make_run_serializer(),
        )

    def test_operator_turn_reopens_a_completed_session(self) -> None:
        """Talking to a finished session makes it live again."""
        runner = self._runner()
        aq = runner.ensure_active_session("autonomous")
        assert aq is not None
        runner._mark_completed(aq.session_id, aq)
        assert aq.state is AutonomousState.completed

        assert runner.note_operator_turn(aq.session_id) is True
        assert aq.state is AutonomousState.executing
        assert aq.reopened_by_operator is True

    def test_reopened_session_survives_the_restart_fire(self) -> None:
        """The timer returns the operator's session instead of replacing it."""
        runner = self._runner()
        aq = runner.ensure_active_session("autonomous")
        assert aq is not None
        runner._mark_completed(aq.session_id, aq)
        runner.note_operator_turn(aq.session_id)

        # This is exactly what ``_auto_restart`` does when its timer fires.
        again = runner.ensure_active_session("autonomous", force=True)
        assert again is aq
        assert runner.is_autonomous(aq.session_id)

    def test_untracked_session_is_ignored(self) -> None:
        """Ordinary operator chats are not autonomous sessions."""
        runner = self._runner()
        assert runner.note_operator_turn("not-an-autonomous-session") is False

    def test_recent_operator_turn_spares_a_completed_session(self) -> None:
        """The retire sweep leaves a freshly-chatted session alone.

        Covers the race where the agent re-emits the completion marker while
        the operator is still mid-conversation.
        """
        store = ConversationStore()
        runner = self._runner(store)
        aq = runner.ensure_active_session("autonomous")
        assert aq is not None
        session_id = aq.session_id
        runner._mark_completed(session_id, aq)
        # Operator spoke, then the agent closed the session again.
        aq.last_operator_turn_at = time.time()
        aq.state = AutonomousState.completed

        spawned = runner.ensure_active_session("autonomous", force=True)
        assert spawned is not None
        assert spawned.session_id != session_id
        # Spared, not retired: the conversation is still there.
        assert runner.is_autonomous(session_id)
        assert store.last_active(session_id) is not None

    def test_abandoned_reopened_session_releases_the_preset(self) -> None:
        """A conversation nobody returns to must not stall the preset forever.

        Holding the session open is right while someone is talking to it, but
        an operator who walks away would otherwise block every future run.
        """
        runner = self._runner()
        aq = runner.ensure_active_session("autonomous")
        assert aq is not None
        held = aq.session_id
        runner._mark_completed(held, aq)
        runner.note_operator_turn(held)
        # Operator has been silent well past the grace window.
        aq.last_operator_turn_at = time.time() - 10_000.0

        spawned = runner.ensure_active_session("autonomous", force=True)
        assert spawned is not None
        assert spawned.session_id != held
        assert not runner.is_autonomous(held)


class TestRestartTimerIsNotDuplicated:
    """Each owner gets exactly one pending restart timer.

    Restarts are armed from two independent places — the completion path and
    the sessions close/delete routes — and each arming moves the owner's
    single ``_next_fire`` entry forward.  The superseded task still slept out
    its own delay and fired anyway, so the owner accumulated one extra live
    timer per stacked arming.  Observed 2026-08-23: the hourly ``default``
    preset was spawning a replacement session every ~30 minutes.
    """

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
        )

    def _runner(self) -> AutonomousRunner:
        return AutonomousRunner(
            settings=_make_settings(
                sessions=[_make_definition("default", trigger_interval_seconds=600.0)]
            ),
            conversation_store=ConversationStore(),
            agent_factory=MagicMock(),
            run_serializer=_make_run_serializer(),
        )

    @pytest.mark.asyncio
    async def test_second_arming_cancels_the_first(self) -> None:
        """Arming twice leaves one live timer, not two."""
        runner = self._runner()

        runner._arm_restart_timer("autonomous")
        first = runner._restart_timers["autonomous"]

        runner._arm_restart_timer("autonomous")
        second = runner._restart_timers["autonomous"]

        assert second is not first
        await asyncio.sleep(0)
        assert first.cancelled() or first.done()
        assert not second.done()

        second.cancel()

    @pytest.mark.asyncio
    async def test_completion_then_close_leaves_one_timer(self) -> None:
        """The close/delete route's restart replaces the completion's timer."""
        runner = self._runner()
        aq = runner.ensure_active_session("autonomous", schedule_kickoff=False)
        assert aq is not None

        runner._mark_completed(aq.session_id, aq)
        completion_timer = runner._restart_timers["autonomous"]

        # The operator discards the card; the route arms its own restart.
        runner.forget_session(aq.session_id)
        runner.schedule_restart("autonomous")

        await asyncio.sleep(0)
        assert completion_timer.cancelled() or completion_timer.done()
        assert len(runner._restart_timers) == 1

        runner._restart_timers["autonomous"].cancel()


class TestModelLevel:
    """Per-preset model_level is forwarded to the agent factory."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_preset_model_level_forwarded_to_factory(self) -> None:
        """The preset's model_level is passed to the agent factory."""
        store = ConversationStore()
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition("default", model_level=2),
        ]
        run_serializer = _make_run_serializer()

        captured_levels: list[int | None] = []
        agent = MagicMock()

        def _factory(model_level=None):
            captured_levels.append(model_level)
            return agent

        agent.stream = MagicMock()

        async def _stream(*args, **kwargs):
            yield "Done.\n---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=_factory,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()

        owner_id = runner.owner_id_for_definition("default")
        aq = runner.create_session(owner_id, schedule_kickoff=False)
        await runner._auto_continue(aq.session_id)

        assert captured_levels == [2]

    @pytest.mark.asyncio
    async def test_null_model_level_uses_none(self) -> None:
        """A preset with model_level=None passes None to the factory."""
        store = ConversationStore()
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition("default"),  # model_level defaults to None
        ]
        run_serializer = _make_run_serializer()

        captured_levels: list[int | None] = []
        agent = MagicMock()

        def _factory(model_level=None):
            captured_levels.append(model_level)
            return agent

        agent.stream = MagicMock()

        async def _stream(*args, **kwargs):
            yield "Done.\n---AUTONOMOUS COMPLETE---"

        agent.stream.side_effect = _stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=_factory,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()

        owner_id = runner.owner_id_for_definition("default")
        aq = runner.create_session(owner_id, schedule_kickoff=False)
        await runner._auto_continue(aq.session_id)

        assert captured_levels == [None]


class TestMaxRuns:
    """Per-preset max_runs enforcement."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )
        monkeypatch.setattr(AutonomousRunner, "_save_scheduler_state", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_scheduler_state", MagicMock(return_value={})
        )

    def _make_runner_with_max_runs(
        self, max_runs: int
    ) -> tuple[AutonomousRunner, MagicMock]:
        """Return a runner with a preset that has *max_runs*."""
        store = ConversationStore()
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition("default", max_runs=max_runs),
        ]
        run_serializer = _make_run_serializer()
        agent = MagicMock()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda _ml=None: agent,
            run_serializer=run_serializer,
        )
        runner._auto_restart = AsyncMock()
        return runner, agent

    def test_unlimited_preset_stays_active(self) -> None:
        """max_runs=0 (unlimited) never disables the preset."""
        runner, _ = self._make_runner_with_max_runs(0)
        assert "default" in runner._definitions

        # Simulate many completions.
        for _ in range(100):
            owner_id = runner.owner_id_for_definition("default")
            aq = runner.create_session(owner_id, schedule_kickoff=False)
            runner._mark_completed(aq.session_id, aq)

        assert "default" in runner._definitions

    def test_max_runs_disables_preset_after_limit(self) -> None:
        """A preset with max_runs=2 is disabled after 2 completed runs."""
        runner, _ = self._make_runner_with_max_runs(2)
        owner_id = runner.owner_id_for_definition("default")

        # First run.
        aq1 = runner.create_session(owner_id, schedule_kickoff=False)
        runner._mark_completed(aq1.session_id, aq1)
        assert "default" in runner._definitions

        # Second run — should disable the preset.
        aq2 = runner.create_session(owner_id, schedule_kickoff=False)
        runner._mark_completed(aq2.session_id, aq2)
        assert "default" not in runner._definitions

    def test_max_runs_disabled_preset_prevents_new_sessions(self) -> None:
        """After max_runs is reached, ensure_active_session returns None."""
        runner, _ = self._make_runner_with_max_runs(1)
        owner_id = runner.owner_id_for_definition("default")

        aq = runner.create_session(owner_id, schedule_kickoff=False)
        runner._mark_completed(aq.session_id, aq)
        assert "default" not in runner._definitions

        # ensure_active_session should not create a new session.
        result = runner.ensure_active_session(
            owner_id, definition_name="default", schedule_kickoff=False
        )
        assert result is None

    def test_total_runs_persisted_in_scheduler_state(self) -> None:
        """The total_runs counter is recorded in the scheduler state."""
        runner, _ = self._make_runner_with_max_runs(5)
        owner_id = runner.owner_id_for_definition("default")

        aq = runner.create_session(owner_id, schedule_kickoff=False)
        runner._mark_completed(aq.session_id, aq)

        assert runner._total_runs_for("default") == 1

    def test_max_runs_enforcement_survives_restart(self) -> None:
        """After max_runs is exhausted, a new runner (restart) skips the preset.

        Regression: ``_resolve_definitions`` previously only filtered on
        ``d.enabled``.  A preset that exhausted its budget was popped from
        ``_definitions`` in-memory, but the next server restart re-loaded it
        from config (still ``enabled=True``) and created one extra session
        before ``_mark_completed`` re-disabled it.
        """
        store = ConversationStore()
        settings = _make_settings()
        settings.autonomous.sessions = [
            _make_definition("default", max_runs=1),
        ]
        run_serializer = _make_run_serializer()
        agent = MagicMock()

        # Simulate persisted scheduler state with total_runs already at the
        # limit — as if a previous runner completed the run and persisted
        # the state before shutting down.
        persisted_scheduler_state = {
            "default": {"total_runs": 1, "last_outcome": "completed"},
        }

        # First runner: create and exhaust the budget.
        runner1 = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda _ml=None: agent,
            run_serializer=run_serializer,
        )
        # Inject the persisted state as if loaded from disk.
        runner1._scheduler_state = dict(persisted_scheduler_state)
        assert runner1._total_runs_for("default") == 1

        # "Restart": create a fresh runner with the same settings and the
        # persisted scheduler state.  The mock _load_scheduler_state returns
        # the persisted state.
        with unittest.mock.patch.object(
            AutonomousRunner,
            "_load_scheduler_state",
            return_value=dict(persisted_scheduler_state),
        ):
            runner2 = AutonomousRunner(
                settings=settings,
                conversation_store=store,
                agent_factory=lambda _ml=None: agent,
                run_serializer=run_serializer,
            )

        # The preset must NOT be in the active definitions after restart.
        assert "default" not in runner2._definitions

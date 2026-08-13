"""Tests for the AutonomousRunner state machine and auto-continue logic."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_chat.autonomous.models import AutonomousState
from robotsix_chat.autonomous.runner import AutonomousRunner
from robotsix_chat.chat.conversation import (
    OPERATOR_OWNER,
    ConversationStore,
    Session,
)
from robotsix_chat.chat.events import SSE_AGENT_MESSAGE_TYPE, SSE_AUTONOMOUS_TOKEN_TYPE


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
        settings = MagicMock()
        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq = runner.create_session("owner1")
        assert aq.owner_id == "owner1"
        assert aq.state is AutonomousState.planning
        assert runner.is_autonomous(aq.session_id)
        assert runner.get_state(aq.session_id) is AutonomousState.planning

    def test_create_session_with_id(self) -> None:
        """A custom session_id is honoured."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=MagicMock(),
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
            settings=MagicMock(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        assert not runner.is_autonomous("nonexistent")
        assert runner.get_state("nonexistent") is None
        assert runner.get_session("nonexistent") is None
        assert runner.owner_for_session("nonexistent") is None


class TestMarkerDetection:
    """Marker detection and state transition tests."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.fixture
    def runner(self) -> AutonomousRunner:
        """Runner with default markers configured."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.proposal_marker = "---PROPOSAL READY---"
        settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        return AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )

    def test_proposal_marker_transitions_to_awaiting(self, runner) -> None:
        """Approval marker moves state to proposal and stores plan."""
        aq = runner.create_session("owner1")
        reply = "Here is my plan:\n1. Do X\n2. Do Y\n\n---PROPOSAL READY---"
        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        assert new_state is AutonomousState.proposal
        assert aq.state is AutonomousState.proposal
        assert "Here is my plan:" in aq.plan_text
        assert "---PROPOSAL READY---" not in aq.plan_text

    def test_completion_marker_transitions_to_completed(self, runner) -> None:
        """Completion marker moves state to completed."""
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.executing
        reply = "All done!\n\n---AUTONOMOUS COMPLETE---"
        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        assert new_state is AutonomousState.completed
        assert aq.state is AutonomousState.completed

    def test_no_marker_no_transition(self, runner) -> None:
        """Reply without markers leaves state unchanged."""
        aq = runner.create_session("owner1")
        reply = "Working on it..."
        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        assert new_state is None
        assert aq.state is AutonomousState.planning

    def test_unknown_session_returns_none(self, runner) -> None:
        """Marker scan on unknown session returns None."""
        result = runner.check_reply_for_markers("unknown", "---PROPOSAL READY---")
        assert result is None

    def test_completion_takes_priority_over_approval(self, runner) -> None:
        """When both markers appear, completion wins."""
        aq = runner.create_session("owner1")
        reply = "Plan:\n---PROPOSAL READY---\nDone:\n---AUTONOMOUS COMPLETE---"
        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        assert new_state is AutonomousState.completed

    def test_completion_suppressed_when_active_subsessions(self) -> None:
        """Completion marker is ignored when non-periodic subsessions are running."""
        from types import SimpleNamespace

        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.approval_marker = "---AWAITING APPROVAL---"
        settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.auto_approve = False

        reg = MagicMock()
        reg.list_for_owner.return_value = [
            SimpleNamespace(is_active=True, kind="task"),
        ]

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
            subsession_registry=reg,
        )
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.executing
        reply = "All done!\n\n---AUTONOMOUS COMPLETE---"

        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        # Completion must be suppressed — no transition.
        assert new_state is None
        assert aq.state is AutonomousState.executing
        assert aq.completion_suppressed is True

    def test_completion_not_suppressed_with_only_periodic_subsessions(self) -> None:
        """Completion is NOT suppressed when only periodic monitors are active.

        Periodic monitors run indefinitely by design; they must not deadlock
        session completion.  Only task / user_chat subsessions block completion.
        """
        from types import SimpleNamespace

        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.proposal_marker = "---PROPOSAL READY---"
        settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0

        reg = MagicMock()
        reg.list_for_owner.return_value = [
            SimpleNamespace(is_active=True, kind="periodic"),
        ]

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
            subsession_registry=reg,
        )
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.executing
        reply = "All done!\n\n---AUTONOMOUS COMPLETE---"

        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        # Completion must succeed — periodic monitors are not pending.
        assert new_state is AutonomousState.completed
        assert aq.state is AutonomousState.completed

    def test_completion_suppressed_with_pending_task_subsession(self) -> None:
        """Completion is suppressed when a non-periodic (task) subsession is active."""
        from types import SimpleNamespace

        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.proposal_marker = "---PROPOSAL READY---"
        settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0

        reg = MagicMock()
        reg.list_for_owner.return_value = [
            SimpleNamespace(is_active=True, kind="task"),
        ]

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
            subsession_registry=reg,
        )
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.executing
        reply = "All done!\n\n---AUTONOMOUS COMPLETE---"

        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        # Completion must be suppressed — task subsession is still pending.
        assert new_state is None
        assert aq.state is AutonomousState.executing
        assert aq.completion_suppressed is True


class TestAutoContinue:
    """Auto-continue loop tests."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_max_turns_enforcement(self) -> None:
        """When max_auto_turns is reached, revert to proposal."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.proposal_marker = "---PROPOSAL READY---"
        settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
        settings.autonomous.max_auto_turns = 2
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.executing
        aq.auto_turn_count = 2  # Already at max

        await runner._auto_continue(aq.session_id)

        assert aq.state is AutonomousState.proposal

    @pytest.mark.asyncio
    async def test_auto_continue_stops_on_non_executing(self) -> None:
        """_auto_continue exits immediately if not in executing state."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq = runner.create_session("owner1")
        await runner._auto_continue(aq.session_id)
        assert aq.state is AutonomousState.planning

    @pytest.mark.asyncio
    async def test_completion_suppressed_feedback_message(self) -> None:
        """When completion_suppressed is set, the next Continue includes a notice."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.proposal_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        settings.autonomous.auto_approve = False
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(message, *args, **kwargs):
            captured_message.append(str(message))
            yield "[APPROVAL]"  # triggers proposal marker so loop exits
            return

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.executing
        aq.plan_text = "plan"
        aq.auto_turn_count = 1  # non-zero to take the completion_suppressed branch
        aq.completion_suppressed = True
        runner._save_sessions = MagicMock()

        await runner._auto_continue(aq.session_id)

        assert len(captured_message) >= 1
        assert "previous completion marker was ignored" in captured_message[0]
        assert "pending subsessions (task / user_chat)" in captured_message[0]
        assert "list_subsessions" in captured_message[0]
        # The flag must be cleared after the message is delivered.
        assert aq.completion_suppressed is False

    @pytest.mark.asyncio
    async def test_completion_suppressed_and_restart_combined(self) -> None:
        """Deliver both restart notice and suppression feedback when both flags are set.

        When ``is_restart`` and ``completion_suppressed`` are both ``True``,
        the message must include ``SYSTEM RESTARTED`` *and* the suppressed-
        completion notice.  The ``completion_suppressed`` flag must be cleared
        after delivery.
        """
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.proposal_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        settings.autonomous.auto_approve = False
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(message, *args, **kwargs):
            captured_message.append(str(message))
            yield "[APPROVAL]"  # triggers proposal marker so loop exits
            return

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.executing
        aq.plan_text = "plan"
        aq.auto_turn_count = 2  # non-zero to take else branch
        aq.completion_suppressed = True
        runner._save_sessions = MagicMock()

        await runner._auto_continue(aq.session_id, is_restart=True)

        assert len(captured_message) >= 1
        assert "SYSTEM RESTARTED" in captured_message[0]
        assert "previous completion marker was ignored" in captured_message[0]
        assert "pending subsessions (task / user_chat)" in captured_message[0]
        assert "list_subsessions" in captured_message[0]
        # The flag must be cleared after the message is delivered.
        assert aq.completion_suppressed is False

    @pytest.mark.asyncio
    async def test_no_change_reply_not_published_to_event_sink(self) -> None:
        """A NO_CHANGE / idle reply is recorded but NOT published to the event sink."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.max_idle_auto_turns = 5
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.proposal_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        event_sink = MagicMock()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _noop_stream(*args, **kwargs):
            yield "NO_CHANGE\n[APPROVAL]"  # no-op sentinel + marker to exit loop
            return

        agent.stream.return_value = _noop_stream()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
            event_sink=event_sink,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.executing
        aq.plan_text = "plan"
        runner._save_sessions = MagicMock()

        await runner._auto_continue(aq.session_id)

        # Token frames are still published (streaming).
        token_calls = [
            c
            for c in event_sink.publish.call_args_list
            if c[0][1].get("type") == SSE_AUTONOMOUS_TOKEN_TYPE
        ]
        assert len(token_calls) >= 1

        # agent_message_frame must NOT be published for a no-op reply.
        agent_msg_calls = [
            c
            for c in event_sink.publish.call_args_list
            if c[0][1].get("type") == SSE_AGENT_MESSAGE_TYPE
        ]
        assert len(agent_msg_calls) == 0

        # The exchange is still recorded in history.
        turns = store.history(aq.session_id)
        assert len(turns) >= 1

        # consecutive_no_change must be incremented.
        assert aq.consecutive_no_change == 1

    @pytest.mark.asyncio
    async def test_idle_cap_halts_loop(self) -> None:
        """After max_idle_auto_turns consecutive NO_CHANGE replies, halt loop."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.max_idle_auto_turns = 2
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.proposal_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _noop_stream(*args, **kwargs):
            yield "NO_CHANGE"
            return

        agent.stream.return_value = _noop_stream()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.executing
        aq.plan_text = "plan"
        # Simulate already having one idle turn.
        aq.consecutive_no_change = 1
        runner._save_sessions = MagicMock()

        await runner._auto_continue(aq.session_id)

        # After one more NO_CHANGE (making 2 consecutive), must revert to proposal.
        assert aq.state is AutonomousState.proposal

    @pytest.mark.asyncio
    async def test_non_no_change_reply_resets_consecutive_counter(self) -> None:
        """A real (non-NO_CHANGE) reply resets the consecutive idle counter."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.max_idle_auto_turns = 5
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.proposal_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        event_sink = MagicMock()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _real_stream(*args, **kwargs):
            yield "Working on task — updated config file.\n[APPROVAL]"
            return

        agent.stream.return_value = _real_stream()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
            event_sink=event_sink,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.executing
        aq.plan_text = "plan"
        aq.consecutive_no_change = 3  # was idle, but now real work
        runner._save_sessions = MagicMock()

        await runner._auto_continue(aq.session_id)

        # Counter must reset to 0 after a real reply.
        assert aq.consecutive_no_change == 0

        # agent_message_frame must be published for a real reply.
        agent_msg_calls = [
            c
            for c in event_sink.publish.call_args_list
            if c[0][1].get("type") == SSE_AGENT_MESSAGE_TYPE
        ]
        assert len(agent_msg_calls) == 1

    @pytest.mark.asyncio
    async def test_max_idle_zero_disables_idle_cap(self) -> None:
        """When max_idle_auto_turns is 0, the idle cap is disabled."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.max_idle_auto_turns = 0  # disabled
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.proposal_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _noop_stream(*args, **kwargs):
            yield "NO_CHANGE\n[APPROVAL]"
            return

        agent.stream.return_value = _noop_stream()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.executing
        aq.plan_text = "plan"
        aq.consecutive_no_change = 99  # way past any reasonable cap
        runner._save_sessions = MagicMock()

        await runner._auto_continue(aq.session_id)

        # Should NOT have halted on the idle cap (which is disabled).
        # The loop exits via the proposal marker, not the idle cap.
        assert aq.state is AutonomousState.proposal
        # consecutive_no_change still incremented by the no-op turn
        # (idle cap didn't intervene — we exited via the marker instead).


class TestAgentFactoryLoopSafety:
    """Agent factory calling asyncio.run() must not crash inside the event loop."""

    @pytest.mark.asyncio
    async def test_factory_with_asyncio_run_via_to_thread(self) -> None:
        """A factory that calls asyncio.run() must work via asyncio.to_thread.

        Regression test for #752: ``_kickoff_initial_turn`` and ``_auto_continue``
        call ``self._agent_factory()`` inside a running event loop.  The factory
        calls ``create_agent_from_settings`` → ``_inject_skills`` →
        ``fetch_roster_sync`` → ``asyncio.run(fetch_roster(...))``, which raises
        ``RuntimeError: asyncio.run() cannot be called from a running event loop``.
        Wrapping the factory call in ``asyncio.to_thread`` offloads it to a
        separate thread where no loop is running.
        """

        def factory_that_calls_asyncio_run() -> str:
            # Simulates the exact pattern: fetch_roster_sync → asyncio.run(...)
            return asyncio.run(asyncio.sleep(0))  # type: ignore[func-returns-value]

        # Must not raise RuntimeError.
        await asyncio.to_thread(factory_that_calls_asyncio_run)

    @pytest.mark.asyncio
    async def test_kickoff_initial_turn_loop_safe(self) -> None:
        """_kickoff_initial_turn must not crash when agent factory calls asyncio.run().

        Full-path integration: the runner calls the factory via asyncio.to_thread,
        which should prevent the ``asyncio.run() cannot be called from a running
        event loop`` RuntimeError.
        """
        store = ConversationStore()
        store.create_session("owner1")
        sessions, _active = store.list_sessions("owner1")
        sid = sessions[0]["session_id"]

        settings = MagicMock()
        settings.autonomous.initial_task = ""
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        # Factory that triggers the asyncio.run() crash path.
        def factory() -> MagicMock:
            asyncio.run(asyncio.sleep(0))  # simulates fetch_roster_sync
            agent = MagicMock()
            agent.stream = MagicMock()

            async def _empty_stream(*args, **kwargs):
                yield ""
                return

            agent.stream.return_value = _empty_stream()
            return agent

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=factory,
            run_serializer=run_serializer,
        )
        # Must not raise RuntimeError.
        await runner._kickoff_initial_turn(sid, "owner1")


class TestStorePublicMethods:
    """Tests for the new public ConversationStore methods."""

    def test_owner_for_session(self) -> None:
        """owner_for_session returns the owning owner_id.

        Single-user: a client-supplied owner resolves to the canonical
        operator owner, so that is what comes back.
        """
        store = ConversationStore()
        store.create_session("owner1")
        sessions, _active = store.list_sessions("owner1")
        sid = sessions[0]["session_id"]
        assert store.owner_for_session(sid) == OPERATOR_OWNER
        assert store.owner_for_session("nonexistent") is None

    def test_iter_sessions(self) -> None:
        """iter_sessions yields all tracked sessions."""
        store = ConversationStore()
        store.create_session("owner1")
        store.create_session("owner2")
        sessions = dict(store.iter_sessions())
        assert len(sessions) >= 2
        for sid, session in sessions.items():
            assert isinstance(sid, str)
            assert isinstance(session, Session)


class TestConversationStoreRegistration:
    """Autonomous sessions must appear in ``store.list_sessions`` for their owner.

    Regression tests for the UI-invisibility bug: the AutonomousRunner's own
    persistence (autonomous_sessions.json) survived restarts, but the
    conversation-store entry (conversations.json) was missing, so
    ``list_sessions`` never returned the session and the UI never showed it.
    """

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    def test_create_session_registers_in_conversation_store(self) -> None:
        """Creating an autonomous session makes it appear in list_sessions.

        Crucially this holds even when no ordinary store session was created
        for the owner first — the runner must register the session under the
        owner itself (previously it only called ``begin``, which registers
        globally but not under the owner, so ``list_sessions`` never returned
        it).
        """
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=MagicMock(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)

        sessions, _active = store.list_sessions("owner1")
        session_ids = [s["session_id"] for s in sessions]
        assert aq.session_id in session_ids
        # And the runner still reports it as autonomous (drives the UI badge /
        # the `autonomous=True` annotation on the list endpoint).
        assert runner.is_autonomous(aq.session_id)

    def test_create_session_persists_owner_link(self) -> None:
        """The owner→session link is written to disk so it survives a restart.

        A store whose only registration path was ``record`` (with an existing
        owner) would never persist the autonomous session — persistence only
        writes sessions reachable through an owner.  Registering on create
        fixes that.
        """
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            persist = Path(td) / "conversations.json"
            store = ConversationStore(persist_path=persist)
            runner = AutonomousRunner(
                settings=MagicMock(),
                conversation_store=store,
                agent_factory=MagicMock(),
                run_serializer=MagicMock(),
            )
            aq = runner.create_session("owner1", schedule_kickoff=False)

            # The persisted file must contain the owner with this session.
            # "owner1" is a client-supplied id, so it lands in the canonical
            # single-user pool.
            raw = json.loads(persist.read_text())
            assert OPERATOR_OWNER in raw
            persisted_ids = {s["session_id"] for s in raw[OPERATOR_OWNER]["sessions"]}
            assert aq.session_id in persisted_ids

    @pytest.mark.asyncio
    async def test_resume_reconciles_orphaned_session(self) -> None:
        """resume_sessions re-registers a session missing from the store.

        Simulates the live incident: the AutonomousRunner state (loaded from
        autonomous_sessions.json) has a session that the conversation store
        (conversations.json) lacks entirely.  On resume, the runner must
        reconcile it back into the store so it reappears in list_sessions.
        """
        from robotsix_chat.autonomous.models import AutonomousSession as ASession

        store = ConversationStore()
        settings = MagicMock()
        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        # Prevent the resumed executing session from actually driving a loop.
        runner._auto_continue = AsyncMock()

        # Inject an orphaned autonomous session directly (as if loaded from
        # autonomous_sessions.json) with NO corresponding store entry.
        runner._sessions["orphan-1"] = ASession(
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
    async def test_kickoff_publishes_tokens_to_event_sink(self) -> None:
        """_kickoff_initial_turn publishes each streamed token to the event sink."""
        store = ConversationStore()
        store.create_session("owner1")
        sessions, _active = store.list_sessions("owner1")
        sid = sessions[0]["session_id"]

        event_sink = MagicMock()
        settings = MagicMock()
        settings.autonomous.initial_task = "Test task"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _token_stream(*args, **kwargs):
            yield "Hello"
            yield " "
            yield "world!"

        agent.stream.return_value = _token_stream()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
            event_sink=event_sink,
        )
        await runner._kickoff_initial_turn(sid, "owner1")

        # Verify token frames were published.
        token_calls = [
            c
            for c in event_sink.publish.call_args_list
            if c[0][1].get("type") == SSE_AUTONOMOUS_TOKEN_TYPE
        ]
        assert len(token_calls) == 3
        assert token_calls[0][0][1]["token"] == "Hello"
        assert token_calls[1][0][1]["token"] == " "
        assert token_calls[2][0][1]["token"] == "world!"

        # Verify an agent_message frame was published after the stream.
        agent_msg_calls = [
            c
            for c in event_sink.publish.call_args_list
            if c[0][1].get("type") == SSE_AGENT_MESSAGE_TYPE
        ]
        assert len(agent_msg_calls) == 1
        assert agent_msg_calls[0][0][1]["text"] == "Hello world!"

    @pytest.mark.asyncio
    async def test_kickoff_records_to_store(self) -> None:
        """_kickoff_initial_turn records the turn so /history is non-empty."""
        store = ConversationStore()
        store.create_session("owner1")
        sessions, _active = store.list_sessions("owner1")
        sid = sessions[0]["session_id"]

        settings = MagicMock()
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _token_stream(*args, **kwargs):
            yield "Plan text"
            yield " [APPROVAL_NEEDED]"

        agent.stream.return_value = _token_stream()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        await runner._kickoff_initial_turn(sid, "owner1")

        # /history must be non-empty after kickoff.
        turns = store.history(sid)
        assert len(turns) >= 1
        user_msg, asst_msg = turns[0]
        assert "Begin a new autonomous session" in user_msg
        assert "Plan text" in asst_msg
        assert "APPROVAL_NEEDED" in asst_msg

    @pytest.mark.asyncio
    async def test_auto_continue_publishes_tokens_to_event_sink(self) -> None:
        """_auto_continue publishes streamed tokens and agent_message to the sink."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.proposal_marker = "[APPROVAL_NEEDED]"
        settings.autonomous.completion_marker = "[COMPLETED]"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        event_sink = MagicMock()

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _token_stream(*args, **kwargs):
            yield "Executing"
            yield " step 1"

        agent.stream.return_value = _token_stream()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
            event_sink=event_sink,
        )
        # Per-preset max_auto_turns: mock the definition lookup to return 1.
        runner._definition_for_owner = MagicMock(  # type: ignore[method-assign]
            return_value={"max_auto_turns": 1, "name": "default"}
        )
        # Create session without scheduling kickoff so the background task
        # does not also publish tokens / agent_message frames.
        aq = runner.create_session("owner1", schedule_kickoff=False)
        # Manually transition to executing so _auto_continue runs.
        aq.state = AutonomousState.executing
        aq.plan_text = "plan"
        runner._save_sessions = MagicMock()  # re-stub after create_session

        await runner._auto_continue(aq.session_id)

        # Verify token frames were published during the single turn.
        token_calls = [
            c
            for c in event_sink.publish.call_args_list
            if c[0][1].get("type") == SSE_AUTONOMOUS_TOKEN_TYPE
        ]
        assert len(token_calls) == 2
        assert token_calls[0][0][1]["token"] == "Executing"
        assert token_calls[1][0][1]["token"] == " step 1"

        # Verify exactly one agent_message frame was published.
        agent_msg_calls = [
            c
            for c in event_sink.publish.call_args_list
            if c[0][1].get("type") == SSE_AGENT_MESSAGE_TYPE
        ]
        assert len(agent_msg_calls) == 1
        assert agent_msg_calls[0][0][1]["text"] == "Executing step 1"

        # Verify store was recorded.
        turns = store.history(aq.session_id)
        assert len(turns) >= 1


class TestCreateSessionSingleSessionInvariant:
    """create_session must refuse to create a second open session for the same owner."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    def test_create_session_returns_existing_when_open_exists(self) -> None:
        """When owner has an open session, create_session returns it unchanged."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=MagicMock(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq1 = runner.create_session("owner1")
        assert aq1.state is AutonomousState.planning

        # Second call must return the existing session, not create a new one.
        aq2 = runner.create_session("owner1")
        assert aq2.session_id == aq1.session_id
        assert aq2.state is AutonomousState.planning

        # Only one session must exist for owner1.
        owner_sessions = [
            s for s in runner._sessions.values() if s.owner_id == "owner1"
        ]
        assert len(owner_sessions) == 1

    def test_create_session_allows_new_when_existing_is_completed(self) -> None:
        """A completed session does not block creating a new one."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=MagicMock(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq1 = runner.create_session("owner1")
        aq1.state = AutonomousState.completed

        # Should create a new session because the existing one is terminal.
        aq2 = runner.create_session("owner1")
        assert aq2.session_id != aq1.session_id
        assert aq2.state is AutonomousState.planning

        # Both should be in the registry (one completed, one open).
        owner_sessions = [
            s for s in runner._sessions.values() if s.owner_id == "owner1"
        ]
        assert len(owner_sessions) == 2


class TestResumeSessionsNonBlocking:
    """resume_sessions must schedule completed-session respawn as background tasks."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_resume_completed_leaves_as_is(self) -> None:
        """resume_sessions leaves completed sessions as-is (operator closes)."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.initial_task = ""
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.completed
        old_sid = aq.session_id

        # resume_sessions must return without blocking.
        import asyncio

        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)

        # Completed sessions are left as-is — no auto-close, no respawn.
        assert runner.get_session(old_sid) is not None
        assert runner.get_session(old_sid).state is AutonomousState.completed

    @pytest.mark.asyncio
    async def test_resume_executing_schedules_auto_continue(self) -> None:
        """resume_sessions schedules _auto_continue for executing sessions."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.executing
        runner._auto_continue = AsyncMock()

        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)

        # resume_sessions returned quickly.  Give the background task a
        # chance to run, then verify _auto_continue was called via the
        # scheduled background task (not directly awaited).
        await asyncio.sleep(0)
        assert runner._auto_continue.call_count >= 1

    @pytest.mark.asyncio
    async def test_resume_empty_store_bootstraps_session(self) -> None:
        """resume_sessions auto-starts one session when the store is empty."""
        store = ConversationStore()
        settings = MagicMock()
        from types import SimpleNamespace

        settings.autonomous.sessions = [
            SimpleNamespace(
                name="default",
                prompt="",
                trigger_type=SimpleNamespace(value="periodic"),
                trigger_interval_seconds=45.0,
                max_auto_turns=20,
                enabled=True,
                self_refine=False,
                self_refine_require_approval=False,
            )
        ]
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=run_serializer,
        )
        # Verify the runner starts with no sessions.
        assert len(runner._sessions) == 0

        import asyncio

        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)

        # A bootstrap session must have been created for the preset's owner.
        assert len(runner._sessions) == 1
        new_aq = next(iter(runner._sessions.values()))
        assert new_aq.owner_id == "autonomous"
        assert new_aq.state is AutonomousState.planning

    @pytest.mark.asyncio
    async def test_resume_multiple_periodic_presets_fire_at_startup(self) -> None:
        """Every enabled periodic preset bootstraps immediately at startup.

        Regression test for periodic-at-startup semantics: a scheduler that
        treats ``trigger_interval_seconds`` as an initial delay (or that only
        reconciles persisted sessions) leaves named periodic presets idle.
        """
        from types import SimpleNamespace

        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.sessions = [
            SimpleNamespace(
                name=name,
                prompt="",
                trigger_type=SimpleNamespace(value="periodic"),
                trigger_interval_seconds=3600.0,
                max_auto_turns=20,
                enabled=True,
                self_refine=False,
                self_refine_require_approval=False,
            )
            for name in ("cost-review", "mail-check", "release-review")
        ]
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=run_serializer,
        )
        runner._kickoff_initial_turn = AsyncMock()  # type: ignore[method-assign]

        # resume_sessions must return quickly — the interval is a delay
        # between runs, never an initial delay before the first run.
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
            assert matching[0].state is AutonomousState.planning


class TestRestartContextInjection:
    """Restart-context messages are injected when resuming after a restart."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_kickoff_restart_injects_system_restarted(self) -> None:
        """_kickoff_initial_turn with is_restart=True prepends SYSTEM RESTARTED."""
        store = ConversationStore()
        store.create_session("owner1")
        sessions, _active = store.list_sessions("owner1")
        sid = sessions[0]["session_id"]

        settings = MagicMock()
        settings.autonomous.initial_task = ""
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        captured_prompt: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(prompt, *args, **kwargs):
            captured_prompt.append(str(prompt))
            yield ""
            return

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        await runner._kickoff_initial_turn(sid, "owner1", is_restart=True)

        assert len(captured_prompt) == 1
        assert "SYSTEM RESTARTED" in captured_prompt[0]
        assert "resuming an existing autonomous session" in captured_prompt[0]

    @pytest.mark.asyncio
    async def test_kickoff_no_restart_has_no_system_restarted(self) -> None:
        """_kickoff_initial_turn without is_restart has no SYSTEM RESTARTED."""
        store = ConversationStore()
        store.create_session("owner1")
        sessions, _active = store.list_sessions("owner1")
        sid = sessions[0]["session_id"]

        settings = MagicMock()
        settings.autonomous.initial_task = ""
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        captured_prompt: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(prompt, *args, **kwargs):
            captured_prompt.append(str(prompt))
            yield ""
            return

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        await runner._kickoff_initial_turn(sid, "owner1", is_restart=False)

        assert len(captured_prompt) == 1
        assert "SYSTEM RESTARTED" not in captured_prompt[0]

    @pytest.mark.asyncio
    async def test_kickoff_restart_board_unchanged_injects_no_change(
        self, monkeypatch
    ) -> None:
        """Resuming kickoff with an unchanged board digest tells the agent NO_CHANGE."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.initial_task = ""
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

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
            yield ""
            return

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.last_board_digest = "digest-123"

        await runner._kickoff_initial_turn(aq.session_id, "owner1", is_restart=True)

        assert len(captured_prompt) == 1
        assert "SYSTEM RESTARTED" in captured_prompt[0]
        assert "BOARD UNCHANGED" in captured_prompt[0]
        assert "NO_CHANGE" in captured_prompt[0]
        assert aq.last_board_digest == "digest-123"

    @pytest.mark.asyncio
    async def test_kickoff_restart_board_changed_stores_new_digest(
        self, monkeypatch
    ) -> None:
        """A changed board on resume skips NO_CHANGE and persists the new digest."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.initial_task = ""
        settings.autonomous.proposal_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

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
            yield ""
            return

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.last_board_digest = "old-digest"

        await runner._kickoff_initial_turn(aq.session_id, "owner1", is_restart=True)

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
        import hashlib

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

    @pytest.mark.asyncio
    async def test_auto_continue_restart_mid_execution(self) -> None:
        """_auto_continue with is_restart and auto_turn_count>0 injects restart msg."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20  # high enough to not hit the cap
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.proposal_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(message, *args, **kwargs):
            captured_message.append(str(message))
            yield "[APPROVAL]"  # triggers proposal so loop exits
            return

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.executing
        aq.plan_text = "plan"
        aq.auto_turn_count = 3  # mid-execution, not first turn
        runner._save_sessions = MagicMock()

        await runner._auto_continue(aq.session_id, is_restart=True)

        assert len(captured_message) >= 1
        assert "SYSTEM RESTARTED" in captured_message[0]
        assert "Continue" in captured_message[0]

    @pytest.mark.asyncio
    async def test_auto_continue_restart_first_turn(self) -> None:
        """is_restart + auto_turn_count=0 injects restart + approval."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.proposal_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(message, *args, **kwargs):
            captured_message.append(str(message))
            yield "[APPROVAL]"  # triggers proposal so loop exits
            return

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.executing
        aq.plan_text = "plan"
        aq.auto_turn_count = 0  # first turn after approval
        runner._save_sessions = MagicMock()

        await runner._auto_continue(aq.session_id, is_restart=True)

        assert len(captured_message) >= 1
        assert "SYSTEM RESTARTED" in captured_message[0]
        assert "The operator has seen your plan" in captured_message[0]

    @pytest.mark.asyncio
    async def test_auto_continue_no_restart_has_no_system_restarted(self) -> None:
        """_auto_continue without is_restart has no SYSTEM RESTARTED."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.proposal_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(message, *args, **kwargs):
            captured_message.append(str(message))
            yield "[APPROVAL]"
            return

        agent.stream.side_effect = _capture_stream

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=lambda: agent,
            run_serializer=run_serializer,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)
        aq.state = AutonomousState.executing
        aq.plan_text = "plan"
        aq.auto_turn_count = 5
        runner._save_sessions = MagicMock()

        await runner._auto_continue(aq.session_id, is_restart=False)

        assert len(captured_message) >= 1
        assert "SYSTEM RESTARTED" not in captured_message[0]


class TestAutoContinueThrottleAndSubsessionGate:
    """Throttle interval, subsession gate blocking, and timeout fallback.

    Also covers the backward-compat no-registry path.
    """

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _make_subsession_info(is_active: bool = True) -> MagicMock:
        """Return a mock SubsessionInfo with controllable is_active.

        The mock defaults to ``kind="task"`` so it counts as a pending
        (blocking) subsession in ``_has_pending_subsessions``.
        """
        info = MagicMock()
        info.is_active = is_active
        info.kind = "task"
        return info

    @staticmethod
    def _make_registry(
        *subsessions: MagicMock,
    ) -> MagicMock:
        """Return a mock registry with list_for_owner returning *subsessions."""
        reg = MagicMock()
        reg.list_for_owner.return_value = list(subsessions)
        return reg

    # -- tests ------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_throttle_waits_interval(self) -> None:
        """``_wait_before_continue`` sleeps at least ``continue_interval_seconds``."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.continue_interval_seconds = 0.05
        settings.autonomous.pending_subsession_wait_timeout = 600.0

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

        # Should have waited at least the interval (with a small tolerance
        # for test environment scheduling jitter).
        assert elapsed >= 0.04

    @pytest.mark.asyncio
    async def test_subsession_gate_blocks_while_active(self) -> None:
        """``_wait_before_continue`` keeps waiting while active subsessions exist."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.continue_interval_seconds = 0.01
        settings.autonomous.pending_subsession_wait_timeout = 0.5

        # Active subsession that becomes inactive after a few polls.
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

        # Should have waited longer than the interval because the gate
        # kept polling while the subsession was active.
        assert elapsed >= 0.1
        # Registry was consulted at least once.
        assert registry.list_for_owner.call_count >= 1

    @pytest.mark.asyncio
    async def test_subsession_gate_timeout_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_wait_before_continue`` eventually returns when subsession stays active."""
        store = ConversationStore()
        timeout = 0.2
        monkeypatch.setattr(
            "robotsix_chat.autonomous.runner._PENDING_SUBSESSION_WAIT_TIMEOUT",
            timeout,
        )
        settings = MagicMock()
        settings.autonomous.continue_interval_seconds = 0.01

        # Subsession stays active forever — gate must time out.
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

        # Must return without raising, after approximately the timeout.
        assert elapsed >= timeout
        # Must not hugely exceed the timeout (50% tolerance for CI jitter).
        assert elapsed < timeout * 1.5

    @pytest.mark.asyncio
    async def test_no_registry_backward_compat(self) -> None:
        """``_wait_before_continue`` works when subsession_registry is None."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.continue_interval_seconds = 0.01
        settings.autonomous.pending_subsession_wait_timeout = 600.0

        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
            subsession_registry=None,
        )
        aq = runner.create_session("owner1", schedule_kickoff=False)

        # Must not raise despite having no registry.
        await runner._wait_before_continue(aq.session_id)


# ---------------------------------------------------------------------------
# _rejected_subjects_note tests
# ---------------------------------------------------------------------------


class TestRejectedSubjectsNote:
    """Tests for the _rejected_subjects_note helper."""

    def test_none_session(self) -> None:
        """Returns empty string when session is None."""
        from robotsix_chat.autonomous.runner import _rejected_subjects_note

        assert _rejected_subjects_note(None) == ""

    def test_no_rejections(self) -> None:
        """Returns empty string when rejected_subjects is empty/None."""
        from robotsix_chat.autonomous.models import AutonomousSession
        from robotsix_chat.autonomous.runner import _rejected_subjects_note

        aq = AutonomousSession(session_id="s1", owner_id="o1")
        assert _rejected_subjects_note(aq) == ""

    def test_with_rejections(self) -> None:
        """Returns a formatted note with all rejected subjects."""
        from robotsix_chat.autonomous.models import AutonomousSession
        from robotsix_chat.autonomous.runner import _rejected_subjects_note

        aq = AutonomousSession(
            session_id="s1",
            owner_id="o1",
            rejected_subjects=["Subject A", "Subject B"],
        )
        note = _rejected_subjects_note(aq)
        assert "PREVIOUSLY REJECTED SUBJECTS" in note
        assert "Subject A" in note
        assert "Subject B" in note
        assert "do NOT propose" in note


class TestStalemateDetection:
    """Stalemate detection in ``on_user_message`` — repeated identical messages."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    def _make_runner(self) -> AutonomousRunner:
        store = ConversationStore()
        settings = MagicMock()
        from types import SimpleNamespace

        settings.autonomous.sessions = [
            SimpleNamespace(
                name="default",
                prompt="",
                trigger_type=SimpleNamespace(value="periodic"),
                trigger_interval_seconds=45.0,
                max_auto_turns=20,
                enabled=True,
                self_refine=False,
                self_refine_require_approval=False,
            )
        ]
        return AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )

    def test_no_stalemate_on_first_message(self) -> None:
        """The first occurrence of a message never triggers stalemate."""
        runner = self._make_runner()
        aq = runner.create_session("owner1")
        # Move to proposal so on_user_message processes it.
        aq.state = AutonomousState.proposal

        result = runner.on_user_message(aq.session_id, "Hello")
        assert result == "neutral"

    def test_no_stalemate_on_second_identical(self) -> None:
        """Two identical messages do NOT yet trigger stalemate (need 3+)."""
        runner = self._make_runner()
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.proposal

        runner.on_user_message(aq.session_id, "Hello")
        result = runner.on_user_message(aq.session_id, "Hello")
        assert result == "neutral"

    def test_stalemate_on_third_identical(self) -> None:
        """The third identical message triggers stalemate (repeat_count >= 2)."""
        runner = self._make_runner()
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.proposal

        runner.on_user_message(aq.session_id, "Hello")
        runner.on_user_message(aq.session_id, "Hello")
        result = runner.on_user_message(aq.session_id, "Hello")
        assert result == "stalemate"

    def test_stalemate_reset_by_different_message(self) -> None:
        """A different message between repeats prevents stalemate on the third."""
        runner = self._make_runner()
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.proposal

        runner.on_user_message(aq.session_id, "Hello")
        runner.on_user_message(aq.session_id, "Something else entirely")
        runner.on_user_message(aq.session_id, "Hello")
        # Only 1 prior "Hello" in recent list (the first one), so no stalemate.
        result = runner.on_user_message(aq.session_id, "Hello")
        assert result == "neutral"

    def test_stalemate_after_fourth_identical(self) -> None:
        """Stalemate persists on the fourth identical message."""
        runner = self._make_runner()
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.proposal

        runner.on_user_message(aq.session_id, "Hello")
        runner.on_user_message(aq.session_id, "Hello")
        runner.on_user_message(aq.session_id, "Hello")  # stalemate
        result = runner.on_user_message(aq.session_id, "Hello")  # still stalemate
        assert result == "stalemate"

    def test_stalemate_detection_in_planning_state(self) -> None:
        """Stalemate detection works even when session is not in proposal state."""
        runner = self._make_runner()
        aq = runner.create_session("owner1")
        # Stays in planning (default).

        runner.on_user_message(aq.session_id, "Begin a new autonomous session")
        runner.on_user_message(aq.session_id, "Begin a new autonomous session")
        result = runner.on_user_message(aq.session_id, "Begin a new autonomous session")
        assert result == "stalemate"

    def test_unknown_session_returns_neutral(self) -> None:
        """on_user_message on unknown session always returns neutral."""
        runner = self._make_runner()
        result = runner.on_user_message("unknown", "Hello")
        assert result == "neutral"

    def test_recent_messages_stored_on_session(self) -> None:
        """recent_user_messages is populated on the AutonomousSession object."""
        runner = self._make_runner()
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.proposal

        runner.on_user_message(aq.session_id, "msg1")
        runner.on_user_message(aq.session_id, "msg2")
        runner.on_user_message(aq.session_id, "msg1")

        assert aq.recent_user_messages == ["msg1", "msg2", "msg1"]


async def _drain_tasks(runner: AutonomousRunner) -> None:
    """Await every background task the runner scheduled, following cascades.

    A scheduled task (e.g. auto-restart) may itself schedule another (the
    fresh session's kickoff), so drain repeatedly until the set is quiet.
    """
    for _ in range(20):
        pending = [t for t in list(runner._auto_tasks) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


class TestEnsureActiveSessionAutoRestart:
    """`ensure_active_session`, `forget_session`, and auto-restart-always."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    def _make_runner(self) -> AutonomousRunner:
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
        settings.autonomous.proposal_marker = "---PROPOSAL READY---"
        settings.autonomous.continue_interval_seconds = 0
        from types import SimpleNamespace

        settings.autonomous.sessions = [
            SimpleNamespace(
                name="default",
                prompt="",
                trigger_type=SimpleNamespace(value="periodic"),
                trigger_interval_seconds=45.0,
                max_auto_turns=20,
                enabled=True,
                self_refine=False,
                self_refine_require_approval=False,
            )
        ]
        return AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )

    def test_bootstrap_owner_property(self) -> None:
        """The bootstrap owner id is the fixed 'autonomous' pseudo-owner."""
        runner = self._make_runner()
        assert runner.bootstrap_owner == "autonomous"

    def test_forget_session_removes_and_reports(self) -> None:
        """forget_session drops the record and returns whether it existed."""
        runner = self._make_runner()
        aq = runner.create_session("autonomous", schedule_kickoff=False)
        assert runner.forget_session(aq.session_id) is True
        assert not runner.is_autonomous(aq.session_id)
        # Second call is a no-op.
        assert runner.forget_session(aq.session_id) is False

    def test_ensure_active_returns_existing_open_session(self) -> None:
        """When an open session exists, ensure_active_session is a no-op."""
        runner = self._make_runner()
        existing = runner.create_session("autonomous", schedule_kickoff=False)
        result = runner.ensure_active_session("autonomous", schedule_kickoff=False)
        assert result.session_id == existing.session_id
        # Exactly one session tracked.
        assert len(runner._sessions) == 1

    def test_ensure_active_retires_completed_and_starts_fresh(self) -> None:
        """A completed-only owner gets its stale session retired + a fresh run."""
        runner = self._make_runner()
        done = runner.create_session("autonomous", schedule_kickoff=False)
        done.state = AutonomousState.completed

        fresh = runner.ensure_active_session("autonomous", schedule_kickoff=False)

        assert fresh.session_id != done.session_id
        assert fresh.state is AutonomousState.planning
        # The completed session is gone from both runner and store.
        assert not runner.is_autonomous(done.session_id)
        sessions, _ = runner._store.list_sessions("autonomous", create_default=False)
        ids = {s["session_id"] for s in sessions}
        assert done.session_id not in ids
        assert fresh.session_id in ids

    @pytest.mark.asyncio
    async def test_completion_schedules_auto_restart(self) -> None:
        """Hitting the completion marker starts a fresh run (auto-restart)."""
        runner = self._make_runner()
        # Stub the agent kickoff so the fresh run doesn't drive a real turn.
        runner._kickoff_initial_turn = AsyncMock()  # type: ignore[method-assign]
        aq = runner.create_session("autonomous", schedule_kickoff=False)
        aq.state = AutonomousState.executing

        new_state = runner.check_reply_for_markers(
            aq.session_id, "done\n\n---AUTONOMOUS COMPLETE---"
        )
        assert new_state is AutonomousState.completed

        # The auto-restart background task was scheduled; let it (and any
        # kickoff task it spawns) run to completion.
        await _drain_tasks(runner)

        # The completed session has been retired and a fresh open one started.
        open_sessions = [
            s
            for s in runner._sessions.values()
            if s.state is not AutonomousState.completed
        ]
        assert len(open_sessions) == 1
        assert open_sessions[0].session_id != aq.session_id

    @pytest.mark.asyncio
    async def test_resume_starts_fresh_when_only_completed(self) -> None:
        """resume_sessions guarantees one open run even if all were completed."""
        runner = self._make_runner()
        runner._kickoff_initial_turn = AsyncMock()  # type: ignore[method-assign]
        done = runner.create_session("autonomous", schedule_kickoff=False)
        done.state = AutonomousState.completed

        await runner.resume_sessions()
        await _drain_tasks(runner)

        open_sessions = [
            s
            for s in runner._sessions.values()
            if s.state is not AutonomousState.completed
        ]
        assert len(open_sessions) == 1
        assert open_sessions[0].session_id != done.session_id


# ---------------------------------------------------------------------------
# _schedule_refinement integration
# ---------------------------------------------------------------------------


class TestScheduleRefinement:
    """Tests for the runner's _schedule_refinement integration."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @staticmethod
    def _make_refinement_store() -> MagicMock:
        """Return a MagicMock standing in for RefinementStore."""
        store = MagicMock()
        store.propose_refinement = AsyncMock()
        store.effective_prompt = MagicMock(return_value="base + addendum")
        store.get_state = MagicMock()
        store.get_entries = MagicMock(return_value=[])
        return store

    def _make_runner(
        self,
        tmp_path: Path,
        *,
        self_refine: bool = True,
        refinement_store: MagicMock | None = None,
    ) -> AutonomousRunner:
        """Build a runner with a session definition and optional refinement store."""
        conv_store = ConversationStore()
        settings = MagicMock()
        from types import SimpleNamespace

        settings.autonomous.sessions = [
            SimpleNamespace(
                name="default",
                prompt="base prompt",
                trigger_type=SimpleNamespace(value="periodic"),
                trigger_interval_seconds=45.0,
                max_auto_turns=20,
                enabled=True,
                self_refine=self_refine,
                self_refine_require_approval=False,
            )
        ]
        runner = AutonomousRunner(
            settings=settings,
            conversation_store=conv_store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
            refinement_store=refinement_store,
        )
        # Suppress kickoff side-effects in tests.
        runner._kickoff_initial_turn = AsyncMock()  # type: ignore[method-assign]
        return runner

    def test_no_refinement_store_is_noop(self, tmp_path: Path) -> None:
        """_schedule_refinement returns early when no refinement store is set."""
        runner = self._make_runner(tmp_path, refinement_store=None)
        aq = runner.create_session("autonomous")
        # Should not raise.
        runner._schedule_refinement(aq.session_id, aq)

    def test_no_self_refine_is_noop(self, tmp_path: Path) -> None:
        """_schedule_refinement returns early when self_refine is disabled."""
        mock_store = self._make_refinement_store()
        runner = self._make_runner(
            tmp_path, self_refine=False, refinement_store=mock_store
        )
        aq = runner.create_session("autonomous")
        runner._schedule_refinement(aq.session_id, aq)
        # propose_refinement should never have been called.
        mock_store.propose_refinement.assert_not_called()

    def test_unknown_owner_is_noop(self, tmp_path: Path) -> None:
        """_schedule_refinement is a no-op for owners with no definition."""
        mock_store = self._make_refinement_store()
        runner = self._make_runner(tmp_path, refinement_store=mock_store)
        # owner "bogus" has no definition.
        aq = runner.create_session("bogus")
        runner._schedule_refinement(aq.session_id, aq)
        mock_store.propose_refinement.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedules_propose_refinement(self, tmp_path: Path) -> None:
        """_schedule_refinement schedules a propose_refinement background task."""
        mock_store = self._make_refinement_store()
        runner = self._make_runner(tmp_path, refinement_store=mock_store)
        # Suppress kickoff side-effects.
        runner._kickoff_initial_turn = AsyncMock()  # type: ignore[method-assign]
        aq = runner.create_session("autonomous")
        # Put some history in the conversation store.
        runner._store.record(aq.session_id, aq.owner_id, "user msg", "agent reply")

        runner._schedule_refinement(aq.session_id, aq)

        # The background task was scheduled; we need to drain it.
        await _drain_tasks(runner)

        mock_store.propose_refinement.assert_called_once()
        call_kwargs = mock_store.propose_refinement.call_args.kwargs
        assert call_kwargs["definition_name"] == "default"
        assert call_kwargs["base_prompt"] == "base prompt"
        assert call_kwargs["session_id"] == aq.session_id
        # auto_accept=True when require_approval is False.
        assert call_kwargs["auto_accept"] is True

    @pytest.mark.asyncio
    async def test_auto_accept_false_when_approval_required(
        self, tmp_path: Path
    ) -> None:
        """auto_accept=False when self_refine_require_approval is True."""
        mock_store = self._make_refinement_store()
        conv_store = ConversationStore()
        settings = MagicMock()
        from types import SimpleNamespace

        settings.autonomous.sessions = [
            SimpleNamespace(
                name="default",
                prompt="base prompt",
                trigger_type=SimpleNamespace(value="periodic"),
                trigger_interval_seconds=45.0,
                max_auto_turns=20,
                enabled=True,
                self_refine=True,
                self_refine_require_approval=True,
            )
        ]
        runner = AutonomousRunner(
            settings=settings,
            conversation_store=conv_store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
            refinement_store=mock_store,
        )
        # Suppress kickoff side-effects.
        runner._kickoff_initial_turn = AsyncMock()  # type: ignore[method-assign]
        aq = runner.create_session("autonomous")
        runner._store.record(aq.session_id, aq.owner_id, "user msg", "agent reply")

        runner._schedule_refinement(aq.session_id, aq)
        await _drain_tasks(runner)

        mock_store.propose_refinement.assert_called_once()
        assert mock_store.propose_refinement.call_args.kwargs["auto_accept"] is False

    @pytest.mark.asyncio
    async def test_history_truncation_head_tail(self, tmp_path: Path) -> None:
        """Long history is truncated to head+tail before passing to refinement."""
        mock_store = self._make_refinement_store()
        runner = self._make_runner(tmp_path, refinement_store=mock_store)
        aq = runner.create_session("autonomous")
        # Generate a conversation large enough to trigger truncation (>30k chars).
        # max_history_turns is 50, so we need per-turn length high enough.
        long_msg = "x" * 800
        for i in range(50):
            runner._store.record(
                aq.session_id,
                aq.owner_id,
                f"u{i}",
                long_msg,
            )

        runner._schedule_refinement(aq.session_id, aq)
        await _drain_tasks(runner)

        mock_store.propose_refinement.assert_called_once()
        history_text = mock_store.propose_refinement.call_args.kwargs[
            "conversation_history"
        ]
        # Should contain truncation marker.
        assert "transcript truncated" in history_text

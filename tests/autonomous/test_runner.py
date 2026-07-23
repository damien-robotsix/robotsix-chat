"""Tests for the AutonomousRunner state machine and auto-continue logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_chat.autonomous.models import AutonomousState
from robotsix_chat.autonomous.runner import AutonomousRunner
from robotsix_chat.chat.conversation import ConversationStore, Session
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
        assert aq.state is AutonomousState.selecting_subject
        assert runner.is_autonomous(aq.session_id)
        assert runner.get_state(aq.session_id) is AutonomousState.selecting_subject

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
        settings.autonomous.approval_marker = "---AWAITING APPROVAL---"
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

    def test_approval_marker_transitions_to_awaiting(self, runner) -> None:
        """Approval marker moves state to awaiting_approval and stores plan."""
        aq = runner.create_session("owner1")
        reply = "Here is my plan:\n1. Do X\n2. Do Y\n\n---AWAITING APPROVAL---"
        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        assert new_state is AutonomousState.awaiting_approval
        assert aq.state is AutonomousState.awaiting_approval
        assert "Here is my plan:" in aq.plan_text
        assert "---AWAITING APPROVAL---" not in aq.plan_text

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
        assert aq.state is AutonomousState.selecting_subject

    def test_unknown_session_returns_none(self, runner) -> None:
        """Marker scan on unknown session returns None."""
        result = runner.check_reply_for_markers("unknown", "---AWAITING APPROVAL---")
        assert result is None

    def test_completion_takes_priority_over_approval(self, runner) -> None:
        """When both markers appear, completion wins."""
        aq = runner.create_session("owner1")
        reply = "Plan:\n---AWAITING APPROVAL---\nDone:\n---AUTONOMOUS COMPLETE---"
        new_state = runner.check_reply_for_markers(aq.session_id, reply)
        assert new_state is AutonomousState.completed


class TestApprovalGate:
    """Approve/reject endpoint logic tests."""

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
        settings.autonomous.approval_marker = "---AWAITING APPROVAL---"
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

    @pytest.mark.asyncio
    async def test_approve_success(self, runner) -> None:
        """Approval transitions to executing and schedules auto-continue."""
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.awaiting_approval
        runner._auto_continue = AsyncMock()  # prevent coroutine creation
        ok, reason = runner.approve("owner1", aq.session_id)
        assert ok
        assert reason == ""
        assert aq.state is AutonomousState.executing

    def test_approve_wrong_owner(self, runner) -> None:
        """Approval with mismatched owner_id fails."""
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.awaiting_approval
        ok, reason = runner.approve("owner2", aq.session_id)
        assert not ok
        assert "owner_id mismatch" in reason

    def test_approve_wrong_state(self, runner) -> None:
        """Approval when not in awaiting_approval fails."""
        aq = runner.create_session("owner1")
        ok, reason = runner.approve("owner1", aq.session_id)
        assert not ok
        assert "not awaiting_approval" in reason.lower()

    def test_approve_unknown_session(self, runner) -> None:
        """Approval of unknown session fails."""
        ok, reason = runner.approve("owner1", "unknown")
        assert not ok
        assert "not found" in reason.lower()

    def test_reject_success(self, runner) -> None:
        """Rejection resets to selecting_subject and clears plan."""
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.awaiting_approval
        ok, reason = runner.reject("owner1", aq.session_id)
        assert ok
        assert reason == ""
        assert aq.state is AutonomousState.selecting_subject
        assert aq.plan_text == ""

    def test_reject_wrong_owner(self, runner) -> None:
        """Rejection with mismatched owner_id fails."""
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.awaiting_approval
        ok, reason = runner.reject("owner2", aq.session_id)
        assert not ok
        assert "owner_id mismatch" in reason


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
        """When max_auto_turns is reached, revert to awaiting_approval."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.approval_marker = "---AWAITING APPROVAL---"
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

        assert aq.state is AutonomousState.awaiting_approval

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
        assert aq.state is AutonomousState.selecting_subject


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
        import asyncio

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
        import asyncio

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
        """owner_for_session returns the owning owner_id."""
        store = ConversationStore()
        store.create_session("owner1")
        sessions, _active = store.list_sessions("owner1")
        sid = sessions[0]["session_id"]
        assert store.owner_for_session(sid) == "owner1"
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
        settings.autonomous.initial_task = "Test task"
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
        assert "Test task" in user_msg
        assert "Plan text" in asst_msg
        assert "APPROVAL_NEEDED" in asst_msg

    @pytest.mark.asyncio
    async def test_auto_continue_publishes_tokens_to_event_sink(self) -> None:
        """_auto_continue publishes streamed tokens and agent_message to the sink."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 1
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.approval_marker = "[APPROVAL_NEEDED]"
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


class TestCloseAndRespawn:
    """Tests for _close_and_respawn: non-blocking, single-session invariant."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_close_and_respawn_removes_completed_and_creates_new(self) -> None:
        """_close_and_respawn removes the completed session and spawns a successor."""
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

        await runner._close_and_respawn(old_sid)

        # Old session must be gone from the runner's registry.
        assert runner.get_session(old_sid) is None

        # A new session must exist for owner1 in a non-terminal state.
        new_session = None
        for _sid, session in runner._sessions.items():
            if session.owner_id == "owner1":
                new_session = session
                break
        assert new_session is not None
        assert new_session.session_id != old_sid
        assert new_session.state is AutonomousState.selecting_subject

    @pytest.mark.asyncio
    async def test_close_and_respawn_is_idempotent(self) -> None:
        """_close_and_respawn called twice for the same session spawns one successor."""
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

        await runner._close_and_respawn(old_sid)
        # Second call with the same (now-gone) session_id must be a no-op.
        await runner._close_and_respawn(old_sid)

        # Only one new session should exist for owner1.
        open_count = sum(
            1
            for s in runner._sessions.values()
            if s.owner_id == "owner1" and s.state is not AutonomousState.completed
        )
        assert open_count == 1

    @pytest.mark.asyncio
    async def test_close_and_respawn_enforces_single_session(self) -> None:
        """_close_and_respawn refuses to spawn when owner has an open session."""
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
        # Create two sessions for the same owner (bypassing the guard via
        # direct dict insertion to simulate a pre-existing open session).
        aq1 = runner.create_session("owner1")
        aq2 = runner.create_session("owner1")  # returns aq1 due to guard
        assert aq2.session_id == aq1.session_id  # guard returned existing

        # Manually inject a second open session to simulate a stale/buggy state.
        from robotsix_chat.autonomous.models import AutonomousSession as ASession

        rogue = ASession(
            session_id="rogue-1", owner_id="owner1", state=AutonomousState.executing
        )
        runner._sessions["rogue-1"] = rogue

        # Mark aq1 as completed, then try to respawn.
        aq1.state = AutonomousState.completed
        await runner._close_and_respawn(aq1.session_id)

        # The rogue open session should still be there — no new session spawned.
        assert "rogue-1" in runner._sessions
        # aq1 should be gone.
        assert runner.get_session(aq1.session_id) is None
        # No new session should have been created (only rogue + aq1-removed).
        open_sessions = [
            s
            for s in runner._sessions.values()
            if s.owner_id == "owner1" and s.state is not AutonomousState.completed
        ]
        assert len(open_sessions) == 1
        assert open_sessions[0].session_id == "rogue-1"

    @pytest.mark.asyncio
    async def test_close_and_respawn_unknown_session_is_noop(self) -> None:
        """_close_and_respawn on an unknown session returns immediately."""
        store = ConversationStore()
        runner = AutonomousRunner(
            settings=MagicMock(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        # Must not raise.
        await runner._close_and_respawn("nonexistent")

    @pytest.mark.asyncio
    async def test_close_and_respawn_kickoff_is_background(self) -> None:
        """_close_and_respawn returns immediately; kickoff is scheduled, not awaited."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.initial_task = "test"
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

        # _close_and_respawn should return without blocking on agent I/O.
        import asyncio

        await asyncio.wait_for(runner._close_and_respawn(aq.session_id), timeout=0.5)

        # A new session must exist (kickoff is background; session exists immediately).
        assert len(runner._sessions) == 1
        new_aq = next(iter(runner._sessions.values()))
        assert new_aq.state is AutonomousState.selecting_subject


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
        assert aq1.state is AutonomousState.selecting_subject

        # Second call must return the existing session, not create a new one.
        aq2 = runner.create_session("owner1")
        assert aq2.session_id == aq1.session_id
        assert aq2.state is AutonomousState.selecting_subject

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
        assert aq2.state is AutonomousState.selecting_subject

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
    async def test_resume_completed_schedules_background(self) -> None:
        """resume_sessions returns immediately; _close_and_respawn is not awaited."""
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

        # Yield control so the background task runs (_close_and_respawn is
        # non-blocking and completes synchronously within its task).
        await asyncio.sleep(0)

        # The completed session must be closed and removed, and a new one
        # spawned.
        assert runner.get_session(old_sid) is None
        assert len(runner._sessions) == 1
        new_aq = next(iter(runner._sessions.values()))
        assert new_aq.state is AutonomousState.selecting_subject

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

        import asyncio

        await asyncio.wait_for(runner.resume_sessions(), timeout=0.5)

        # resume_sessions returned quickly.  Give the background task a
        # chance to run, then verify _auto_continue was called via the
        # scheduled background task (not directly awaited).
        await asyncio.sleep(0)
        assert runner._auto_continue.call_count >= 1


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
    async def test_auto_continue_restart_mid_execution(self) -> None:
        """_auto_continue with is_restart and auto_turn_count>0 injects restart msg."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20  # high enough to not hit the cap
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.approval_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(message, *args, **kwargs):
            captured_message.append(str(message))
            yield "[APPROVAL]"  # triggers awaiting_approval so loop exits
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
        settings.autonomous.approval_marker = "[APPROVAL]"
        settings.autonomous.completion_marker = "[COMPLETE]"
        run_serializer = MagicMock()
        run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
        run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

        captured_message: list[str] = []

        agent = MagicMock()
        agent.stream = MagicMock()

        async def _capture_stream(message, *args, **kwargs):
            captured_message.append(str(message))
            yield "[APPROVAL]"  # triggers awaiting_approval so loop exits
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
        assert "OPERATOR APPROVAL RECEIVED" in captured_message[0]

    @pytest.mark.asyncio
    async def test_auto_continue_no_restart_has_no_system_restarted(self) -> None:
        """_auto_continue without is_restart has no SYSTEM RESTARTED."""
        store = ConversationStore()
        settings = MagicMock()
        settings.autonomous.max_auto_turns = 20
        settings.autonomous.continue_interval_seconds = 0
        settings.autonomous.pending_subsession_wait_timeout = 0
        settings.autonomous.approval_marker = "[APPROVAL]"
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


class TestCloseAndRespawnExceptionLogging:
    """_close_and_respawn must log exceptions instead of crashing background tasks."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_close_and_respawn_logs_exception(self) -> None:
        """Exception inside _close_and_respawn is logged, not raised."""
        store = ConversationStore()
        # Make close_session raise to simulate a store error.
        store.close_session = MagicMock(side_effect=RuntimeError("store failure"))

        runner = AutonomousRunner(
            settings=MagicMock(),
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq = runner.create_session("owner1")
        aq.state = AutonomousState.completed

        # Patch the logger so we can assert the exception was logged.
        with MagicMock() as mock_logger:
            # Temporarily swap the module-level logger.
            import robotsix_chat.autonomous.runner as runner_mod

            orig_logger = runner_mod.logger
            runner_mod.logger = mock_logger
            try:
                # Must not raise.
                await runner._close_and_respawn(aq.session_id)
            finally:
                runner_mod.logger = orig_logger

        # Verify exception was logged.
        assert mock_logger.exception.called
        call_args = mock_logger.exception.call_args[0]
        assert "Error in _close_and_respawn" in call_args[0]

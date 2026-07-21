"""Tests for the AutonomousRunner state machine and auto-continue logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_chat.autonomous.models import AutonomousState
from robotsix_chat.autonomous.runner import AutonomousRunner
from robotsix_chat.chat.conversation import ConversationStore, Session


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
        runner = AutonomousRunner(
            settings=settings,
            conversation_store=store,
            agent_factory=MagicMock(),
            run_serializer=MagicMock(),
        )
        aq = runner.create_session("owner1")
        await runner._auto_continue(aq.session_id)
        assert aq.state is AutonomousState.selecting_subject


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

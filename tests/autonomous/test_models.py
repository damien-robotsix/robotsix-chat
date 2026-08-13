"""Tests for autonomous session data models."""

from __future__ import annotations

from robotsix_chat.autonomous.models import AutonomousSession, AutonomousState


class TestAutonomousState:
    """AutonomousState enum tests."""

    def test_values(self) -> None:
        """Each enum member equals its string value."""
        assert AutonomousState.planning == "planning"
        assert AutonomousState.proposal == "proposal"
        assert AutonomousState.executing == "executing"
        assert AutonomousState.completed == "completed"

    def test_is_str_enum(self) -> None:
        """AutonomousState is a string enum (comparable directly to str)."""
        assert isinstance(AutonomousState.planning, str)


class TestAutonomousSession:
    """AutonomousSession dataclass tests."""

    def test_defaults(self) -> None:
        """Default values for plan_text, state, turn_count, and rejected_subjects."""
        aq = AutonomousSession(session_id="abc", owner_id="owner1")
        assert aq.session_id == "abc"
        assert aq.owner_id == "owner1"
        assert aq.state is AutonomousState.planning
        assert aq.plan_text == ""
        assert aq.auto_turn_count == 0
        assert aq.rejected_subjects is None
        assert aq.last_board_digest == ""

    def test_custom_state(self) -> None:
        """All fields accept explicit values."""
        aq = AutonomousSession(
            session_id="abc",
            owner_id="owner1",
            state=AutonomousState.executing,
            plan_text="a plan",
            auto_turn_count=5,
            last_board_digest="abc123",
        )
        assert aq.state is AutonomousState.executing
        assert aq.plan_text == "a plan"
        assert aq.auto_turn_count == 5
        assert aq.last_board_digest == "abc123"

"""Tests for autonomous session data models."""

from __future__ import annotations

from robotsix_chat.autonomous.models import AutonomousSession, AutonomousState


class TestAutonomousState:
    """AutonomousState enum tests."""

    def test_values(self) -> None:
        """Each enum member equals its string value."""
        assert AutonomousState.selecting_subject == "selecting_subject"
        assert AutonomousState.awaiting_approval == "awaiting_approval"
        assert AutonomousState.executing == "executing"
        assert AutonomousState.completed == "completed"

    def test_is_str_enum(self) -> None:
        """AutonomousState is a string enum (comparable directly to str)."""
        assert isinstance(AutonomousState.selecting_subject, str)


class TestAutonomousSession:
    """AutonomousSession dataclass tests."""

    def test_defaults(self) -> None:
        """Default values for plan_text, state, and auto_turn_count."""
        aq = AutonomousSession(session_id="abc", owner_id="owner1")
        assert aq.session_id == "abc"
        assert aq.owner_id == "owner1"
        assert aq.state is AutonomousState.selecting_subject
        assert aq.plan_text == ""
        assert aq.auto_turn_count == 0

    def test_custom_state(self) -> None:
        """All fields accept explicit values."""
        aq = AutonomousSession(
            session_id="abc",
            owner_id="owner1",
            state=AutonomousState.executing,
            plan_text="a plan",
            auto_turn_count=5,
        )
        assert aq.state is AutonomousState.executing
        assert aq.plan_text == "a plan"
        assert aq.auto_turn_count == 5

"""Tests for autonomous session data models."""

from __future__ import annotations

from robotsix_chat.autonomous.models import AutonomousSession, AutonomousState


class TestAutonomousState:
    """AutonomousState enum tests."""

    def test_values(self) -> None:
        """Each enum member equals its string value."""
        assert AutonomousState.executing == "executing"
        assert AutonomousState.completed == "completed"

    def test_is_str_enum(self) -> None:
        """AutonomousState is a string enum (comparable directly to str)."""
        assert isinstance(AutonomousState.executing, str)


class TestAutonomousSession:
    """AutonomousSession dataclass tests."""

    def test_defaults(self) -> None:
        """Default values for state and turn counts."""
        aq = AutonomousSession(session_id="abc", owner_id="owner1")
        assert aq.session_id == "abc"
        assert aq.owner_id == "owner1"
        assert aq.state is AutonomousState.executing
        assert aq.auto_turn_count == 0
        assert aq.definition_name == ""

    def test_custom_state(self) -> None:
        """All fields accept explicit values."""
        aq = AutonomousSession(
            session_id="abc",
            owner_id="owner1",
            state=AutonomousState.completed,
            auto_turn_count=5,
            definition_name="nightly",
        )
        assert aq.state is AutonomousState.completed
        assert aq.auto_turn_count == 5
        assert aq.definition_name == "nightly"

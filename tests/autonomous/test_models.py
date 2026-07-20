"""Tests for autonomous session models."""

from robotsix_chat.autonomous.models import (
    AUTONOMOUS_KIND,
    AUTONOMOUS_TITLE_PREFIX,
    DEFAULT_KIND,
    VALID_KINDS,
    AutonomousState,
)


class TestAutonomousState:
    """Tests for the AutonomousState enum."""

    def test_all_states_defined(self) -> None:
        """All four lifecycle states have the expected string values."""
        assert AutonomousState.SELECTING_SUBJECT == "selecting_subject"
        assert AutonomousState.AWAITING_APPROVAL == "awaiting_approval"
        assert AutonomousState.EXECUTING == "executing"
        assert AutonomousState.COMPLETED == "completed"

    def test_state_values_are_strings(self) -> None:
        """Every state's value is a string."""
        for state in AutonomousState:
            assert isinstance(state.value, str)

    def test_transition_order(self) -> None:
        """States are defined in lifecycle order."""
        states = list(AutonomousState)
        assert states == [
            AutonomousState.SELECTING_SUBJECT,
            AutonomousState.AWAITING_APPROVAL,
            AutonomousState.EXECUTING,
            AutonomousState.COMPLETED,
        ]


class TestKindConstants:
    """Tests for session kind constants."""

    def test_autonomous_kind(self) -> None:
        """The autonomous kind constant is 'autonomous'."""
        assert AUTONOMOUS_KIND == "autonomous"

    def test_default_kind(self) -> None:
        """The default kind constant is 'chat'."""
        assert DEFAULT_KIND == "chat"

    def test_valid_kinds(self) -> None:
        """The valid kinds set contains 'chat' and 'autonomous'."""
        assert frozenset({"chat", "autonomous"}) == VALID_KINDS

    def test_title_prefix(self) -> None:
        """The autonomous title prefix is '[AUTONOMOUS] '."""
        assert AUTONOMOUS_TITLE_PREFIX == "[AUTONOMOUS] "

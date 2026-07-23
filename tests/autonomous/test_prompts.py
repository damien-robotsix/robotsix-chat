"""Tests for autonomous protocol prompt generation."""

from __future__ import annotations

from unittest.mock import MagicMock

from robotsix_chat.autonomous.prompts import build_autonomous_instruction


class TestBuildAutonomousInstruction:
    """Tests for build_autonomous_instruction()."""

    def test_includes_approval_marker(self) -> None:
        """Default markers and lifecycle sections are present."""
        settings = MagicMock()
        settings.autonomous.approval_marker = "---AWAITING APPROVAL---"
        settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
        result = build_autonomous_instruction(settings)
        assert "---AWAITING APPROVAL---" in result
        assert "---AUTONOMOUS COMPLETE---" in result
        assert "SUBJECT SELECTION" in result
        assert "PLAN DRAFTING" in result
        assert "APPROVAL GATE" in result
        assert "EXECUTION" in result
        assert "CLOSURE" in result
        assert "no-change loop" in result.lower()

    def test_escalation_guidance_present(self) -> None:
        """The stuck/no-change loop escalation section is present."""
        settings = MagicMock()
        settings.autonomous.approval_marker = "---AWAITING APPROVAL---"
        settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
        result = build_autonomous_instruction(settings)
        assert "Re-trigger implementation" in result
        assert "Request human review" in result
        assert "Suggest direct debugging" in result

    def test_custom_markers(self) -> None:
        """Custom marker strings are injected, defaults are absent."""
        settings = MagicMock()
        settings.autonomous.approval_marker = "---CUSTOM APPROVAL---"
        settings.autonomous.completion_marker = "---CUSTOM COMPLETE---"
        result = build_autonomous_instruction(settings)
        assert "---CUSTOM APPROVAL---" in result
        assert "---CUSTOM COMPLETE---" in result
        assert "---AWAITING APPROVAL---" not in result

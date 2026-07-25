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
        settings.autonomous.stale_monitor_runs_before_completion = 3
        result = build_autonomous_instruction(settings)
        assert "---AWAITING APPROVAL---" in result
        assert "---AUTONOMOUS COMPLETE---" in result
        assert "SUBJECT SELECTION" in result
        assert "PLAN DRAFTING" in result
        assert "APPROVAL GATE" in result
        assert "EXECUTION" in result
        assert "CLOSURE" in result
        assert "Stale monitor completion" in result
        assert "3 or more consecutive cycles" in result

    def test_custom_markers(self) -> None:
        """Custom marker strings are injected, defaults are absent."""
        settings = MagicMock()
        settings.autonomous.approval_marker = "---CUSTOM APPROVAL---"
        settings.autonomous.completion_marker = "---CUSTOM COMPLETE---"
        settings.autonomous.stale_monitor_runs_before_completion = 5
        result = build_autonomous_instruction(settings)
        assert "---CUSTOM APPROVAL---" in result
        assert "---CUSTOM COMPLETE---" in result
        assert "---AWAITING APPROVAL---" not in result
        assert "5 or more consecutive cycles" in result

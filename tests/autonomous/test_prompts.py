"""Tests for autonomous protocol prompt generation."""

from __future__ import annotations

from unittest.mock import MagicMock

from robotsix_chat.autonomous.prompts import build_autonomous_instruction


class TestBuildAutonomousInstruction:
    """Tests for build_autonomous_instruction()."""

    def test_includes_proposal_marker(self) -> None:
        """Default markers and lifecycle sections are present."""
        settings = MagicMock()
        settings.autonomous.proposal_marker = "---PROPOSAL READY---"
        settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
        settings.autonomous.stale_monitor_runs_before_completion = 3
        result = build_autonomous_instruction(settings)
        assert "---PROPOSAL READY---" in result
        assert "---AUTONOMOUS COMPLETE---" in result
        assert "PLANNING" in result
        assert "PROPOSAL" in result
        assert "EXECUTION" in result
        assert "COMPLETION" in result
        assert "Stale monitor completion" in result
        assert "Stall guard response" in result
        assert "3 or more consecutive cycles" in result
        assert "MUTATION AUTHORIZATION" in result
        assert "read-only work" in result
        assert "CONSENT SCOPING" in result
        assert "CONDITIONAL AUTHORIZATION" in result
        assert "HUMAN_ISSUE_APPROVAL" in result
        assert "human_issue_approval" in result
        assert "gate-specific" in result

    def test_custom_markers(self) -> None:
        """Custom marker strings are injected, defaults are absent."""
        settings = MagicMock()
        settings.autonomous.proposal_marker = "---CUSTOM PROPOSAL---"
        settings.autonomous.completion_marker = "---CUSTOM COMPLETE---"
        settings.autonomous.stale_monitor_runs_before_completion = 5
        result = build_autonomous_instruction(settings)
        assert "---CUSTOM PROPOSAL---" in result
        assert "---CUSTOM COMPLETE---" in result
        assert "---PROPOSAL READY---" not in result
        assert "5 or more consecutive cycles" in result

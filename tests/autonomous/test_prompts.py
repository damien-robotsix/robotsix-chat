"""Tests for autonomous protocol prompt generation."""

from __future__ import annotations

from unittest.mock import MagicMock

from robotsix_chat.autonomous.prompts import build_autonomous_instruction


class TestBuildAutonomousInstruction:
    """Tests for build_autonomous_instruction()."""

    def _make_settings(
        self,
        proposal_marker: str = "---PROPOSAL READY---",
        completion_marker: str = "---AUTONOMOUS COMPLETE---",
        stale_threshold: int = 3,
        auto_approve: bool = False,
        allowlist: list[str] | None = None,
        suppress_no_change: bool = False,
    ) -> MagicMock:
        """Build a mock Settings with the given autonomy parameters."""
        settings = MagicMock()
        settings.autonomous.proposal_marker = proposal_marker
        settings.autonomous.completion_marker = completion_marker
        settings.autonomous.stale_monitor_runs_before_completion = stale_threshold
        settings.autonomy.auto_approve_self_authored = auto_approve
        settings.autonomy.auto_approve_repo_allowlist = allowlist or []
        settings.autonomy.suppress_no_change_monitors = suppress_no_change
        return settings

    def test_includes_proposal_marker(self) -> None:
        """Default markers and lifecycle sections are present."""
        settings = self._make_settings()
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
        settings = self._make_settings(
            proposal_marker="---CUSTOM PROPOSAL---",
            completion_marker="---CUSTOM COMPLETE---",
            stale_threshold=5,
        )
        result = build_autonomous_instruction(settings)
        assert "---CUSTOM PROPOSAL---" in result
        assert "---CUSTOM COMPLETE---" in result
        assert "---PROPOSAL READY---" not in result
        assert "5 or more consecutive cycles" in result

    def test_autonomy_tier_section_present(self) -> None:
        """AUTONOMY TIER section is always present, with tier status."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "AUTONOMY TIER" in result
        assert "auto_approve_self_authored=OFF" in result
        assert "allowlist=[(none)]" in result
        assert "suppress_no_change_monitors=OFF" in result
        assert "non-negotiable" in result

    def test_autonomy_tier_auto_approve_enabled(self) -> None:
        """When auto_approve is ON, the tier line and rules reflect it."""
        settings = self._make_settings(
            auto_approve=True,
            allowlist=["robotsix-chat"],
        )
        result = build_autonomous_instruction(settings)
        assert "auto_approve_self_authored=ON" in result
        assert "allowlist=[robotsix-chat]" in result
        assert "suppress_no_change_monitors=OFF" in result

    def test_autonomy_tier_suppress_enabled(self) -> None:
        """When suppress_no_change_monitors is ON, suppression rules appear."""
        settings = self._make_settings(suppress_no_change=True)
        result = build_autonomous_instruction(settings)
        assert "suppress_no_change_monitors=ON" in result
        assert "MONITOR OUTCOME SUPPRESSION" in result

    def test_autonomy_tier_both_enabled(self) -> None:
        """Both auto_approve and suppress can be ON together."""
        settings = self._make_settings(
            auto_approve=True,
            allowlist=["robotsix-chat", "robotsix-mill"],
            suppress_no_change=True,
        )
        result = build_autonomous_instruction(settings)
        assert "auto_approve_self_authored=ON" in result
        assert "allowlist=[robotsix-chat, robotsix-mill]" in result
        assert "suppress_no_change_monitors=ON" in result
        assert "AUTO-APPROVAL RULES" in result
        assert "MONITOR OUTCOME SUPPRESSION" in result

    def test_human_issue_approval_references_autonomy_tier(self) -> None:
        """HUMAN_ISSUE_APPROVAL section references the AUTONOMY TIER rules."""
        settings = self._make_settings(auto_approve=True, allowlist=["r"])
        result = build_autonomous_instruction(settings)
        assert "AUTONOMY TIER rules" in result

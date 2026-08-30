"""Tests for autonomous protocol prompt generation."""

from __future__ import annotations

from unittest.mock import MagicMock

from robotsix_chat.autonomous.prompts import build_autonomous_instruction


class TestBuildAutonomousInstruction:
    """Tests for build_autonomous_instruction()."""

    def _make_settings(
        self,
        completion_marker: str = "---AUTONOMOUS COMPLETE---",
        stale_threshold: int = 3,
        queue_tolerance: int = 3,
    ) -> MagicMock:
        """Build a mock Settings with the given parameters."""
        settings = MagicMock()
        settings.autonomous.completion_marker = completion_marker
        settings.autonomous.stale_monitor_runs_before_completion = stale_threshold
        settings.autonomous.queue_tolerance_runs_before_escalation = queue_tolerance
        return settings

    def test_includes_lifecycle_sections(self) -> None:
        """Default completion marker and lifecycle sections are present."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "---AUTONOMOUS COMPLETE---" in result
        assert "SUBJECT SELECTION" in result
        assert "NO PLANNING PROSE" in result
        assert "STATE VERIFICATION" in result
        assert "EXECUTION" in result
        assert "COMPLETION" in result
        assert "Stale monitor completion" in result
        assert "Stall guard response" in result
        assert "SERIAL-BOARD QUEUE TOLERANCE" in result
        assert "3 consecutive NO_CHANGE cycles as queue wait" in result
        assert "3 or more consecutive cycles" in result
        assert "HUMAN-REVIEW PAUSE COMPLIANCE" in result
        assert "human_mr_approval" in result
        assert "merge detection" in result
        assert "MUTATION AUTHORIZATION" in result
        assert "read-only work" in result
        assert "CONSENT SCOPING" in result
        assert "CONDITIONAL AUTHORIZATION" in result
        assert "HUMAN_ISSUE_APPROVAL" in result
        assert "human_issue_approval" in result
        assert "gate-specific" in result

    def test_proposal_consent_propagation_is_present(self) -> None:
        """The PROPOSAL CONSENT PROPAGATION section is present.

        The old proposal handshake markers are gone.
        """
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "PROPOSAL CONSENT PROPAGATION" in result
        assert "---PROPOSAL READY---" not in result
        assert "---PROPOSAL SENT---" not in result

    def test_no_planning_prose(self) -> None:
        """The protocol forbids plan prose: first tool call, not a plan."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "PLAN DRAFTING" not in result
        assert "draft a step-by-step plan" not in result
        assert "do NOT draft a plan" in result
        assert "make the first actionable" in result
        assert "release gate" in result

    def test_custom_completion_marker(self) -> None:
        """Custom completion marker is injected, the default is absent."""
        settings = self._make_settings(
            completion_marker="---CUSTOM COMPLETE---",
            stale_threshold=5,
        )
        result = build_autonomous_instruction(settings)
        assert "---CUSTOM COMPLETE---" in result
        assert "---AUTONOMOUS COMPLETE---" not in result
        assert "5 or more consecutive cycles" in result

    def test_custom_queue_tolerance(self) -> None:
        """Custom queue tolerance is injected into the serial-board guidance."""
        settings = self._make_settings(queue_tolerance=7)
        result = build_autonomous_instruction(settings)
        assert "7 consecutive NO_CHANGE cycles as queue wait" in result
        assert "3 consecutive NO_CHANGE cycles as queue wait" not in result

    def test_standing_autonomy_policy_present(self) -> None:
        """Standing autonomy policy section is present."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "STANDING AUTONOMY POLICY" in result
        assert "act autonomously for anything safe" in result
        assert "non-negotiable hard gates" in result

    def test_autonomy_tier_standing_policy(self) -> None:
        """Standing autonomy policy is present without config toggles."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "STANDING AUTONOMY POLICY" in result
        assert "act autonomously for anything safe" in result
        assert "non-negotiable hard gates" in result

    def test_human_issue_approval_references_autonomy_tier(self) -> None:
        """HUMAN_ISSUE_APPROVAL section references the AUTONOMY TIER rules."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "AUTONOMY TIER rules" in result

    def test_human_review_pause_compliance_rule_present(self) -> None:
        """HUMAN-REVIEW PAUSE COMPLIANCE rule is present in the prompt."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "HUMAN-REVIEW PAUSE COMPLIANCE" in result
        assert "human_mr_approval" in result
        assert "human_issue_approval" in result
        assert "pause the monitor" in result

    def test_secret_scan_escalation_present(self) -> None:
        """SECRET-SCAN ESCALATION directs auto-filing a rotation ticket."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "SECRET-SCAN ESCALATION" in result
        assert "credential-rotation workflow" in result
        assert "rotate credentials" in result
        assert "close vs restore" in result

    def test_operator_review_escalation_present(self) -> None:
        """OPERATOR REVIEW ESCALATION section is present."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "OPERATOR REVIEW ESCALATION" in result
        assert "48 hours" in result

    # -- config-restart guidance (spec: config-changes-requiring-restart) ----

    def test_operator_config_guidance_no_false_restart_claim(self) -> None:
        """False claim 'no server restart is needed' is removed from guidance."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "OPERATOR CONFIGURATION GUIDANCE" in result
        assert "no server restart is needed" not in result
        assert "no server restart" not in result

    def test_operator_config_guidance_truthful_restart_warning(self) -> None:
        """Guidance warns that autonomous.sessions changes need a restart."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "read only at server startup" in result
        assert "do NOT take effect until the chat service restarts" in result
        assert "CONFIG-APPLY-AND-VERIFY" in result

    def test_config_apply_and_verify_section_present(self) -> None:
        """CONFIG-APPLY-AND-VERIFY section exists with full protocol."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "CONFIG-APPLY-AND-VERIFY" in result
        assert "schedule_continuation" in result
        assert "GET /autonomous/definitions" in result

    def test_auto_self_restart_covers_session_definitions(self) -> None:
        """AUTO SELF-RESTART lists autonomous.sessions changes as valid reason."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "autonomous.sessions changes ARE a valid reason" in result

    def test_delegated_decision_non_duplication_present(self) -> None:
        """DELEGATED DECISION NON-DUPLICATION section is present."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "DELEGATED DECISION NON-DUPLICATION" in result
        assert "once the operator has delegated" in result
        assert "genuinely new delta" in result
        assert "Do NOT re-ask the operator for the same decision" in result
        assert "emit NO_CHANGE" in result

    def test_config_apply_verify_mandates_completion_gate(self) -> None:
        """Task is NOT complete until verification confirms the definition."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "NOT complete until GET /autonomous/definitions" in result
        assert "half-finished task" in result

    def test_state_reporting_accuracy_section_present(self) -> None:
        """STATE REPORTING ACCURACY section prevents false completion claims."""
        settings = self._make_settings()
        result = build_autonomous_instruction(settings)
        assert "STATE REPORTING ACCURACY" in result
        assert "NEVER claim a fix has 'landed'" in result
        assert "PR API verification" in result
        assert "Deploy health verification" in result
        assert "Never infer progress from ticket state alone" in result
        assert "no PR was produced" in result
        assert "deploy access not configured" in result

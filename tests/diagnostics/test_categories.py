"""Tests for the diagnostics categorisation engine.

Covers :class:`FailureCategory`, :class:`Categorizer`, :func:`categorize_record`,
and :func:`recategorize_blocked_event` (agent tool).
"""

# ruff: noqa: D102 — class-based test methods: docstrings on the class
# itself are the primary documentation; per-method docstrings on 50+
# parameterless assertions add noise without value.

from __future__ import annotations

import pytest

from robotsix_chat.diagnostics import FailureCategory, categorize_record
from robotsix_chat.diagnostics.models import DiagnosticRecord

# ---------------------------------------------------------------------------
# FailureCategory enum
# ---------------------------------------------------------------------------


class TestFailureCategory:
    """Tests for the FailureCategory enum."""

    def test_all_five_categories_exist(self) -> None:
        """The enum has exactly the five expected members."""
        assert len(FailureCategory) == 5
        assert FailureCategory.CLONE_TARGET.value == "CLONE_TARGET"
        assert FailureCategory.CI_FAILURE.value == "CI_FAILURE"
        assert FailureCategory.DEPENDENCY.value == "DEPENDENCY"
        assert FailureCategory.REFINEMENT.value == "REFINEMENT"
        assert FailureCategory.OTHER.value == "OTHER"

    def test_is_string_enum(self) -> None:
        """Each member is a str subclass so comparison is natural."""
        assert FailureCategory.CLONE_TARGET == "CLONE_TARGET"
        assert isinstance(FailureCategory.CLONE_TARGET, str)

    def test_lookup_by_value(self) -> None:
        """Values can be looked up via FailureCategory(value)."""
        assert FailureCategory("CLONE_TARGET") is FailureCategory.CLONE_TARGET
        assert FailureCategory("OTHER") is FailureCategory.OTHER

    def test_lookup_invalid_raises(self) -> None:
        """Looking up an unknown value raises ValueError."""
        with pytest.raises(ValueError):
            FailureCategory("BOGUS")


# ---------------------------------------------------------------------------
# DiagnosticRecord model
# ---------------------------------------------------------------------------


class TestDiagnosticRecord:
    """Tests for the DiagnosticRecord dataclass."""

    def test_default_category_is_other(self) -> None:
        """A fresh record defaults to category OTHER."""
        record = DiagnosticRecord(ticket_id="T1", block_reason="some reason")
        assert record.category == "OTHER"

    def test_effective_category_returns_auto_when_no_override(self) -> None:
        """When category_override is None, effective_category == category."""
        record = DiagnosticRecord(
            ticket_id="T1", block_reason="refinement failed", category="REFINEMENT"
        )
        assert record.effective_category == "REFINEMENT"

    def test_effective_category_returns_override_when_set(self) -> None:
        """category_override takes precedence over auto-assigned category."""
        record = DiagnosticRecord(
            ticket_id="T1",
            block_reason="ci failure",
            category="CI_FAILURE",
            category_override="DEPENDENCY",
        )
        assert record.effective_category == "DEPENDENCY"

    def test_extra_field_stores_arbitrary_data(self) -> None:
        """The extra dict can hold supplemental diagnostic fields."""
        record = DiagnosticRecord(
            ticket_id="T1",
            block_reason="clone error",
            extra={"repo": "robotsix-chat", "branch": "main"},
        )
        assert record.extra["repo"] == "robotsix-chat"
        assert record.extra["branch"] == "main"


# ---------------------------------------------------------------------------
# Categorizer — each category
# ---------------------------------------------------------------------------


class TestCategorizerCloneTarget:
    """CLONE_TARGET category matching."""

    def test_clone_keyword(self) -> None:
        record = DiagnosticRecord(ticket_id="T1", block_reason="failed to clone repo")
        assert categorize_record(record) == "CLONE_TARGET"
        assert record.category == "CLONE_TARGET"

    def test_clone_target_keyword(self) -> None:
        record = DiagnosticRecord(ticket_id="T2", block_reason="clone-target not found")
        assert categorize_record(record) == "CLONE_TARGET"

    def test_repo_mapping(self) -> None:
        record = DiagnosticRecord(ticket_id="T3", block_reason="repo mapping is broken")
        assert categorize_record(record) == "CLONE_TARGET"

    def test_repo_registration(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T4", block_reason="repo registration failed"
        )
        assert categorize_record(record) == "CLONE_TARGET"

    def test_target_repo(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T5", block_reason="target repo not configured"
        )
        assert categorize_record(record) == "CLONE_TARGET"

    def test_missing_repo(self) -> None:
        record = DiagnosticRecord(ticket_id="T6", block_reason="missing repository")
        assert categorize_record(record) == "CLONE_TARGET"


class TestCategorizerCiFailure:
    """CI_FAILURE category matching."""

    def test_ci_keyword(self) -> None:
        record = DiagnosticRecord(ticket_id="T1", block_reason="CI is red")
        assert categorize_record(record) == "CI_FAILURE"

    def test_branch_failure(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T2", block_reason="branch protection rule failure"
        )
        assert categorize_record(record) == "CI_FAILURE"

    def test_pull_request(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T3", block_reason="pull request cannot be merged"
        )
        assert categorize_record(record) == "CI_FAILURE"

    def test_pr_keyword(self) -> None:
        record = DiagnosticRecord(ticket_id="T4", block_reason="PR checks failing")
        assert categorize_record(record) == "CI_FAILURE"

    def test_merge_conflict(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T5", block_reason="merge conflict on branch"
        )
        assert categorize_record(record) == "CI_FAILURE"

    def test_build_failed(self) -> None:
        record = DiagnosticRecord(ticket_id="T6", block_reason="build failed in CI")
        assert categorize_record(record) == "CI_FAILURE"  # CI matches first

    def test_workflow_failed(self) -> None:
        record = DiagnosticRecord(ticket_id="T7", block_reason="workflow failed")
        assert categorize_record(record) == "CI_FAILURE"

    def test_test_failure(self) -> None:
        record = DiagnosticRecord(ticket_id="T8", block_reason="test suite failed")
        assert categorize_record(record) == "CI_FAILURE"

    def test_pytest_fail(self) -> None:
        record = DiagnosticRecord(ticket_id="T9", block_reason="pytest fails on main")
        assert categorize_record(record) == "CI_FAILURE"


class TestCategorizerDependency:
    """DEPENDENCY category matching."""

    def test_blocked_on_ticket(self) -> None:
        record = DiagnosticRecord(ticket_id="T1", block_reason="blocked on ticket #42")
        assert categorize_record(record) == "DEPENDENCY"

    def test_blocked_by_ticket(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T2", block_reason="blocked by ticket ABC123"
        )
        assert categorize_record(record) == "DEPENDENCY"

    def test_dependency(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T3", block_reason="dependency not satisfied"
        )
        assert categorize_record(record) == "DEPENDENCY"

    def test_depends_on(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T4", block_reason="depends on another ticket"
        )
        assert categorize_record(record) == "DEPENDENCY"

    def test_prerequisite(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T5", block_reason="prerequisite ticket not done"
        )
        assert categorize_record(record) == "DEPENDENCY"

    def test_missing_sha(self) -> None:
        record = DiagnosticRecord(ticket_id="T6", block_reason="missing SHA in config")
        assert categorize_record(record) == "DEPENDENCY"

    def test_missing_commit(self) -> None:
        record = DiagnosticRecord(ticket_id="T7", block_reason="missing commit hash")
        assert categorize_record(record) == "DEPENDENCY"

    def test_waiting_for_ticket(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T8", block_reason="waiting for ticket to complete"
        )
        assert categorize_record(record) == "DEPENDENCY"


class TestCategorizerRefinement:
    """REFINEMENT category matching."""

    def test_refinement_loop_failed(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T1", block_reason="refine loop failed after 3 attempts"
        )
        assert categorize_record(record) == "REFINEMENT"

    def test_refinement_stuck(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T2", block_reason="refinement stuck on ambiguous spec"
        )
        assert categorize_record(record) == "REFINEMENT"

    def test_implement_loop_failed(self) -> None:
        record = DiagnosticRecord(ticket_id="T3", block_reason="implement loop failed")
        assert categorize_record(record) == "REFINEMENT"

    def test_implement_stuck(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T4", block_reason="implement stuck on test failure"
        )
        assert categorize_record(record) == "REFINEMENT"

    def test_refinement_keyword(self) -> None:
        record = DiagnosticRecord(ticket_id="T5", block_reason="refinement error")
        assert categorize_record(record) == "REFINEMENT"

    def test_refine_failed(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T6", block_reason="refine failed due to missing context"
        )
        assert categorize_record(record) == "REFINEMENT"


class TestCategorizerOther:
    """OTHER (fallback) category."""

    def test_no_match_falls_back_to_other(self) -> None:
        record = DiagnosticRecord(
            ticket_id="T1", block_reason="something completely unexpected"
        )
        assert categorize_record(record) == "OTHER"
        assert record.category == "OTHER"

    def test_empty_reason(self) -> None:
        record = DiagnosticRecord(ticket_id="T2", block_reason="")
        assert categorize_record(record) == "OTHER"

    def test_irrelevant_text(self) -> None:
        record = DiagnosticRecord(ticket_id="T3", block_reason="the sky is blue")
        assert categorize_record(record) == "OTHER"


# ---------------------------------------------------------------------------
# Determinism & reproducibility
# ---------------------------------------------------------------------------


class TestCategorizerDeterminism:
    """Same input always produces same category."""

    def test_repeated_call_same_result(self) -> None:
        record = DiagnosticRecord(ticket_id="T1", block_reason="CI build failed")
        assert categorize_record(record) == "CI_FAILURE"
        # Second call on the same record (category already set) still
        # returns the same result.
        assert categorize_record(record) == "CI_FAILURE"

    def test_different_records_same_reason(self) -> None:
        """Two records with the same reason get the same category."""
        r1 = DiagnosticRecord(ticket_id="T1", block_reason="repo mapping error")
        r2 = DiagnosticRecord(ticket_id="T2", block_reason="repo mapping error")
        assert categorize_record(r1) == categorize_record(r2)

    def test_order_independent(self) -> None:
        """Categorising records in any order yields the same result per record."""
        records = [
            DiagnosticRecord(ticket_id="T1", block_reason="CI failed"),
            DiagnosticRecord(ticket_id="T2", block_reason="blocked on ticket #5"),
            DiagnosticRecord(ticket_id="T3", block_reason="clone error"),
        ]
        categories_forward = [categorize_record(r) for r in records]
        categories_reverse = [categorize_record(r) for r in reversed(records)]
        assert categories_forward == list(reversed(categories_reverse))


# ---------------------------------------------------------------------------
# Case insensitivity
# ---------------------------------------------------------------------------


class TestCategorizerCaseInsensitive:
    """Patterns match regardless of case."""

    def test_uppercase(self) -> None:
        record = DiagnosticRecord(ticket_id="T1", block_reason="CI FAILED")
        assert categorize_record(record) == "CI_FAILURE"

    def test_mixed_case(self) -> None:
        record = DiagnosticRecord(ticket_id="T2", block_reason="Clone Error")
        assert categorize_record(record) == "CLONE_TARGET"

    def test_title_case(self) -> None:
        record = DiagnosticRecord(ticket_id="T3", block_reason="Blocked On Ticket")
        assert categorize_record(record) == "DEPENDENCY"


# ---------------------------------------------------------------------------
# Rule ordering — first match wins
# ---------------------------------------------------------------------------


class TestCategorizerRuleOrdering:
    """When multiple patterns could match, the first one wins."""

    def test_ci_before_other(self) -> None:
        """A reason mentioning CI with other words should still match CI."""
        record = DiagnosticRecord(
            ticket_id="T1",
            block_reason="CI build failed and something unexpected happened",
        )
        assert categorize_record(record) == "CI_FAILURE"

    def test_dependency_before_ci(self) -> None:
        """'blocked on' takes precedence over incidental 'test' mentions."""
        record = DiagnosticRecord(
            ticket_id="T2",
            block_reason="blocked on ticket #5 — CI tests were failing",
        )
        # DEPENDENCY rules are checked before CI_FAILURE rules.
        assert categorize_record(record) == "DEPENDENCY"


# ---------------------------------------------------------------------------
# Override behaviour
# ---------------------------------------------------------------------------


class TestCategorizerOverride:
    """Manual override via category_override."""

    def test_override_present_still_categorises_auto(self) -> None:
        """Even with an override set, auto-categorisation still writes category."""
        record = DiagnosticRecord(
            ticket_id="T1",
            block_reason="CI failed",
            category_override="OTHER",
        )
        result = categorize_record(record)
        assert result == "CI_FAILURE"
        assert record.category == "CI_FAILURE"
        # But effective_category still respects the override.
        assert record.effective_category == "OTHER"

    def test_override_takes_precedence_in_effective(self) -> None:
        """effective_category returns override over auto."""
        record = DiagnosticRecord(
            ticket_id="T1",
            block_reason="blocked on ticket",
            category_override="CI_FAILURE",
        )
        assert record.effective_category == "CI_FAILURE"


# ---------------------------------------------------------------------------
# recategorize_blocked_event agent tool
# ---------------------------------------------------------------------------


class TestRecategorizeBlockedEvent:
    """Tests for the manual override agent tool."""

    def test_invalid_category_returns_error(self) -> None:
        """Passing an unknown category returns an error string."""
        from robotsix_chat.diagnostics import recategorize_blocked_event

        result = recategorize_blocked_event("T123", "BOGUS")
        assert isinstance(result, str)
        assert "not a valid failure category" in result.lower()

    def test_store_not_wired_returns_error(self) -> None:
        """Until the diagnostic store is wired, the tool returns an error."""
        from robotsix_chat.diagnostics import recategorize_blocked_event

        result = recategorize_blocked_event("T123", "CI_FAILURE")
        assert isinstance(result, str)
        assert "store is not yet available" in result

    def test_all_valid_categories_accepted(self) -> None:
        """Every FailureCategory value passes validation (store wired or not)."""
        from robotsix_chat.diagnostics import recategorize_blocked_event

        for cat in FailureCategory:
            result = recategorize_blocked_event("T123", cat.value)
            # Validation passes → store-not-wired error, not invalid-category error.
            assert "not a valid failure category" not in result.lower()

    def test_lowercase_input_accepted(self) -> None:
        """Lowercase category names are normalised."""
        from robotsix_chat.diagnostics import recategorize_blocked_event

        result = recategorize_blocked_event("T123", "clone_target")
        assert "not a valid failure category" not in result.lower()
        assert "store is not yet available" in result

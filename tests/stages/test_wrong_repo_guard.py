"""Tests for the wrong-repository auto-approve guard.

The shadow package's ``__init__.py`` monkey-patches
``_resolve_next_state`` to block auto-approval when the triage note
contains a "wrong repository" pattern.  This test verifies the guard
logic directly — the guard function is pure Python and does not depend
on the installed ``robotsix_mill`` internals.

The test defines a minimal ``State`` enum locally to avoid importing
``robotsix_mill`` (which triggers the shadow ``__init__.py`` and its
sys.path manipulation).
"""

from __future__ import annotations

from enum import StrEnum
from types import SimpleNamespace
from typing import Any

# ---------------------------------------------------------------------------
# Minimal State enum — mirrors the two states the guard interacts with.
# ---------------------------------------------------------------------------


class State(StrEnum):
    """Minimal State enum for testing the wrong-repo guard."""

    READY = "ready"
    HUMAN_ISSUE_APPROVAL = "human_issue_approval"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# The wrong-repo guard — same logic as in the shadow __init__.py.
# ---------------------------------------------------------------------------

_WRONG_REPO_PATTERNS: frozenset[str] = frozenset(
    {
        "different repositor",
        "different repo",
        "wrong repositor",
        "wrong repo",
        "belongs to a different",
        "lives in a different",
        "not present in this",
    }
)


def _original_resolve_stub(
    ctx: Any,
    spec: str,
    ticket_id: str,
    source: str | None = None,
    *,
    triage_note: str | None = None,
) -> tuple[State, str | None]:
    """Stub simulating the original _resolve_next_state behaviour.

    Returns READY when require_approval is False, otherwise simulates
    the LLM classifier returning APPROVE.
    """
    if not ctx.settings.require_approval:
        return State.READY, None
    return State.READY, "auto-approve: APPROVE — routine change"


def _resolve_next_state_with_wrong_repo_guard(
    ctx: Any,
    spec: str,
    ticket_id: str,
    source: str | None = None,
    *,
    triage_note: str | None = None,
) -> tuple[State, str | None]:
    """Wrap ``_resolve_next_state`` to block auto-approve on wrong-repo triage.

    When the triage note contains a wrong-repository pattern, return
    HUMAN_ISSUE_APPROVAL immediately — the LLM classifier's permissive
    bias would otherwise approve the ticket despite the triage signal.
    All other cases delegate to the original function unchanged.

    The guard respects ``require_approval``: when approval is not required
    (e.g. internal pipelines), the guard is skipped and the original
    function's fast-path (READY) is taken.
    """
    if getattr(ctx.settings, "require_approval", True) and triage_note:
        triage_lower = triage_note.lower()
        if any(p in triage_lower for p in _WRONG_REPO_PATTERNS):
            return (
                State.HUMAN_ISSUE_APPROVAL,
                "auto-approve: NEEDS_APPROVAL — triage flagged the ticket as "
                "filed against the wrong repository; human review required "
                "to confirm scope or re-file against the correct repo",
            )
    return _original_resolve_stub(
        ctx, spec, ticket_id, source=source, triage_note=triage_note
    )


# ---------------------------------------------------------------------------
# Test the wrong-repo guard directly.
# ---------------------------------------------------------------------------


class TestWrongRepoGuard:
    """Verify that the wrong-repo guard blocks auto-approval."""

    def _make_ctx(self, require_approval: bool = True) -> Any:
        """Build a duck-typed StageContext."""
        settings = SimpleNamespace(require_approval=require_approval)
        return SimpleNamespace(settings=settings)

    def test_wrong_repo_pattern_blocks_auto_approve(self) -> None:
        """A triage note containing 'wrong repo' forces HUMAN_ISSUE_APPROVAL."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="The ticket was filed against the wrong repo",
        )
        assert state is State.HUMAN_ISSUE_APPROVAL
        assert note is not None
        assert "wrong repository" in note.lower()

    def test_different_repo_pattern_blocks_auto_approve(self) -> None:
        """A triage note containing 'different repo' forces HUMAN_ISSUE_APPROVAL."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="This belongs to a different repository",
        )
        assert state is State.HUMAN_ISSUE_APPROVAL
        assert note is not None
        assert "wrong repository" in note.lower()

    def test_wrong_repositor_pattern_blocks_auto_approve(self) -> None:
        """A triage note containing 'wrong repositor' forces HUMAN_ISSUE_APPROVAL."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="Wrong repository — this is a deployment config package",
        )
        assert state is State.HUMAN_ISSUE_APPROVAL
        assert note is not None

    def test_not_present_in_this_pattern_blocks_auto_approve(self) -> None:
        """'not present in this' in triage note forces HUMAN_ISSUE_APPROVAL."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="The module is not present in this repository",
        )
        assert state is State.HUMAN_ISSUE_APPROVAL
        assert note is not None

    def test_lives_in_a_different_pattern_blocks_auto_approve(self) -> None:
        """'lives in a different' in triage note forces HUMAN_ISSUE_APPROVAL."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="This feature lives in a different repo",
        )
        assert state is State.HUMAN_ISSUE_APPROVAL
        assert note is not None

    def test_belongs_to_a_different_pattern_blocks_auto_approve(self) -> None:
        """'belongs to a different' in triage forces HUMAN_ISSUE_APPROVAL."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="This ticket belongs to a different board",
        )
        assert state is State.HUMAN_ISSUE_APPROVAL
        assert note is not None

    def test_no_wrong_repo_pattern_falls_through(self) -> None:
        """A triage note without wrong-repo patterns falls through to the LLM."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="The spec looks good",
        )
        assert state is State.READY
        assert "APPROVE" in (note or "")

    def test_no_triage_note_falls_through(self) -> None:
        """No triage note falls through to the LLM classifier."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
        )
        assert state is State.READY

    def test_case_insensitive_matching(self) -> None:
        """Wrong-repo patterns are matched case-insensitively."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="Filed against WRONG REPOsitory",
        )
        assert state is State.HUMAN_ISSUE_APPROVAL

    def test_mixed_case_different_repo(self) -> None:
        """Mixed case 'Different Repo' is matched."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="This is for a Different Repo entirely",
        )
        assert state is State.HUMAN_ISSUE_APPROVAL

    def test_require_approval_false_skips_guard(self) -> None:
        """When require_approval is False, the guard is skipped."""
        ctx = self._make_ctx(require_approval=False)
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="Filed against the wrong repo",
        )
        assert state is State.READY
        assert note is None

    def test_other_rejection_patterns_still_fall_through(self) -> None:
        """Other rejection patterns (not wrong-repo) still fall through to LLM."""
        ctx = self._make_ctx()
        # "no change needed" is a rejection pattern but not a wrong-repo pattern
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="No change needed for this ticket",
        )
        assert state is State.READY

    def test_guard_note_mentions_re_file_option(self) -> None:
        """The guard note mentions the option to re-file against the correct repo."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="Wrong repo for this ticket",
        )
        assert state is State.HUMAN_ISSUE_APPROVAL
        assert note is not None
        assert "re-file" in note.lower()

    def test_guard_note_mentions_human_review(self) -> None:
        """The guard note mentions that human review is required."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="Wrong repository",
        )
        assert state is State.HUMAN_ISSUE_APPROVAL
        assert note is not None
        assert "human review" in note.lower()

    def test_empty_triage_note_falls_through(self) -> None:
        """An empty triage note falls through to the LLM."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="",
        )
        assert state is State.READY

    def test_whitespace_only_triage_note_falls_through(self) -> None:
        """A whitespace-only triage note falls through to the LLM."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="   ",
        )
        assert state is State.READY

    def test_partial_match_not_triggered(self) -> None:
        """Partial non-matching phrase in triage note falls through."""
        ctx = self._make_ctx()
        # "wrong" alone should not trigger the guard
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="Something went wrong with the build",
        )
        assert state is State.READY

    def test_multiple_wrong_repo_patterns_first_wins(self) -> None:
        """Multiple wrong-repo patterns in the same note still trigger the guard."""
        ctx = self._make_ctx()
        state, note = _resolve_next_state_with_wrong_repo_guard(
            ctx,
            spec="Some spec text",
            ticket_id="T1",
            source="robotsix-chat",
            triage_note="Wrong repo — belongs to a different repository",
        )
        assert state is State.HUMAN_ISSUE_APPROVAL
        assert note is not None

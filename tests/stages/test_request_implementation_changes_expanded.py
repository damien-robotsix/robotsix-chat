"""Tests for the expanded request-implementation-changes patch.

The patch lives in the ``robotsix_mill`` shadow package
(``src/robotsix_mill/__init__.py``) and expands
``request_implementation_changes`` to accept ``IMPLEMENT_COMPLETE`` and
``FIXING_CI`` in addition to ``HUMAN_MR_APPROVAL``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_mill = pytest.importorskip("robotsix_mill")

# ``tests/stages/test_document.py`` registers stub ``robotsix_mill.*``
# modules in ``sys.modules`` at import time.  When it is collected first,
# ``importorskip`` returns the stub instead of the real package and these
# tests would exercise fakes.
if not getattr(_mill, "__file__", ""):
    pytest.skip(
        "robotsix_mill resolved to sibling-test stubs, not the real package",
        allow_module_level=True,
    )

from robotsix_mill.core.service._helpers import TransitionError  # noqa: E402
from robotsix_mill.core.service._transition_mixin import (  # noqa: E402
    _TransitionMixin,
)
from robotsix_mill.core.states import TRANSITIONS, State  # noqa: E402

TICKET_ID = "20260825T000000Z-test-abcd"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTicket:
    """Minimal ticket stub for the patched method."""

    def __init__(self, state: State = State.IMPLEMENT_COMPLETE) -> None:
        self.state = state
        self.id = TICKET_ID
        self.updated_at: datetime | None = None


class _FakeWorkspace:
    """Minimal workspace stub."""

    def __init__(self) -> None:
        self.artifacts_dir = MagicMock()
        self.dir = MagicMock()


class _FakeService:
    """A TicketService-like object wired for the patched method."""

    def __init__(self, ticket_state: State = State.IMPLEMENT_COMPLETE) -> None:
        self._ticket = _FakeTicket(ticket_state)
        self._workspace = _FakeWorkspace()
        self.settings = SimpleNamespace(
            max_implement_review_cycles=0,
            implement_max_spawns_per_ticket=0,
        )
        self._on_transition: MagicMock | None = MagicMock()
        self._board_id: str | None = None

    def get(self, ticket_id: str) -> _FakeTicket | None:
        """Return the fake ticket."""
        return self._ticket

    def workspace(self, ticket: object) -> _FakeWorkspace:
        """Return the fake workspace."""
        return self._workspace

    def _board_for(self, ticket_id: str) -> str | None:
        """Return the board id."""
        return self._board_id


def _get_expanded_method():
    """Return the patched request_implementation_changes."""
    return _TransitionMixin.request_implementation_changes


def _make_retry_ctx(ticket):
    """Build a fake retry_on_db_full context manager returning *ticket*."""

    class _FakeRetryCtx:
        def __init__(self, settings, board_id):
            pass

        def __enter__(self):
            session = MagicMock()
            session.query.return_value.filter.return_value.first.return_value = ticket
            return session

        def __exit__(self, *args):
            pass

    return _FakeRetryCtx


# ---------------------------------------------------------------------------
# Tests: TRANSITIONS dict expanded
# ---------------------------------------------------------------------------


class TestTransitionsExpanded:
    """Verify the TRANSITIONS dict was patched to allow READY from new states."""

    def test_implement_complete_allows_ready(self) -> None:
        """IMPLEMENT_COMPLETE → READY is now a legal transition."""
        assert State.READY in TRANSITIONS[State.IMPLEMENT_COMPLETE]

    def test_fixing_ci_allows_ready(self) -> None:
        """FIXING_CI → READY is now a legal transition."""
        assert State.READY in TRANSITIONS[State.FIXING_CI]

    def test_human_mr_approval_still_allows_ready(self) -> None:
        """Backward compat: HUMAN_MR_APPROVAL → READY was already present."""
        assert State.READY in TRANSITIONS[State.HUMAN_MR_APPROVAL]


# ---------------------------------------------------------------------------
# Tests: patch applied
# ---------------------------------------------------------------------------


class TestPatchApplied:
    """Smoke-test that the request_implementation_changes wrapper is active."""

    def test_method_is_wrapper(self) -> None:
        """The shadow package replaces request_implementation_changes."""
        method = _get_expanded_method()
        assert method.__name__ == "_request_implementation_changes_expanded"


# ---------------------------------------------------------------------------
# Tests: accepted source states
# ---------------------------------------------------------------------------


class TestAcceptedStates:
    """Verify the expanded method accepts the new source states."""

    @pytest.mark.parametrize(
        "state",
        [
            State.HUMAN_MR_APPROVAL,
            State.IMPLEMENT_COMPLETE,
            State.FIXING_CI,
        ],
    )
    def test_accepted_state(self, state: State) -> None:
        """All three states should be accepted and transition to READY."""
        svc = _FakeService(ticket_state=state)
        ticket = svc._ticket

        import robotsix_mill.core.service._transition_mixin as _tm

        original_retry = _tm.retry_on_db_full
        _tm.retry_on_db_full = _make_retry_ctx(ticket)
        try:
            method = _get_expanded_method()
            comment, result_ticket = method(svc, TICKET_ID, "please fix the tests")
            assert result_ticket.state is State.READY
            assert comment.body == "please fix the tests"
            assert comment.author == "user"
        finally:
            _tm.retry_on_db_full = original_retry


# ---------------------------------------------------------------------------
# Tests: rejected source states
# ---------------------------------------------------------------------------


class TestRejectedStates:
    """Verify states outside the accepted set are rejected."""

    @pytest.mark.parametrize(
        "state",
        [
            State.DRAFT,
            State.READY,
            State.DONE,
            State.CLOSED,
            State.BLOCKED,
            State.DELIVERABLE,
        ],
    )
    def test_rejected_state(self, state: State) -> None:
        """States not in the accepted set should raise TransitionError."""
        svc = _FakeService(ticket_state=state)
        ticket = svc._ticket

        import robotsix_mill.core.service._transition_mixin as _tm

        original_retry = _tm.retry_on_db_full
        _tm.retry_on_db_full = _make_retry_ctx(ticket)
        try:
            method = _get_expanded_method()
            with pytest.raises(TransitionError, match="not in an accepted state"):
                method(svc, TICKET_ID, "rework needed")
        finally:
            _tm.retry_on_db_full = original_retry


# ---------------------------------------------------------------------------
# Tests: empty body rejection
# ---------------------------------------------------------------------------


class TestEmptyBody:
    """Verify empty body is rejected regardless of state."""

    @pytest.mark.parametrize(
        "body",
        ["", "   ", "\n"],
    )
    def test_empty_body_rejected(self, body: str) -> None:
        """An empty or whitespace-only body is always rejected."""
        svc = _FakeService(ticket_state=State.IMPLEMENT_COMPLETE)
        method = _get_expanded_method()
        with pytest.raises(TransitionError, match="non-empty body is required"):
            method(svc, TICKET_ID, body)


# ---------------------------------------------------------------------------
# Tests: guards cleared
# ---------------------------------------------------------------------------


class TestGuardsCleared:
    """Verify the stale-spec guard and spawn counter are cleared."""

    def test_stale_guard_cleared(self) -> None:
        """_clear_stale_implement_guard is called on success."""
        svc = _FakeService(ticket_state=State.IMPLEMENT_COMPLETE)
        ticket = svc._ticket

        import robotsix_mill.core.service._transition_mixin as _tm

        original_retry = _tm.retry_on_db_full
        original_clear = _tm._clear_stale_implement_guard
        original_reset = _tm._reset_implement_spawn_counter

        clear_called = False
        reset_called = False

        def mock_clear(ws):
            nonlocal clear_called
            clear_called = True

        def mock_reset(ws):
            nonlocal reset_called
            reset_called = True

        _tm.retry_on_db_full = _make_retry_ctx(ticket)
        _tm._clear_stale_implement_guard = mock_clear
        _tm._reset_implement_spawn_counter = mock_reset
        try:
            method = _get_expanded_method()
            method(svc, TICKET_ID, "fix tests")
            assert clear_called, "_clear_stale_implement_guard was not called"
            assert reset_called, "_reset_implement_spawn_counter was not called"
        finally:
            _tm.retry_on_db_full = original_retry
            _tm._clear_stale_implement_guard = original_clear
            _tm._reset_implement_spawn_counter = original_reset

    def test_on_transition_callback(self) -> None:
        """The _on_transition callback fires with the old state."""
        svc = _FakeService(ticket_state=State.FIXING_CI)
        ticket = svc._ticket

        import robotsix_mill.core.service._transition_mixin as _tm

        original_retry = _tm.retry_on_db_full
        _tm.retry_on_db_full = _make_retry_ctx(ticket)
        try:
            method = _get_expanded_method()
            method(svc, TICKET_ID, "fix CI")
            svc._on_transition.assert_called_once()
            call_args = svc._on_transition.call_args
            assert call_args[0][1] == "fixing_ci"  # old_state
        finally:
            _tm.retry_on_db_full = original_retry

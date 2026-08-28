"""Tests for the expanded request-implementation-changes patch.

The patch lives in the ``robotsix_mill`` shadow package
(``src/robotsix_mill/__init__.py``) and expands
``request_implementation_changes`` to accept ``IMPLEMENT_COMPLETE`` and
``FIXING_CI`` in addition to ``HUMAN_MR_APPROVAL``.

Mocking strategy
~~~~~~~~~~~~~~~~
The expanded method performs *local* imports inside its body::

    from robotsix_mill.core.db import retry_on_db_full
    from robotsix_mill.core.service._helpers import (
        TransitionError, _get_ticket, _make_event,
    )

These resolve at call-time through ``sys.modules``, so the correct
patching targets are the *source* modules:

* ``robotsix_mill.core.db.retry_on_db_full`` — root DB retry gesture
* ``robotsix_mill.core.service._helpers._get_ticket`` — ticket look-up
* ``robotsix_mill.core.service._helpers._make_event`` — event factory

The ``_clear_stale_implement_guard`` and ``_reset_implement_spawn_counter``
functions are captured as globals inside the expanded function's
``__globals__`` (which is the shadow ``__init__`` module dict), so we
patch those via ``method.__globals__`` mutation.
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
# tests would exercise fakes (or crash on
# ``robotsix_mill.agents.coding``).  The real package — and the shadow
# ``__init__`` that hands off to it — always carries a real ``__file__``;
# the stubs do not.
if not getattr(_mill, "__file__", ""):
    pytest.skip(
        "robotsix_mill resolved to sibling-test stubs, not the real package",
        allow_module_level=True,
    )

import robotsix_mill.core.db as _db  # noqa: E402
import robotsix_mill.core.service._helpers as _helpers  # noqa: E402
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

    def workspace(self, ticket: object) -> _FakeWorkspace:
        """Return the fake workspace."""
        return self._workspace

    def _board_for(self, ticket_id: str) -> str | None:
        """Return the board id."""
        return self._board_id


class _FakeRetryCtx:
    """Context manager that stands in for ``retry_on_db_full``.

    Yields a ``MagicMock`` session.  ``_get_ticket`` is always mocked
    separately, so the session's query chain is irrelevant.
    """

    def __init__(self, settings, board_id):
        pass

    def __enter__(self):
        self._session = MagicMock()
        return self._session

    def __exit__(self, *args):
        pass


def _get_expanded_method():
    """Return the patched request_implementation_changes."""
    return _TransitionMixin.request_implementation_changes


# ---------------------------------------------------------------------------
# Context manager: patch all mock targets at once
# ---------------------------------------------------------------------------


class _PatchedCtx:
    """Simple context manager that patches all correct mock targets.

    On enter it replaces the *source-module* attributes and the
    function's global names with mocks, and restores originals on exit.
    """

    def __init__(self, ticket: _FakeTicket, make_event_ret=None):
        self._ticket = ticket
        self._make_event_ret = make_event_ret or MagicMock()
        self._method = _get_expanded_method()
        # -- originals to restore --
        self._orig_retry = None
        self._orig_get_ticket = None
        self._orig_make_event = None
        self._orig_clear = None
        self._orig_reset = None
        # -- mocks exposed to tests --
        self.get_ticket = MagicMock(return_value=ticket)
        self.make_event = MagicMock(return_value=self._make_event_ret)
        self.clear_guard = MagicMock()
        self.reset_counter = MagicMock()

    def __enter__(self):
        # Source-module patches (affects local imports inside the function)
        self._orig_retry = _db.retry_on_db_full
        self._orig_get_ticket = _helpers._get_ticket
        self._orig_make_event = _helpers._make_event

        _db.retry_on_db_full = lambda settings, board_id: _FakeRetryCtx(
            settings, board_id
        )
        _helpers._get_ticket = self.get_ticket
        _helpers._make_event = self.make_event

        # Global-name patches (affects the function's __globals__)
        g = self._method.__globals__
        self._orig_clear = g.get("_clear_stale_implement_guard")
        self._orig_reset = g.get("_reset_implement_spawn_counter")

        g["_clear_stale_implement_guard"] = self.clear_guard
        g["_reset_implement_spawn_counter"] = self.reset_counter
        return self

    def __exit__(self, *exc):
        _db.retry_on_db_full = self._orig_retry
        _helpers._get_ticket = self._orig_get_ticket
        _helpers._make_event = self._orig_make_event

        g = self._method.__globals__
        g["_clear_stale_implement_guard"] = self._orig_clear
        g["_reset_implement_spawn_counter"] = self._orig_reset


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

        with _PatchedCtx(ticket):
            method = _get_expanded_method()
            comment, result_ticket = method(svc, TICKET_ID, "please fix the tests")
            assert result_ticket.state is State.READY
            assert comment.body == "please fix the tests"
            assert comment.author == "user"


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

        with _PatchedCtx(ticket):
            from robotsix_mill.core.service._helpers import TransitionError

            method = _get_expanded_method()
            with pytest.raises(TransitionError, match="not in an accepted state"):
                method(svc, TICKET_ID, "rework needed")


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

        from robotsix_mill.core.service._helpers import TransitionError

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

        with _PatchedCtx(ticket) as mocks:
            method = _get_expanded_method()
            method(svc, TICKET_ID, "fix tests")
            mocks.clear_guard.assert_called_once()
            mocks.reset_counter.assert_called_once()

    def test_on_transition_callback(self) -> None:
        """The _on_transition callback fires with the old state."""
        svc = _FakeService(ticket_state=State.FIXING_CI)
        ticket = svc._ticket

        with _PatchedCtx(ticket):
            method = _get_expanded_method()
            method(svc, TICKET_ID, "fix CI")
            svc._on_transition.assert_called_once()
            call_args = svc._on_transition.call_args
            assert call_args[0][1] == "fixing_ci"  # old_state as .value

    def test_on_transition_none_is_safe(self) -> None:
        """When _on_transition is None the method returns without error."""
        svc = _FakeService(ticket_state=State.IMPLEMENT_COMPLETE)
        svc._on_transition = None
        ticket = svc._ticket

        with _PatchedCtx(ticket):
            method = _get_expanded_method()
            comment, result_ticket = method(svc, TICKET_ID, "fix it")
            assert result_ticket.state is State.READY
            assert comment.body == "fix it"

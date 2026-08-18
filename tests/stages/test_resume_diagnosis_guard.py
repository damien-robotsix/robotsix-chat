"""Tests for the resume-blocked diagnosis guard patch.

The patch lives in the ``robotsix_mill`` shadow package
(``src/robotsix_mill/__init__.py``) and only activates when the shadow is
imported with the real ``robotsix_mill`` installed.
"""

from __future__ import annotations

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
from robotsix_mill.core.states import State  # noqa: E402

TICKET_ID = "20260816T000000Z-test-abcd"


class _FakeTicket:
    def __init__(self, state: State = State.BLOCKED) -> None:
        self.state = state
        self.id = TICKET_ID


class _FakeComment:
    def __init__(self, body: str, author: str = "system") -> None:
        self.body = body
        self.author = author

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _FakeComment):
            return self.body == other.body and self.author == other.author
        return NotImplemented


class _FakeEvent:
    def __init__(self, state: State, note: str | None = None) -> None:
        self.state = state
        self.note = note


class _FakeService:
    """A TicketService-like mock that captures add_comment calls."""

    def __init__(
        self,
        ticket_state: State = State.BLOCKED,
        events: list[_FakeEvent] | None = None,
    ) -> None:
        self._ticket = _FakeTicket(ticket_state)
        self._events = events or []
        self.comments: list[_FakeComment] = []

    def get(self, ticket_id: str) -> _FakeTicket | None:
        return self._ticket

    def history(
        self,
        ticket_id: str,
        limit: int | None = None,
        offset: int = 0,
        order: str = "asc",
    ) -> list[_FakeEvent]:
        return list(self._events)

    def add_comment(
        self,
        ticket_id: str,
        body: str,
        author: str = "user",
        parent_id: int | None = None,
    ) -> None:
        self.comments.append(_FakeComment(body, author))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_wrapper():
    """Return the patched resume_blocked method installed by the shadow."""
    return _TransitionMixin.resume_blocked


def _get_diagnosis_re():
    """Return the compiled diagnosis regex from the shadow module."""
    return _get_wrapper().__globals__["_DIAGNOSIS_RE"]


# ---------------------------------------------------------------------------
# Tests: patch applied
# ---------------------------------------------------------------------------


class TestPatchApplied:
    """Smoke-test that the resume_blocked wrapper is active."""

    def test_resume_blocked_is_wrapper(self) -> None:
        """The shadow package replaces resume_blocked with the diagnosis guard."""
        assert (
            _TransitionMixin.resume_blocked.__name__
            == "_resume_blocked_with_diagnosis_guard"
        )


# ---------------------------------------------------------------------------
# Tests: diagnosis regex
# ---------------------------------------------------------------------------


class TestDiagnosisRegex:
    """Verify the diagnosis detection regex matches expected patterns."""

    @pytest.mark.parametrize(
        "note,expected",
        [
            # Non-empty summary tail
            (
                "implement spawn limit reached (3/3)\n\n"
                "Last attempt summary tail:\n[SPAWN ABORT] TimeoutError: upstream",
                True,
            ),
            # Summary tail with actual content (even without ABORT marker)
            (
                "spawn limit reached\n\n"
                "Last attempt summary tail:\nThe agent produced this output",
                True,
            ),
            # [SPAWN ABORT] breadcrumb in any note
            ("blocked: something went wrong\n[SPAWN ABORT] OSError: no space", True),
            # Exception-name: message pattern
            ("OSError: [Errno 28] No space left on device", True),
            ("ValueError: invalid config key 'foo'", True),
            ("blocked: RuntimeError: coordinator crashed", True),
            # Traceback marker
            ("some error\nTraceback (most recent call last):\n  File ...", True),
            # Empty summary tail (just whitespace after header) — no diagnosis
            (
                "implement spawn limit reached (3/3)\n\n"
                "Last attempt summary tail:\n   \n",
                False,
            ),
            # Boilerplate spawn-limit note with no tail
            (
                "implement spawn limit reached (3/3) — "
                "escalating to BLOCKED for human inspection.",
                False,
            ),
            # Empty note
            ("", False),
            # Blank note
            ("   ", False),
            # Generic block with no exception/trace/tail
            ("empty or missing specification — cannot implement without a spec", False),
        ],
    )
    def test_diagnosis_detection(self, note: str, expected: bool) -> None:
        """Verify the diagnosis regex against expected note patterns."""
        assert bool(_get_diagnosis_re().search(note)) is expected


# ---------------------------------------------------------------------------
# Tests: refusal path
# ---------------------------------------------------------------------------


class TestRefusal:
    """When the block event has no diagnosis, resume is refused."""

    def test_spawn_limit_no_tail_refused(self) -> None:
        """Refuse when the block note is spawn-limit boilerplate without a tail."""
        svc = _FakeService(
            events=[
                _FakeEvent(
                    State.BLOCKED,
                    "implement spawn limit reached (3/3) — "
                    "escalating to BLOCKED for human inspection.",
                )
            ],
        )
        wrapper = _get_wrapper()
        with pytest.raises(TransitionError, match="resume refused"):
            wrapper(svc, TICKET_ID)
        assert svc.comments == [
            _FakeComment(
                "[needs-investigation] This blocked ticket has no error summary or "
                "trace in its block event — resuming would re-exhaust the spawn "
                "budget invisibly.  The ticket needs manual investigation to "
                "identify the root cause before resuming.",
                "system",
            )
        ]

    def test_empty_note_refused(self) -> None:
        """Refuse when the block note is an empty string."""
        svc = _FakeService(events=[_FakeEvent(State.BLOCKED, "")])
        wrapper = _get_wrapper()
        with pytest.raises(TransitionError):
            wrapper(svc, TICKET_ID)
        assert len(svc.comments) == 1
        assert svc.comments[0].body.startswith("[needs-investigation]")

    def test_no_block_event_refused(self) -> None:
        """Refuse when no BLOCKED event exists in the history."""
        svc = _FakeService(events=[])
        wrapper = _get_wrapper()
        with pytest.raises(TransitionError):
            wrapper(svc, TICKET_ID)
        assert len(svc.comments) == 1


# ---------------------------------------------------------------------------
# Tests: delegation path
# ---------------------------------------------------------------------------


class TestDelegation:
    """When the block event HAS a diagnosis, the original method is called."""

    def test_diagnosis_delegates(self, monkeypatch) -> None:
        """Delegate when the block note has a non-empty summary tail."""
        svc = _FakeService(
            events=[
                _FakeEvent(
                    State.BLOCKED,
                    "implement spawn limit reached (3/3)\n\n"
                    "Last attempt summary tail:\n[SPAWN ABORT] TimeoutError: up",
                )
            ],
        )
        wrapper = _get_wrapper()
        original_called: list[tuple] = []

        def fake_original(self, ticket_id, note=""):
            original_called.append((ticket_id, note))
            return _FakeTicket(State.READY)

        monkeypatch.setitem(
            wrapper.__globals__, "_original_resume_blocked", fake_original
        )

        result = wrapper(svc, TICKET_ID, note="override note")
        assert len(original_called) == 1
        assert original_called[0] == (TICKET_ID, "override note")
        assert result.state == State.READY
        assert svc.comments == []  # no investigation comment

    def test_spawn_abort_marker_delegates(self, monkeypatch) -> None:
        """Delegate when the block note contains a [SPAWN ABORT] marker."""
        svc = _FakeService(
            events=[
                _FakeEvent(
                    State.BLOCKED,
                    "[SPAWN ABORT] RuntimeError: something broke",
                )
            ],
        )
        wrapper = _get_wrapper()
        original_called: list[tuple] = []

        def fake_original(self, ticket_id, note=""):
            original_called.append((ticket_id, note))
            return _FakeTicket(State.READY)

        monkeypatch.setitem(
            wrapper.__globals__, "_original_resume_blocked", fake_original
        )

        result = wrapper(svc, TICKET_ID)
        assert len(original_called) == 1
        assert result.state == State.READY
        assert svc.comments == []

    def test_traceback_delegates(self, monkeypatch) -> None:
        """Delegate when the block note contains a Python traceback."""
        svc = _FakeService(
            events=[
                _FakeEvent(
                    State.BLOCKED,
                    "Traceback (most recent call last):\n"
                    "  File 'foo.py', line 42, in bar\n"
                    "RuntimeError: boom",
                )
            ],
        )
        wrapper = _get_wrapper()
        original_called: list[tuple] = []

        def fake_original(self, ticket_id, note=""):
            original_called.append((ticket_id, note))
            return _FakeTicket(State.READY)

        monkeypatch.setitem(
            wrapper.__globals__, "_original_resume_blocked", fake_original
        )

        result = wrapper(svc, TICKET_ID)
        assert len(original_called) == 1
        assert result.state == State.READY
        assert svc.comments == []

    def test_non_blocked_ticket_skips_guard(self, monkeypatch) -> None:
        """Retry-attempt tickets (not BLOCKED) are exempt and delegated."""
        svc = _FakeService(ticket_state=State.READY, events=[])
        wrapper = _get_wrapper()
        original_called: list[tuple] = []

        def fake_original(self, ticket_id, note=""):
            original_called.append((ticket_id, note))
            return _FakeTicket(State.READY)

        monkeypatch.setitem(
            wrapper.__globals__, "_original_resume_blocked", fake_original
        )

        result = wrapper(svc, TICKET_ID)
        assert len(original_called) == 1
        assert result.state == State.READY
        assert svc.comments == []

    def test_ticket_not_found_delegates(self, monkeypatch) -> None:
        """None from get() means the original handles it."""
        svc = _FakeService(ticket_state=State.BLOCKED, events=[])
        svc._ticket = None  # type: ignore[assignment]
        wrapper = _get_wrapper()
        original_called: list[tuple] = []

        def fake_original(self, ticket_id, note=""):
            original_called.append((ticket_id, note))
            return _FakeTicket(State.READY)

        monkeypatch.setitem(
            wrapper.__globals__, "_original_resume_blocked", fake_original
        )

        wrapper(svc, TICKET_ID)
        assert len(original_called) == 1
        assert svc.comments == []

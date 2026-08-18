"""Tests for the implement-spawn abort breadcrumb patches.

The patches live in the ``robotsix_mill`` shadow package
(``src/robotsix_mill/__init__.py``) and only activate when the shadow is
imported with the real ``robotsix_mill`` installed (the production
mill-worker setup).  When the mill is absent the whole module skips
cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path

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

import robotsix_mill.agents.coding as _coding  # noqa: E402
from robotsix_mill.agents.coding import AgentRunError  # noqa: E402
from robotsix_mill.core.states import State  # noqa: E402
from robotsix_mill.stages.implement import phase_coordinator  # noqa: E402
from robotsix_mill.stages.implement._shared import (  # noqa: E402
    _ImplementContext,
)
from robotsix_mill.stages.implement.implementation_logic import (  # noqa: E402
    ImplementationLogicMixin,
)

TICKET_ID = "20260816T000000Z-test-abcd"


class _FakeTicket:
    id = TICKET_ID
    board_id = "robotsix-chat"
    kind = None


class _FakeComment:
    def __init__(self, body: str, author: str = "mill") -> None:
        self.body = body
        self.author = author


class _FakeWorkspace:
    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir


class _FakeService:
    def __init__(self, artifacts_dir: Path) -> None:
        self.comments: list[_FakeComment] = []
        self._ws = _FakeWorkspace(artifacts_dir)

    def workspace(self, ticket: object) -> _FakeWorkspace:
        return self._ws

    def add_comment(
        self,
        ticket_id: str,
        body: str,
        author: str = "user",
        parent_id: int | None = None,
    ) -> None:
        self.comments.append(_FakeComment(body, author))

    def list_comments(self, ticket_id: str) -> list[_FakeComment]:
        return list(self.comments)


class _FakeSettings:
    def __init__(self, events_path: Path) -> None:
        self._events_path = events_path

    def diagnostic_events_file_for(self, board_id: str) -> Path:
        return self._events_path


class _FakeCtx:
    def __init__(self, service: _FakeService, settings: _FakeSettings) -> None:
        self.service = service
        self.settings = settings
        self.repo_config = None

    def memory_board_id(self, ticket: object) -> str:
        return "robotsix-chat"


@pytest.fixture
def env(tmp_path: Path):
    service = _FakeService(tmp_path)
    settings = _FakeSettings(tmp_path / "events.jsonl")
    ctx = _FakeCtx(service, settings)
    return ctx, service, _FakeTicket(), tmp_path


class TestPatchesApplied:
    """Smoke-tests that the shadow package's patches are active."""

    def test_invoke_wrapper_applied(self) -> None:
        """The shadow package replaces the spawn invocation with the wrapper."""
        assert (
            ImplementationLogicMixin._invoke_implement_agent.__name__
            == "_invoke_implement_agent_with_abort_breadcrumbs"
        )

    def test_preflight_wrapper_applied(self) -> None:
        """The shadow package replaces the preflight with the wrapper."""
        assert (
            phase_coordinator.PhaseCoordinatorMixin.preflight.__name__
            == "_preflight_with_abort_breadcrumbs"
        )


class TestInvokeWrapper:
    """Tests for the ``_invoke_implement_agent`` abort wrapper."""

    def test_transient_re_raise_records_breadcrumbs(self, env, monkeypatch) -> None:
        """A transient error is recorded and re-raised unchanged."""
        ctx, service, ticket, tmp_path = env
        wrapper = ImplementationLogicMixin._invoke_implement_agent
        ic = _ImplementContext(
            spec="spec",
            memory_text="",
            reference_files=None,
            file_map=None,
            feedback=None,
            previous_attempt_summary=None,
            open_thread_ids=None,
        )

        def raise_transient(**kwargs):
            cause = TimeoutError("upstream timed out")
            raise AgentRunError("transient wrapper failure", [], cause=cause)

        monkeypatch.setattr(_coding, "run_implement_agent", raise_transient)

        with pytest.raises(TimeoutError):
            wrapper(
                ctx,
                ticket,
                tmp_path,
                "branch",
                ctx.settings,
                ic,
                "lang",
                2,
                None,
                None,
                "robotsix-chat",
                service.workspace(ticket),
                "main",
            )

        assert service.comments == [
            _FakeComment(
                "[implement-spawn-abort] TimeoutError: upstream timed out",
                "mill",
            )
        ]
        summary = (tmp_path / "implement_summary.md").read_text()
        assert "[SPAWN ABORT] TimeoutError: upstream timed out" in summary

        events = [
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
        ]
        assert events[0]["category"] == "IMPLEMENT_SPAWN_ABORT"
        assert events[0]["ticket_id"] == TICKET_ID

    def test_unhandled_exception_records_breadcrumbs(self, env, monkeypatch) -> None:
        """An escaping exception is recorded before re-raising."""
        ctx, service, ticket, tmp_path = env
        wrapper = ImplementationLogicMixin._invoke_implement_agent
        ic = _ImplementContext(
            spec="spec",
            memory_text="",
            reference_files=None,
            file_map=None,
            feedback=None,
            previous_attempt_summary=None,
            open_thread_ids=None,
        )

        def raise_runtime(**kwargs):
            raise RuntimeError("coordinator setup failed")

        monkeypatch.setattr(_coding, "run_implement_agent", raise_runtime)

        with pytest.raises(RuntimeError):
            wrapper(
                ctx,
                ticket,
                tmp_path,
                "branch",
                ctx.settings,
                ic,
                "lang",
                2,
                None,
                None,
                "robotsix-chat",
                service.workspace(ticket),
                "main",
            )

        assert service.comments == [
            _FakeComment(
                "[implement-spawn-abort] RuntimeError: coordinator setup failed",
                "mill",
            )
        ]
        assert (tmp_path / "implement_summary.md").read_text() == (
            "\n[SPAWN ABORT] RuntimeError: coordinator setup failed\n"
        )

    def test_success_path_records_nothing(self, env, monkeypatch) -> None:
        """A successful agent run leaves no breadcrumbs."""
        ctx, service, ticket, tmp_path = env
        wrapper = ImplementationLogicMixin._invoke_implement_agent
        ic = _ImplementContext(
            spec="spec",
            memory_text="",
            reference_files=None,
            file_map=None,
            feedback=None,
            previous_attempt_summary=None,
            open_thread_ids=None,
        )

        def succeed(**kwargs):
            return ("summary", ["ref.py"], "memory", None, None, False, "")

        monkeypatch.setattr(_coding, "run_implement_agent", succeed)

        result = wrapper(
            ctx,
            ticket,
            tmp_path,
            "branch",
            ctx.settings,
            ic,
            "lang",
            2,
            None,
            None,
            "robotsix-chat",
            service.workspace(ticket),
            "main",
        )

        assert result.success
        assert not service.comments
        assert not (tmp_path / "implement_summary.md").exists()
        assert not (tmp_path / "events.jsonl").exists()


class TestPreflightWrapper:
    """Tests for the implement-stage preflight abort wrapper."""

    def test_fatal_exception_returns_blocked_outcome(self, env, monkeypatch) -> None:
        """A fatal preflight exception returns a BLOCKED outcome."""
        ctx, service, ticket, _tmp_path = env
        wrapper = phase_coordinator.PhaseCoordinatorMixin.preflight

        # Replace run_preflight_checks with a fatal-bug raise.
        import robotsix_mill.stages.implement.phase_coordinator_preflight as pcp

        def fatal(ticket, ctx):
            raise ValueError("agent definition missing")

        monkeypatch.setattr(pcp, "run_preflight_checks", fatal)

        outcome = wrapper(phase_coordinator.PhaseCoordinatorMixin, ticket, ctx)

        assert outcome is not None
        assert outcome.next_state == State.BLOCKED
        assert "ValueError: agent definition missing" in outcome.note

    def test_transient_exception_reraised_with_breadcrumb(
        self, env, monkeypatch
    ) -> None:
        """A transient exception is re-raised with a deduped breadcrumb."""
        ctx, service, ticket, tmp_path = env
        wrapper = phase_coordinator.PhaseCoordinatorMixin.preflight

        import robotsix_mill.stages.implement.phase_coordinator_preflight as pcp

        def transient(ticket, ctx):
            raise TimeoutError("deploy api unreachable")

        monkeypatch.setattr(pcp, "run_preflight_checks", transient)

        with pytest.raises(TimeoutError):
            wrapper(phase_coordinator.PhaseCoordinatorMixin, ticket, ctx)

        assert service.comments == [
            _FakeComment(
                "[implement-spawn-abort] TimeoutError: deploy api unreachable",
                "mill",
            )
        ]

        # Second identical transient — dedup should prevent a duplicate comment.
        with pytest.raises(TimeoutError):
            wrapper(phase_coordinator.PhaseCoordinatorMixin, ticket, ctx)

        assert len(service.comments) == 1  # deduped

        # Summary must NOT be written by a preflight abort.
        assert not (tmp_path / "implement_summary.md").exists()

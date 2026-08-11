"""Tests for the health scheduler."""

from __future__ import annotations

import asyncio
import logging

import pytest

from robotsix_chat.health.models import CheckSeverity
from robotsix_chat.health.scheduler import HealthScheduler


class TestHealthScheduler:
    """Tests for :class:`HealthScheduler`."""

    def test_initial_state(self) -> None:
        """Scheduler starts with no task and the configured interval."""
        state = _FakeState()
        scheduler = HealthScheduler(interval_seconds=300.0, state=state)
        assert scheduler.interval_seconds == 300.0
        assert scheduler._task is None

    @pytest.mark.asyncio
    async def test_start_stop(self) -> None:
        """start() launches a task; stop() cancels it."""
        state = _FakeState()
        scheduler = HealthScheduler(interval_seconds=0.01, state=state)
        scheduler.start()
        assert scheduler._task is not None
        await asyncio.sleep(0.05)
        await scheduler.stop()
        assert scheduler._task is None

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        """Calling start() twice only creates one task."""
        state = _FakeState()
        scheduler = HealthScheduler(interval_seconds=300.0, state=state)
        scheduler.start()
        task1 = scheduler._task
        scheduler.start()
        assert scheduler._task is task1
        await scheduler.stop()

    def test_run_once_persists_on_state(self) -> None:
        """After run_once(), state.health_status holds the result."""
        state = _FakeState()
        scheduler = HealthScheduler(interval_seconds=300.0, state=state)
        status = asyncio.run(scheduler.run_once())
        assert status is not None
        assert len(status.checks) == 4
        assert state.health_status is status

    def test_run_once_overall_ok_when_all_checks_pass(self) -> None:
        """Overall is OK when every subsystem is healthy."""
        state = _FakeState(
            memory=_FakeMemory({"backend": "cognee", "degraded": False}),
            knowledge_store=_FakeStore(list_result=[]),
            feedback_runner=None,
            diagnostic_store=_FakeStore(list_events_result=[]),
        )
        scheduler = HealthScheduler(interval_seconds=300.0, state=state)
        status = asyncio.run(scheduler.run_once())
        assert status.overall == CheckSeverity.OK

    def test_run_once_error_when_memory_degraded(self) -> None:
        """Overall is ERROR when memory is degraded."""
        state = _FakeState(
            memory=_FakeMemory(
                {"backend": "cognee", "degraded": True, "reason": "fail"}
            ),
        )
        scheduler = HealthScheduler(interval_seconds=300.0, state=state)
        status = asyncio.run(scheduler.run_once())
        assert status.overall == CheckSeverity.ERROR

    def test_run_once_transitions_logged(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """Degradation transitions are logged at WARNING level."""
        caplog.set_level(logging.WARNING)
        state = _FakeState(
            memory=_FakeMemory({"backend": "cognee", "degraded": False}),
            knowledge_store=_FakeStore(list_result=[]),
            feedback_runner=None,
            diagnostic_store=_FakeStore(list_events_result=[]),
        )
        scheduler = HealthScheduler(interval_seconds=300.0, state=state)

        asyncio.run(scheduler.run_once())
        assert "DEGRADED" not in caplog.text

        state.memory = _FakeMemory(
            {"backend": "cognee", "degraded": True, "reason": "fail"}
        )
        asyncio.run(scheduler.run_once())
        assert "DEGRADED → ERROR" in caplog.text

    def test_stop_before_start_is_noop(self) -> None:
        """Calling stop() before start() does not raise."""
        state = _FakeState()
        scheduler = HealthScheduler(interval_seconds=300.0, state=state)
        asyncio.run(scheduler.stop())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeState:
    """Minimal app.state stand-in for scheduler tests."""

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.health_status: object = None


class _FakeMemory:
    """Fake memory backend returning a canned status dict."""

    def __init__(self, status_result: dict | None = None) -> None:
        self._status_result = status_result or {}

    def status(self) -> dict:
        """Return the canned status dict."""
        return self._status_result


class _FakeStore:
    """Fake store for knowledge and diagnostic store checks."""

    def __init__(
        self,
        list_result: object = None,
        list_events_result: object = None,
    ) -> None:
        self._list_result = list_result
        self._list_events_result = list_events_result

    def list(self) -> object:
        """Return a canned list."""
        return self._list_result

    def list_events(self) -> object:
        """Return a canned event list."""
        return self._list_events_result

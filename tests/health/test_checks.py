"""Tests for health check functions."""

from __future__ import annotations

import asyncio

import robotsix_chat.health.checks as checks_mod
from robotsix_chat.health.checks import (
    check_container_memory,
    check_diagnostics_store,
    check_feedback_runner,
    check_knowledge_store,
    check_memory,
)
from robotsix_chat.health.models import CheckSeverity

# ---------------------------------------------------------------------------
# check_memory
# ---------------------------------------------------------------------------


class TestCheckMemory:
    """Tests for :func:`check_memory`."""

    def test_no_memory_on_state(self) -> None:
        """WARNING when no memory backend is attached."""
        state = _FakeState(memory=None)
        result = asyncio.run(check_memory(state))
        assert result.name == "memory"
        assert result.status == CheckSeverity.WARNING
        assert "No memory backend" in result.message

    def test_no_status_method(self) -> None:
        """WARNING when the memory backend has no status() method."""
        state = _FakeState(memory=_FakeObject())
        result = asyncio.run(check_memory(state))
        assert result.status == CheckSeverity.WARNING
        assert "no status()" in result.message

    def test_status_returns_non_dict(self) -> None:
        """WARNING when status() returns a non-dict."""
        state = _FakeState(memory=_FakeMemory(status_result=42))
        result = asyncio.run(check_memory(state))
        assert result.status == CheckSeverity.WARNING
        assert "non-dict" in result.message

    def test_healthy(self) -> None:
        """OK when memory is healthy."""
        state = _FakeState(
            memory=_FakeMemory(status_result={"backend": "cognee", "degraded": False})
        )
        result = asyncio.run(check_memory(state))
        assert result.status == CheckSeverity.OK
        assert "healthy" in result.message

    def test_degraded(self) -> None:
        """ERROR when memory reports degraded."""
        state = _FakeState(
            memory=_FakeMemory(
                status_result={
                    "backend": "cognee",
                    "degraded": True,
                    "reason": "write failure",
                }
            )
        )
        result = asyncio.run(check_memory(state))
        assert result.status == CheckSeverity.ERROR
        assert "degraded" in result.message

    def test_status_raises(self) -> None:
        """ERROR when status() raises an exception."""
        state = _FakeState(memory=_FakeMemory(status_raises=RuntimeError("boom")))
        result = asyncio.run(check_memory(state))
        assert result.status == CheckSeverity.ERROR
        assert "exception" in result.message


# ---------------------------------------------------------------------------
# check_knowledge_store
# ---------------------------------------------------------------------------


class TestCheckKnowledgeStore:
    """Tests for :func:`check_knowledge_store`."""

    def test_no_store(self) -> None:
        """WARNING when no knowledge store is attached."""
        state = _FakeState(knowledge_store=None)
        result = asyncio.run(check_knowledge_store(state))
        assert result.status == CheckSeverity.WARNING
        assert "No knowledge store" in result.message

    def test_no_list_method(self) -> None:
        """WARNING when the store has no list() method."""
        state = _FakeState(knowledge_store=_FakeObject())
        result = asyncio.run(check_knowledge_store(state))
        assert result.status == CheckSeverity.WARNING
        assert "no list()" in result.message

    def test_healthy(self) -> None:
        """OK when the store responds to list()."""
        store = _FakeStore(list_result=[{"id": "1"}, {"id": "2"}])
        state = _FakeState(knowledge_store=store)
        result = asyncio.run(check_knowledge_store(state))
        assert result.status == CheckSeverity.OK
        assert "responsive" in result.message
        assert result.details["note_count"] == 2

    def test_list_raises(self) -> None:
        """ERROR when list() raises an exception."""
        store = _FakeStore(list_raises=RuntimeError("boom"))
        state = _FakeState(knowledge_store=store)
        result = asyncio.run(check_knowledge_store(state))
        assert result.status == CheckSeverity.ERROR
        assert "exception" in result.message


# ---------------------------------------------------------------------------
# check_feedback_runner
# ---------------------------------------------------------------------------


class TestCheckFeedbackRunner:
    """Tests for :func:`check_feedback_runner`."""

    def test_no_runner(self) -> None:
        """OK when no feedback runner is attached (disabled)."""
        state = _FakeState(feedback_runner=None)
        result = asyncio.run(check_feedback_runner(state))
        assert result.status == CheckSeverity.OK
        assert "No feedback runner" in result.message

    def test_disabled_no_board_url(self) -> None:
        """OK when the runner has no board_url (disabled)."""
        runner = _FakeFeedbackRunner(board_url="")
        state = _FakeState(feedback_runner=runner)
        result = asyncio.run(check_feedback_runner(state))
        assert result.status == CheckSeverity.OK
        assert "disabled" in result.message

    def test_configured_no_runs_yet(self) -> None:
        """OK when the runner is configured but hasn't run yet."""
        runner = _FakeFeedbackRunner(board_url="http://board:8077")
        state = _FakeState(feedback_runner=runner)
        result = asyncio.run(check_feedback_runner(state))
        assert result.status == CheckSeverity.OK
        assert "no runs yet" in result.message

    def test_active(self) -> None:
        """OK when the runner has been active."""
        runner = _FakeFeedbackRunner(
            board_url="http://board:8077",
            last_run_at={"sess1": 100.0},
            last_filed_at={"title1": 200.0},
        )
        state = _FakeState(feedback_runner=runner)
        result = asyncio.run(check_feedback_runner(state))
        assert result.status == CheckSeverity.OK
        assert "has been active" in result.message


# ---------------------------------------------------------------------------
# check_diagnostics_store
# ---------------------------------------------------------------------------


class TestCheckDiagnosticsStore:
    """Tests for :func:`check_diagnostics_store`."""

    def test_no_store(self) -> None:
        """OK when no diagnostic store is attached (optional)."""
        state = _FakeState(diagnostic_store=None)
        result = asyncio.run(check_diagnostics_store(state))
        assert result.status == CheckSeverity.OK
        assert "No diagnostic store" in result.message

    def test_no_list_events_method(self) -> None:
        """WARNING when the store has no list_events() method."""
        state = _FakeState(diagnostic_store=_FakeObject())
        result = asyncio.run(check_diagnostics_store(state))
        assert result.status == CheckSeverity.WARNING
        assert "no list_events()" in result.message

    def test_healthy(self) -> None:
        """OK when the store responds to list_events()."""
        store = _FakeStore(list_events_result=[{"event": "x"}])
        state = _FakeState(diagnostic_store=store)
        result = asyncio.run(check_diagnostics_store(state))
        assert result.status == CheckSeverity.OK
        assert "responsive" in result.message
        assert result.details["event_count"] == 1

    def test_list_events_raises(self) -> None:
        """ERROR when list_events() raises an exception."""
        store = _FakeStore(list_events_raises=RuntimeError("boom"))
        state = _FakeState(diagnostic_store=store)
        result = asyncio.run(check_diagnostics_store(state))
        assert result.status == CheckSeverity.ERROR
        assert "exception" in result.message


# ---------------------------------------------------------------------------
# check_container_memory
# ---------------------------------------------------------------------------


class TestCheckContainerMemory:
    """Tests for :func:`check_container_memory`."""

    @staticmethod
    def _point_at(monkeypatch, tmp_path, current: str, maximum: str) -> None:
        """Write cgroup fixture files and point the check at them."""
        cur = tmp_path / "memory.current"
        mx = tmp_path / "memory.max"
        cur.write_text(current, encoding="utf-8")
        mx.write_text(maximum, encoding="utf-8")
        monkeypatch.setattr(checks_mod, "_MEMORY_CURRENT_PATH", str(cur))
        monkeypatch.setattr(checks_mod, "_MEMORY_MAX_PATH", str(mx))

    def test_missing_files_is_ok(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """OK (never raises) when cgroup accounting files are absent."""
        monkeypatch.setattr(
            checks_mod, "_MEMORY_CURRENT_PATH", str(tmp_path / "nope.current")
        )
        monkeypatch.setattr(checks_mod, "_MEMORY_MAX_PATH", str(tmp_path / "nope.max"))
        result = asyncio.run(check_container_memory(_FakeState()))
        assert result.name == "container_memory"
        assert result.status == CheckSeverity.OK
        assert "unavailable" in result.message

    def test_no_limit_is_ok(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """OK when cgroup imposes no hard limit (memory.max=max)."""
        self._point_at(monkeypatch, tmp_path, current="1048576", maximum="max")
        result = asyncio.run(check_container_memory(_FakeState()))
        assert result.status == CheckSeverity.OK
        assert "No container memory limit" in result.message

    def test_unreadable_values_is_ok(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """OK when the accounting values are not integers."""
        self._point_at(monkeypatch, tmp_path, current="garbage", maximum="4096")
        result = asyncio.run(check_container_memory(_FakeState()))
        assert result.status == CheckSeverity.OK
        assert "unreadable" in result.message

    def test_below_threshold_is_ok(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """OK when usage is comfortably below the warn fraction."""
        # 1 GiB / 4 GiB = 25%
        self._point_at(
            monkeypatch,
            tmp_path,
            current=str(1024 * 1024 * 1024),
            maximum=str(4 * 1024 * 1024 * 1024),
        )
        result = asyncio.run(check_container_memory(_FakeState()))
        assert result.status == CheckSeverity.OK
        assert result.details["fraction"] == 0.25

    def test_at_threshold_warns(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """WARNING (pre-OOM alert) once usage reaches the warn fraction."""
        # 3.5 GiB / 4 GiB ≈ 87.5% ≥ default 0.85
        self._point_at(
            monkeypatch,
            tmp_path,
            current=str(int(3.5 * 1024 * 1024 * 1024)),
            maximum=str(4 * 1024 * 1024 * 1024),
        )
        result = asyncio.run(check_container_memory(_FakeState()))
        assert result.status == CheckSeverity.WARNING
        assert "approaching OOM" in result.message

    def test_custom_warn_fraction_from_settings(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The warn fraction is read from state.health_settings."""
        import types

        # 50% usage — below default 0.85 but above a custom 0.4 threshold.
        self._point_at(
            monkeypatch,
            tmp_path,
            current=str(2 * 1024 * 1024 * 1024),
            maximum=str(4 * 1024 * 1024 * 1024),
        )
        state = _FakeState(
            health_settings=types.SimpleNamespace(memory_warn_fraction=0.4)
        )
        result = asyncio.run(check_container_memory(state))
        assert result.status == CheckSeverity.WARNING


# ---------------------------------------------------------------------------
# test helpers
# ---------------------------------------------------------------------------


class _FakeState:
    """Minimal app.state stand-in for health-check unit tests."""

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeObject:
    """Object with no status/list methods — triggers the missing-method branch."""

    pass


class _FakeMemory:
    """Fake memory backend for testing check_memory."""

    def __init__(
        self,
        status_result: object | None = None,
        status_raises: Exception | None = None,
    ) -> None:
        self._status_result = status_result
        self._status_raises = status_raises

    def status(self) -> object:
        """Return a canned status dict or raise."""
        if self._status_raises is not None:
            raise self._status_raises
        return self._status_result


class _FakeStore:
    """Fake store that can serve as both knowledge and diagnostic store."""

    def __init__(
        self,
        list_result: object = None,
        list_raises: Exception | None = None,
        list_events_result: object = None,
        list_events_raises: Exception | None = None,
    ) -> None:
        self._list_result = list_result
        self._list_raises = list_raises
        self._list_events_result = list_events_result
        self._list_events_raises = list_events_raises

    def list(self) -> object:
        """Return a canned list or raise."""
        if self._list_raises is not None:
            raise self._list_raises
        return self._list_result

    def list_events(self) -> object:
        """Return a canned event list or raise."""
        if self._list_events_raises is not None:
            raise self._list_events_raises
        return self._list_events_result


class _FakeFeedbackRunner:
    """Fake feedback runner for testing check_feedback_runner."""

    def __init__(
        self,
        board_url: str = "",
        last_run_at: dict | None = None,
        last_filed_at: dict | None = None,
    ) -> None:
        self._board_url = board_url
        self._last_run_at = last_run_at or {}
        self._last_filed_at = last_filed_at or {}

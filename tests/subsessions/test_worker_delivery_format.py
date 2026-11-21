"""Worker failure-path and error-formatting tests.

Split out of ``test_worker.py`` (module_size): covers the failure delivery
path (an agent exception fails the subsession and reports to the parent) and
the unit tests for ``_format_worker_error`` / ``_truncate``.
"""

from __future__ import annotations

import pytest

from robotsix_chat.subsessions import SubsessionStatus
from robotsix_chat.subsessions.worker import _format_worker_error, _truncate
from tests.common.subsession_fakes import FakeAgent, build_env
from tests.subsessions.conftest import OWNER, _await_worker, _spawn

# ---------------------------------------------------------------------------
# failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_failure_marks_failed_and_delivers_summary() -> None:
    """An agent exception fails the subsession and reports to the parent."""
    agent = FakeAgent(error=RuntimeError("kaboom"))
    env = build_env(agent=agent)

    sub_id = _spawn(env, title="fragile")
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED
    # _format_worker_error now includes the exception type for unclassified
    # errors so opaque SDK strings are never passed through verbatim.
    assert "[RuntimeError] kaboom" in (info.error or "")
    assert info.summary is not None
    assert "kaboom" in info.summary
    # The task subsession should have exhausted its retry budget.
    assert info.retry_count == 3

    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    label, reply = history[0]
    assert "failed" in label
    assert reply == info.summary


# ---------------------------------------------------------------------------
# _format_worker_error / _truncate unit tests
# ---------------------------------------------------------------------------


def test_format_degenerate_success_error() -> None:
    """A degenerate-success exception gets a clear explanation."""
    exc = RuntimeError("Claude Code returned an error result: success")
    result = _format_worker_error(exc)
    assert "degenerate success frame" in result
    assert "known Claude SDK bug" in result
    assert "Original SDK message:" in result


def test_format_usage_exhausted_error() -> None:
    """A usage-exhausted exception gets a clear message about credits."""
    exc = RuntimeError("You are out of usage credits for this tier")
    result = _format_worker_error(exc)
    assert "usage credits" in result.lower()
    assert "exhausted" in result.lower()


def test_format_process_error_with_exit_code_and_stderr() -> None:
    """A ProcessError-like exception includes exit code and stderr."""
    exc = RuntimeError("command failed")
    exc.exit_code = 1  # type: ignore[attr-defined]
    exc.stderr = "permission denied\nfatal error"  # type: ignore[attr-defined]
    result = _format_worker_error(exc)
    assert "exited with code 1" in result
    assert "permission denied" in result
    assert "command failed" in result


def test_format_process_error_no_stderr() -> None:
    """ProcessError without stderr still includes the exit code."""
    exc = RuntimeError("something broke")
    exc.exit_code = 2  # type: ignore[attr-defined]
    result = _format_worker_error(exc)
    assert "exited with code 2" in result
    assert "stderr:" not in result


def test_format_process_error_empty_stderr() -> None:
    """ProcessError with empty stderr omits the stderr line."""
    exc = RuntimeError("fail")
    exc.exit_code = 3  # type: ignore[attr-defined]
    exc.stderr = "  "  # type: ignore[attr-defined]
    result = _format_worker_error(exc)
    assert "exited with code 3" in result
    assert "stderr:" not in result


def test_format_unknown_error_preserves_message() -> None:
    """An unrecognized error includes the exception type for diagnostics."""
    exc = RuntimeError("kaboom")
    result = _format_worker_error(exc)
    assert result == "[RuntimeError] kaboom"


def test_format_unknown_error_type_name_in_message() -> None:
    """When the type name is already in the message it is not duplicated."""
    exc = RuntimeError("RuntimeError: kaboom")
    result = _format_worker_error(exc)
    assert result == "RuntimeError: kaboom"


def test_truncate_short() -> None:
    """Short text is not truncated."""
    assert _truncate("hello", 10) == "hello"


def test_truncate_long() -> None:
    """Long text is truncated with trailing '...'."""
    assert _truncate("hello world", 5) == "hello..."


def test_truncate_exact() -> None:
    """Text at the exact limit is not truncated."""
    assert _truncate("hello", 5) == "hello"

"""Error-handling and wait-for-event machinery for the subsession worker.

Split out of ``test_worker.py`` (module_size): this module covers the
retry/error/fallback/wait-loop tail — non-transient error handling,
model-tier 404 fallback, tool-failure survival, wait-for-event safety-net
timeouts and checkpoint repair, errored-run notifications, on_close
semantics, event-wait dedup, and draft-decision comment gating.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE
from robotsix_chat.subsessions import (
    SubsessionInfo,
    SubsessionKind,
    SubsessionPeriodicSpawnError,
    SubsessionRegistry,
    SubsessionStatus,
    resume_subsessions,
    spawn_subsession,
)
from robotsix_chat.subsessions import subsession_waits as waits_mod
from robotsix_chat.subsessions import worker as worker_mod
from robotsix_chat.subsessions.subsession_waits import (
    _event_wait_loop,
    _run_wait_for_event_turn,
)
from robotsix_chat.subsessions.worker import (
    _MAX_PERIODIC_HISTORY_TURNS,
    _MAX_WORKER_HISTORY_TURNS,
    SubsessionEnv,
    _draft_decision_comment_posted,
    _history_cap,
    _run_turn,
)
from robotsix_chat.subsessions.worker_periodic import _build_periodic_input
from tests.common.subsession_fakes import (
    CapturingAgentFactory,
    FakeAgent,
    RecordingSink,
    build_env,
    make_settings,
    wait_until,
)


@pytest.fixture(autouse=True)
def _instant_transient_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse transient-retry backoff to zero for every test in this module.

    The retry loop is ``robotsix_http.acall_with_retry``, whose delay is
    ``min(backoff_base ** attempt, backoff_cap)``. Pinning the cap to 0
    makes every retry immediate, so the transient-error paths stay fast
    without a per-test knob — the two ``transient_error_backoff_*``
    settings these tests used to pass were removed along with the
    hand-rolled loop.
    """
    monkeypatch.setattr(worker_mod, "_TRANSIENT_BACKOFF_CAP", 0.0)


OWNER = "sess-main"


def _spawn(
    env: SubsessionEnv,
    *,
    kind: SubsessionKind = SubsessionKind.TASK,
    parent_id: str | None = None,
    depth: int = 1,
    title: str = "job",
    prompt: str = "do the thing",
    model_level: int = 3,
    **kwargs: object,
) -> str:
    """Spawn a subsession with sensible defaults for tests."""
    return spawn_subsession(
        env=env,
        kind=kind,
        owner_session_id=OWNER,
        parent_id=parent_id,
        depth=depth,
        title=title,
        prompt=prompt,
        model_level=model_level,
        **kwargs,  # type: ignore[arg-type]
    )


async def _await_worker(env: SubsessionEnv, sub_id: str, timeout: float = 2.0) -> None:
    """Wait for *sub_id*'s worker task to finish."""
    task = env.registry._running.get(sub_id)
    if task is not None:
        await asyncio.wait_for(task, timeout)


class _FakeTransientError(Exception):
    """A synthetic transient error for testing retry logic."""


@pytest.mark.asyncio
async def test_periodic_non_transient_error_not_retried_when_disabled() -> None:
    """Periodic subsessions use consecutive-error threshold, not worker retries."""
    agent = FakeAgent(error=ValueError("not transient"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            monitor_error_max_retries=0,
            consecutive_error_fail_threshold=1,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED
    # With threshold=1, one errored run fails the subsession.
    assert info.consecutive_errored_runs >= 1
    assert "not transient" in (info.error or "")


@pytest.mark.asyncio
async def test_periodic_non_transient_error_retries_then_fails() -> None:
    """Periodic monitors track consecutive errored runs and fail at threshold."""
    agent = FakeAgent(error=ValueError("tool retry limit"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            monitor_error_max_retries=2,
            consecutive_error_fail_threshold=3,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # After 3 consecutive errored runs the monitor is permanently failed.
    assert info.status is SubsessionStatus.FAILED
    assert info.consecutive_errored_runs >= 3
    assert "tool retry limit" in (info.error or "")
    # The error summary mentions consecutive errored runs.
    assert "consecutive errored runs" in (info.error or "")


@pytest.mark.asyncio
async def test_periodic_non_transient_error_retry_succeeds() -> None:
    """Periodic monitor that fails then succeeds continues running."""
    call_count = 0

    class FailOnceAgent:
        """Fails on the first call, succeeds on subsequent calls."""

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def stream(
            self,
            message: str,
            *,
            history: list[tuple[str, str]] | None = None,
            session_id: str | None = None,
            client_id: str | None = None,
            images: list[tuple[str, bytes]] | None = None,
            trace_metadata: dict[str, str] | None = None,
            trace_name: str | None = None,
            model_level: int | None = None,
        ) -> AsyncIterator[str]:
            nonlocal call_count
            call_count += 1
            self.calls.append({"message": message, "session_id": session_id})
            if call_count == 1:
                raise ValueError("transient glitch")
            yield "queue drained successfully"

    agent = FailOnceAgent()
    env = build_env(
        agent=agent,
        settings=make_settings(
            monitor_error_max_retries=2,
            consecutive_error_fail_threshold=3,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
            max_runs=2,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # The monitor should have completed (not FAILED).
    assert info.status is SubsessionStatus.CLOSED
    # The consecutive error counter was reset after the successful run.
    assert info.consecutive_errored_runs == 0
    assert info.runs >= 2


@pytest.mark.asyncio
async def test_wait_for_event_non_transient_error_retries() -> None:
    """WAIT_FOR_EVENT monitors track consecutive errored runs and fail at threshold."""
    agent = FakeAgent(error=ValueError("tool error"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            monitor_error_max_retries=1,
            consecutive_error_fail_threshold=1,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.WAIT_FOR_EVENT,
            dedup_key="ticket-123",
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED
    assert info.consecutive_errored_runs >= 1
    assert "consecutive errored runs" in (info.error or "")


@pytest.mark.asyncio
async def test_periodic_transient_error_transcript_recorded() -> None:
    """When transient retries are exhausted, a transcript entry is recorded."""
    agent = FakeAgent(error=_FakeTransientError("upstream hiccup"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            transient_error_max_retries=0,
            consecutive_error_fail_threshold=1000,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=True,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        with pytest.raises(asyncio.TimeoutError):
            await _await_worker(env, sub_id, timeout=0.5)

    info = env.registry.get(sub_id)
    assert info is not None
    # Check transcript has a system message about the errored run.
    system_entries = [e for e in info.transcript if e.role == "system"]
    assert any("Run errored" in (e.text or "") for e in system_entries)


@pytest.mark.asyncio
async def test_periodic_unexpected_model_behavior_retried_then_succeeds() -> None:
    """Periodic turn retries on UnexpectedModelBehavior, then succeeds."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    agent = FakeAgent()
    env = build_env(
        agent=agent,
        settings=make_settings(
            transient_error_max_retries=2,
            max_runs_progress_extension=0,
        ),
    )

    call_count = 0

    async def _flaky_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise UnexpectedModelBehavior("Streamed response ended without content")
        yield "recovered"

    with patch.object(agent, "stream", _flaky_stream):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
            max_runs=1,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # The subsession should NOT be failed — the retry succeeded.
    assert info.status is not SubsessionStatus.FAILED
    assert call_count == 2


@pytest.mark.asyncio
async def test_periodic_unexpected_model_behavior_exhausted_skips_cycle() -> None:
    """Periodic cycle is skipped when UnexpectedModelBehavior retries exhaust."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    agent = FakeAgent(error=UnexpectedModelBehavior("bad response shape"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            transient_error_max_retries=1,
            consecutive_error_fail_threshold=1000,
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
    )
    with pytest.raises(asyncio.TimeoutError):
        await _await_worker(env, sub_id, timeout=0.5)

    info = env.registry.get(sub_id)
    assert info is not None
    # Should NOT be FAILED — cycles were skipped gracefully.
    assert info.status is not SubsessionStatus.FAILED
    assert info.is_active


@pytest.mark.asyncio
async def test_task_unexpected_model_behavior_not_retried() -> None:
    """TASK subsessions do NOT retry UnexpectedModelBehavior — they fail immediately."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    agent = FakeAgent(error=UnexpectedModelBehavior("bad response"))
    env = build_env(agent=agent)

    sub_id = _spawn(env, prompt="compute", kind=SubsessionKind.TASK)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED


def test_is_unexpected_model_behavior() -> None:
    """Unit test for the `_is_unexpected_model_behavior` helper."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    from robotsix_chat.subsessions.worker import _is_unexpected_model_behavior

    # Direct match.
    assert _is_unexpected_model_behavior(UnexpectedModelBehavior("stream ended"))

    # Wrapped in another exception (cause chain).
    wrapper = RuntimeError("agent failed")
    wrapper.__cause__ = UnexpectedModelBehavior("empty response")
    assert _is_unexpected_model_behavior(wrapper)

    # Unrelated exception — should NOT match.
    assert not _is_unexpected_model_behavior(ValueError("not this"))

    # None in cause chain — should NOT match and should not loop.
    assert not _is_unexpected_model_behavior(RuntimeError("plain"))


# ---------------------------------------------------------------------------
# model-tier fallback (HTTP 404)
# ---------------------------------------------------------------------------


class _FakeModelTier404Error(Exception):
    """An exception that looks like a model-tier 404 to `_is_model_tier_not_found`."""

    def __init__(self, message: str = "model not found") -> None:
        super().__init__(message)
        self.status_code = 404


@pytest.mark.asyncio
async def test_periodic_model_tier_404_falls_back_to_lower_level() -> None:
    """A periodic subsession at level 2 that hits a 404 falls back to level 1."""
    error_agent = FakeAgent(error=_FakeModelTier404Error())
    fallback_agent = FakeAgent(["monitor result ok"])
    factory = CapturingAgentFactory(error_agent, fallback_agent)
    env = build_env(
        agent_factory=factory,
        settings=make_settings(llmio_api_key="test-key"),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        model_level=2,
        interval_seconds=0.02,
    )
    # The fallback worker skips the already-executed run 1 and goes to
    # sleep, so _await_worker will time out — that's expected.
    with pytest.raises(asyncio.TimeoutError):
        await _await_worker(env, sub_id, timeout=0.5)

    # Verify the factory was called twice: first at level 2, then at level 1.
    assert len(factory.captured) >= 2
    assert factory.captured[0]["model_level"] == 2
    assert factory.captured[1]["model_level"] == 1

    # The subsession should still be alive (not failed).
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status not in (SubsessionStatus.FAILED, SubsessionStatus.CLOSED)
    assert info.model_level == 1

    # The checkpoint should record the fallback.
    cp = info.checkpoint or {}
    assert cp.get("_tier_fallback_count") == 1
    assert cp.get("_fallback_model_level") == 1


@pytest.mark.asyncio
async def test_periodic_model_tier_404_fallback_exhausted_fails() -> None:
    """When all tier fallbacks are exhausted, the subsession fails."""
    error_agent = FakeAgent(error=_FakeModelTier404Error())
    factory = CapturingAgentFactory(error_agent)  # only one agent — all calls fail
    env = build_env(
        agent_factory=factory,
        settings=make_settings(llmio_api_key="test-key"),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        model_level=2,
        interval_seconds=0.02,
    )
    # The worker eventually fails after exhausting fallbacks.
    await _await_worker(env, sub_id, timeout=3.0)

    info = env.registry.get(sub_id)
    assert info is not None
    # After exhausting fallbacks (2→1→fail), the subsession should fail.
    assert info.status == SubsessionStatus.FAILED
    assert "model tier" in (info.error or "").lower()


@pytest.mark.asyncio
async def test_task_subsession_no_model_tier_fallback() -> None:
    """Task subsessions do NOT fall back on model-tier 404.

    They use the existing user_chat/task retry path instead.
    """
    env = build_env(
        agent=FakeAgent(error=_FakeModelTier404Error()),
        settings=make_settings(llmio_api_key="test-key"),
    )

    sub_id = _spawn(env, kind=SubsessionKind.TASK, model_level=2)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # Task subsessions should fail (they don't have model-tier fallback).
    assert info.status == SubsessionStatus.FAILED


@pytest.mark.asyncio
async def test_wait_for_event_model_tier_404_falls_back() -> None:
    """A wait_for_event subsession also gets model-tier fallback.

    The monitor is clamped from 3→2 by monitor_max_model_level,
    then the 404 fallback reduces it further from 2→1.
    """
    error_agent = FakeAgent(error=_FakeModelTier404Error())
    fallback_agent = FakeAgent(["NO_CHANGE"])
    factory = CapturingAgentFactory(error_agent, fallback_agent)
    env = build_env(
        agent_factory=factory,
        settings=make_settings(llmio_api_key="test-key"),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        model_level=3,
        dedup_key="ticket-123",
    )
    # The fallback worker skips the already-executed run 1 and enters
    # event-wait, so _await_worker will time out — that's expected.
    with pytest.raises(asyncio.TimeoutError):
        await _await_worker(env, sub_id, timeout=0.5)

    info = env.registry.get(sub_id)
    assert info is not None
    # Clamped 3→2 by monitor cap, then 404-fell-back 2→1.
    assert info.model_level == 1
    assert info.status not in (SubsessionStatus.FAILED, SubsessionStatus.CLOSED)


@pytest.mark.asyncio
async def test_periodic_model_tier_404_at_floor_no_fallback() -> None:
    """When already at the fallback floor (level 1), a 404 fails immediately."""
    env = build_env(
        agent=FakeAgent(error=_FakeModelTier404Error()),
        settings=make_settings(llmio_api_key="test-key"),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        model_level=1,
        interval_seconds=0.02,
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status == SubsessionStatus.FAILED


# consecutive error handling — acceptance criteria tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_failure_leaves_subsession_alive_periodic() -> None:
    """Tool failure exhausting retries in run k leaves subsession alive.

    Acceptance criteria: A simulated tool failure exhausting retries in
    run k leaves the subsession alive and run k+1 executes normally.
    """
    call_count = 0

    class FailThenSucceedAgent:
        """Fails on the first call, succeeds on subsequent calls."""

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def stream(
            self,
            message: str,
            *,
            history: list[tuple[str, str]] | None = None,
            session_id: str | None = None,
            client_id: str | None = None,
            images: list[tuple[str, bytes]] | None = None,
            trace_metadata: dict[str, str] | None = None,
            trace_name: str | None = None,
            model_level: int | None = None,
        ) -> AsyncIterator[str]:
            nonlocal call_count
            call_count += 1
            self.calls.append({"message": message, "session_id": session_id})
            if call_count == 1:
                raise ValueError("component_request exceeded max retries count of 2")
            yield "run completed normally"

    agent = FailThenSucceedAgent()
    env = build_env(
        agent=agent,
        settings=make_settings(
            consecutive_error_fail_threshold=3,
            transient_error_max_retries=0,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
            max_runs=2,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # The subsession completed normally after the error + recovery.
    assert info.status is SubsessionStatus.CLOSED
    assert info.runs >= 2
    assert info.consecutive_errored_runs == 0  # reset after success


@pytest.mark.asyncio
async def test_tool_failure_exhausting_retries_leaves_subsession_alive_wfe() -> None:
    """Same as periodic but for wait_for_event kind."""
    call_count = 0

    class FailThenSucceedAgent:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def stream(
            self,
            message: str,
            *,
            history: list[tuple[str, str]] | None = None,
            session_id: str | None = None,
            client_id: str | None = None,
            images: list[tuple[str, bytes]] | None = None,
            trace_metadata: dict[str, str] | None = None,
            trace_name: str | None = None,
            model_level: int | None = None,
        ) -> AsyncIterator[str]:
            nonlocal call_count
            call_count += 1
            self.calls.append({"message": message, "session_id": session_id})
            if call_count == 1:
                raise ValueError("component_request exceeded max retries count of 2")
            yield "event processed"

    agent = FailThenSucceedAgent()
    env = build_env(
        agent=agent,
        settings=make_settings(
            consecutive_error_fail_threshold=3,
            transient_error_max_retries=0,
            # Short event-driven timeout so the test doesn't wait 60s.
            event_driven_timeout_seconds=0.1,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.WAIT_FOR_EVENT,
            dedup_key="ticket-456",
        )
        # First run fails, second succeeds.  Wait for both to complete.
        # The event wait loop has a safety-net timeout, so we need to wait
        # long enough for it to fire.
        await asyncio.sleep(2.0)

    info = env.registry.get(sub_id)
    assert info is not None
    # The subsession should still be alive (not FAILED).
    assert info.status is not SubsessionStatus.FAILED
    assert info.consecutive_errored_runs == 0  # reset after success
    assert info.runs >= 2


@pytest.mark.asyncio
async def test_wfe_safety_net_timeout_skips_agent_turn_when_ticket_unchanged() -> None:
    """A quiet safety-net timeout costs a board read, not an agent turn.

    While the direct board read reports the ticket still in the monitor's
    ``last_known_state``, the wait is re-armed without an LLM turn.
    """
    from unittest.mock import AsyncMock

    agent = FakeAgent(default_reply="NO_CHANGE")
    settings = make_settings(
        event_driven_timeout_seconds=0.03,
        event_driven_max_silent_timeouts=100,
    )
    settings.direct_repo = SimpleNamespace(board_api_base_url="http://mill:8077")
    env = build_env(agent=agent, settings=settings)
    query = AsyncMock(return_value="ready")

    with patch.object(waits_mod, "_query_mill_ticket_state", query):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.WAIT_FOR_EVENT,
            dedup_key="ticket-789",
            checkpoint={"ticket_id": "ticket-789", "last_known_state": "READY"},
        )
        await wait_until(lambda: query.await_count >= 3)
        await asyncio.sleep(0.1)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.is_active
    # Only the first observation turn ran; every timeout since was absorbed.
    assert len(agent.calls) == 1
    assert query.await_args is not None
    assert query.await_args.args[:2] == ("http://mill:8077", "ticket-789")

    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")
    with contextlib.suppress(asyncio.CancelledError):
        await _await_worker(env, sub_id)


@pytest.mark.asyncio
async def test_wfe_safety_net_timeout_runs_agent_turn_when_ticket_changed() -> None:
    """A board read showing a different state still runs the safety-net turn."""
    from unittest.mock import AsyncMock

    agent = FakeAgent(default_reply="NO_CHANGE")
    settings = make_settings(
        event_driven_timeout_seconds=0.03,
        event_driven_max_silent_timeouts=100,
    )
    settings.direct_repo = SimpleNamespace(board_api_base_url="http://mill:8077")
    env = build_env(agent=agent, settings=settings)
    query = AsyncMock(return_value="done")

    with patch.object(waits_mod, "_query_mill_ticket_state", query):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.WAIT_FOR_EVENT,
            dedup_key="ticket-790",
            checkpoint={"ticket_id": "ticket-790", "last_known_state": "ready"},
        )
        await wait_until(lambda: len(agent.calls) >= 2)

    assert any("Safety-net timeout fired" in c["message"] for c in agent.calls[1:])

    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")
    with contextlib.suppress(asyncio.CancelledError):
        await _await_worker(env, sub_id)


@pytest.mark.asyncio
async def test_wfe_safety_net_cap_forces_agent_turn_after_silent_timeouts() -> None:
    """After ``event_driven_max_silent_timeouts`` quiet timeouts a turn runs anyway."""
    from unittest.mock import AsyncMock

    agent = FakeAgent(default_reply="NO_CHANGE")
    settings = make_settings(
        event_driven_timeout_seconds=0.03,
        event_driven_max_silent_timeouts=2,
    )
    settings.direct_repo = SimpleNamespace(board_api_base_url="http://mill:8077")
    env = build_env(agent=agent, settings=settings)
    query = AsyncMock(return_value="ready")

    with patch.object(waits_mod, "_query_mill_ticket_state", query):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.WAIT_FOR_EVENT,
            dedup_key="ticket-791",
            checkpoint={"ticket_id": "ticket-791", "last_known_state": "ready"},
        )
        await wait_until(lambda: len(agent.calls) >= 2)

    # Exactly two quiet timeouts were absorbed before the forced turn.
    assert query.await_count == 2

    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")
    with contextlib.suppress(asyncio.CancelledError):
        await _await_worker(env, sub_id)


@pytest.mark.asyncio
async def test_three_consecutive_errored_runs_fails_subsession() -> None:
    """3 consecutive errored runs fail the subsession with last error."""
    agent = FakeAgent(error=ValueError("tool retry limit exceeded"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            consecutive_error_fail_threshold=3,
            transient_error_max_retries=0,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED
    assert info.consecutive_errored_runs >= 3
    assert "consecutive errored runs" in (info.error or "")
    assert "tool retry limit exceeded" in (info.error or "")


@pytest.mark.asyncio
async def test_single_errored_run_produces_at_most_one_notification() -> None:
    """A single errored run produces at most one parent notification.

    Acceptance criteria: A single errored run produces at most one
    parent notification (avoid spamming a notice per errored run).
    """
    event_sink = RecordingSink()
    agent = FakeAgent(error=ValueError("transient tool failure"))
    env = build_env(
        agent=agent,
        event_sink=event_sink,
        settings=make_settings(
            consecutive_error_fail_threshold=5,
            transient_error_max_retries=0,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        # Wait for several errored runs (more than 1).
        await asyncio.sleep(0.3)

    info = env.registry.get(sub_id)
    assert info is not None
    # At least 2 errored runs happened.
    assert info.consecutive_errored_runs >= 2

    # Only one parent notification (urgency=low, not the result frame).
    notification_frames = [
        (s, f)
        for s, f in event_sink.frames
        if f.get("type") == SSE_NOTIFICATION_TYPE and f.get("urgency") == "low"
    ]
    assert len(notification_frames) == 1
    assert "errored run" in str(notification_frames[0][1].get("body", ""))


@pytest.mark.asyncio
async def test_no_behavior_change_for_task_kind() -> None:
    """Task subsessions still fail immediately on error."""
    agent = FakeAgent(error=ValueError("task tool failure"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            consecutive_error_fail_threshold=3,
            transient_error_max_retries=0,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(env, prompt="do something", kind=SubsessionKind.TASK)
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # TASK subsession should be failed immediately (no consecutive error tracking).
    assert info.status is SubsessionStatus.FAILED
    assert info.consecutive_errored_runs == 0  # not tracked for task kind


@pytest.mark.asyncio
async def test_threshold_zero_fails_on_first_errored_run() -> None:
    """consecutive_error_fail_threshold=0 fails the subsession on the first errored run.

    The docstring says "Set to 0 to fail on the first errored run (legacy
    behaviour)".  This test guards that edge case.
    """
    agent = FakeAgent(error=ValueError("tool failure"))
    env = build_env(
        agent=agent,
        settings=make_settings(
            consecutive_error_fail_threshold=0,
            transient_error_max_retries=0,
        ),
    )

    with patch(
        "robotsix_chat.subsessions.worker.is_openrouter_transient",
        return_value=False,
    ):
        sub_id = _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.02,
        )
        await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    # With threshold=0, the very first errored run should fail the subsession.
    assert info.status is SubsessionStatus.FAILED
    assert info.consecutive_errored_runs >= 1
    assert "consecutive errored runs" in (info.error or "")


# wait_for_event checkpoint repair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_event_turn_repairs_checkpoint_ticket_id() -> None:
    """After a set_checkpoint call wipes ticket_id, the turn handler restores it.

    The agent may call set_checkpoint with only ``last_known_state``,
    replacing the spawn-time checkpoint and losing the ticket_id.
    ``_run_wait_for_event_turn`` must recover it from the dedup_key
    and persist it so the subsession survives a restart.
    """
    agent = FakeAgent(["NO_CHANGE"])
    env = build_env(agent=agent)

    sub_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket abc",
        prompt="monitor the ticket",
        model_level=3,
        dedup_key="abc-123",
        checkpoint={"ticket_id": "abc-123", "last_known_state": "open"},
    )
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.dedup_key == "abc-123"

    # Simulate a legacy checkpoint that lost its ticket_id before the
    # registry-level preservation guard existed.  New ``update_checkpoint``
    # writes no longer drop the key, but a persisted entry from before the
    # fix can still load without it — the turn handler must repair it.
    info = env.registry.get(sub_id)
    assert info is not None
    info.checkpoint = {"last_known_state": "in_progress"}

    # Run the post-turn handler — it should repair the checkpoint.
    result = await _run_wait_for_event_turn(env, info, sub_id, "NO_CHANGE", None, 0)
    assert result is not None  # not closed

    # After repair, ticket_id must be back in the checkpoint.
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.checkpoint is not None
    assert info.checkpoint.get("ticket_id") == "abc-123"
    assert info.checkpoint.get("last_known_state") == "in_progress"


@pytest.mark.asyncio
async def test_wait_for_event_turn_repair_noop_when_ticket_id_present() -> None:
    """When ticket_id is already in checkpoint, the turn handler leaves it."""
    agent = FakeAgent(["NO_CHANGE"])
    env = build_env(agent=agent)

    sub_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket xyz",
        prompt="monitor the ticket",
        model_level=3,
        dedup_key="xyz-456",
        checkpoint={"ticket_id": "xyz-456", "last_known_state": "open"},
    )
    info = env.registry.get(sub_id)
    assert info is not None

    result = await _run_wait_for_event_turn(env, info, sub_id, "NO_CHANGE", None, 0)
    assert result is not None

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.checkpoint is not None
    assert info.checkpoint.get("ticket_id") == "xyz-456"


@pytest.mark.asyncio
async def test_wait_for_event_checkpoint_survives_resume(
    tmp_path: Path,
) -> None:
    """A wait_for_event subsession with ticket_id in checkpoint resumes cleanly.

    Simulates a restart: the checkpoint is persisted, a fresh registry
    loads it, and resume_subsessions re-spawns the monitor.  The resumed
    subsession must carry the ticket_id forward.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    wfe = registry1.create(
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket def",
        prompt="monitor the ticket",
        model_level=3,
        checkpoint={"ticket_id": "def-789", "last_known_state": "open"},
        dedup_key="def-789",
        event_timeout_seconds=3600.0,
    )
    registry1.set_status(wfe.id, SubsessionStatus.SLEEPING, runs=1)

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["ok"]),
        registry=registry2,
    )
    resume_subsessions(env)

    resumed = registry2.get(wfe.id)
    assert resumed is not None
    assert resumed.status is SubsessionStatus.RUNNING
    assert resumed.checkpoint is not None
    assert resumed.checkpoint.get("ticket_id") == "def-789"
    assert resumed.checkpoint.get("last_known_state") == "open"
    assert resumed.dedup_key == "def-789"

    # Clean up the worker.
    worker = registry2._running.get(wfe.id)
    registry2.cancel_and_close(wfe.id, reason="teardown", closed_by="system")
    if worker is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


# ---------------------------------------------------------------------------
# on_close kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_close_parent_already_closed_runs_immediately() -> None:
    """When the parent session is already closed, ON_CLOSE runs right away."""
    agent = FakeAgent(["cleanup complete"])
    store = ConversationStore()
    store.is_session_closed = lambda session_id: True  # type: ignore[method-assign]
    env = build_env(agent=agent, store=store)

    sub_id = _spawn(env, kind=SubsessionKind.ON_CLOSE, prompt="do the cleanup")
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "completed"
    assert info.summary == "cleanup complete"
    assert len(agent.calls) == 1
    assert agent.calls[0]["message"] == "do the cleanup"


@pytest.mark.asyncio
async def test_on_close_waits_until_parent_closes() -> None:
    """ON_CLOSE polls until the parent closes, then runs the one-shot task."""
    agent = FakeAgent(["cleanup done"])
    parent_closed = [False]  # mutable closure

    store = ConversationStore()
    store.is_session_closed = lambda session_id: parent_closed[0]  # type: ignore[method-assign]
    env = build_env(agent=agent, store=store)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        sub_id = _spawn(env, kind=SubsessionKind.ON_CLOSE, prompt="cleanup")

        # The worker spins in the close-waiting loop (sleep mocked to
        # return immediately).  Give it a few iterations.
        await asyncio.sleep(0.05)
        assert len(agent.calls) == 0, "agent must not be called while parent is open"

        # Simulate parent closing.
        parent_closed[0] = True

        await _await_worker(env, sub_id)

    assert len(agent.calls) == 1
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "completed"
    assert info.summary == "cleanup done"


@pytest.mark.asyncio
async def test_on_close_external_close_during_wait_exits_cleanly() -> None:
    """Subsession externally closed while waiting — worker exits without running."""
    agent = FakeAgent(["should not run"])

    store = ConversationStore()
    store.is_session_closed = lambda session_id: False  # type: ignore[method-assign]
    env = build_env(agent=agent, store=store)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        sub_id = _spawn(env, kind=SubsessionKind.ON_CLOSE, prompt="cleanup")

        # Let the worker enter the close-waiting loop.
        await asyncio.sleep(0.05)

        # Externally close the subsession while it's waiting.
        env.registry.cancel_and_close(sub_id, reason="cancelled", closed_by="user")

        # The worker task is cancelled by cancel_and_close;
        # suppress CancelledError when awaiting it.
        with contextlib.suppress(asyncio.CancelledError):
            await _await_worker(env, sub_id)

    # The agent must never have been called.
    assert len(agent.calls) == 0

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED


@pytest.mark.asyncio
async def test_on_close_child_of_periodic_is_rejected() -> None:
    """Spawning an ON_CLOSE child under a PERIODIC parent raises an error."""
    env = build_env()
    # Register a periodic parent without spawning a worker (same pattern as
    # test_periodic_parent_cannot_spawn_periodic_or_on_close_child).
    parent = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="parent periodic",
        prompt="monitor",
        model_level=3,
        interval_seconds=10.0,
    )

    with pytest.raises(SubsessionPeriodicSpawnError, match="on_close"):
        _spawn(
            env,
            kind=SubsessionKind.ON_CLOSE,
            parent_id=parent.id,
            depth=2,
            title="cleanup",
            prompt="clean up on close",
        )


@pytest.mark.asyncio
async def test_on_close_retries_up_to_user_chat_max_retries() -> None:
    """ON_CLOSE uses ``user_chat_max_retries`` when the agent fails."""
    agent = FakeAgent(error=RuntimeError("kaboom"))
    store = ConversationStore()
    store.is_session_closed = lambda session_id: True  # type: ignore[method-assign]
    env = build_env(agent=agent, store=store)

    sub_id = _spawn(env, kind=SubsessionKind.ON_CLOSE, prompt="cleanup")
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.FAILED
    # user_chat_max_retries defaults to 3 in make_settings.
    assert info.retry_count == 3
    assert "[RuntimeError] kaboom" in (info.error or "")


# ---------------------------------------------------------------------------
# _event_wait_loop — dedup_key fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_wait_loop_dedup_fallback_checkpoint_none() -> None:
    """When checkpoint is None, the dedup_key is used as ticket_id and written back."""
    env = build_env()
    sub_id = "sub-wfe-none"

    info = SubsessionInfo(
        id=sub_id,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="event monitor",
        prompt="monitor ticket foo",
        model_level=3,
        status=SubsessionStatus.RUNNING,
        created_at=1000.0,
        last_activity_at=1000.0,
        dedup_key="ticket-abc-123",
        checkpoint=None,
        event_timeout_seconds=5.0,
    )
    env.registry._subs[sub_id] = info

    with (
        patch.object(
            env.registry, "wait_for_inbox", new_callable=AsyncMock
        ) as mock_wait,
        patch.object(
            env.registry, "update_checkpoint", wraps=env.registry.update_checkpoint
        ) as mock_update,
        patch.object(env.registry, "drain_inbox", return_value=[]) as _mock_drain,
    ):
        mock_wait.return_value = True

        result = await _event_wait_loop(
            env, info, sub_id, previous_result=None, consecutive_no_change=0
        )

    # The checkpoint should have been repaired via update_checkpoint.
    mock_update.assert_called_once_with(sub_id, {"ticket_id": "ticket-abc-123"})
    # The in-memory info should now carry the repaired checkpoint.
    assert info.checkpoint == {"ticket_id": "ticket-abc-123"}
    # The function should return a tuple (not None), meaning it didn't close.
    assert result is not None
    pending, prev, cn_change = result
    assert prev is None
    assert cn_change == 0


@pytest.mark.asyncio
async def test_event_wait_loop_dedup_fallback_no_ticket_id() -> None:
    """When checkpoint has no ticket_id key, dedup_key fills the gap."""
    env = build_env()
    sub_id = "sub-wfe-missing"

    info = SubsessionInfo(
        id=sub_id,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="event monitor",
        prompt="monitor ticket bar",
        model_level=3,
        status=SubsessionStatus.RUNNING,
        created_at=1000.0,
        last_activity_at=1000.0,
        dedup_key="ticket-xyz-789",
        checkpoint={"last_known_state": "open"},
        event_timeout_seconds=5.0,
    )
    env.registry._subs[sub_id] = info

    with (
        patch.object(
            env.registry, "wait_for_inbox", new_callable=AsyncMock
        ) as mock_wait,
        patch.object(
            env.registry, "update_checkpoint", wraps=env.registry.update_checkpoint
        ) as mock_update,
        patch.object(env.registry, "drain_inbox", return_value=[]) as _mock_drain,
    ):
        mock_wait.return_value = True

        result = await _event_wait_loop(
            env, info, sub_id, previous_result="last ok", consecutive_no_change=2
        )

    # update_checkpoint should merge ticket_id into the existing checkpoint.
    mock_update.assert_called_once_with(
        sub_id, {"last_known_state": "open", "ticket_id": "ticket-xyz-789"}
    )
    assert info.checkpoint == {
        "last_known_state": "open",
        "ticket_id": "ticket-xyz-789",
    }
    assert result is not None
    pending, prev, cn_change = result
    assert prev == "last ok"
    assert cn_change == 2


@pytest.mark.asyncio
async def test_event_wait_loop_closes_when_no_ticket_id_and_no_dedup_key() -> None:
    """When both checkpoint and dedup_key lack a ticket_id, the subsession is closed."""
    env = build_env()
    sub_id = "sub-wfe-noid"

    info = SubsessionInfo(
        id=sub_id,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="broken monitor",
        prompt="monitor ticket baz",
        model_level=3,
        status=SubsessionStatus.RUNNING,
        created_at=1000.0,
        last_activity_at=1000.0,
        dedup_key=None,
        checkpoint=None,
        event_timeout_seconds=5.0,
    )
    env.registry._subs[sub_id] = info

    result = await _event_wait_loop(
        env, info, sub_id, previous_result=None, consecutive_no_change=0
    )

    # The function returns None when the subsession is closed.
    assert result is None
    # The subsession should be marked closed.
    refreshed = env.registry.get(sub_id)
    assert refreshed is not None
    assert refreshed.status is SubsessionStatus.CLOSED
    assert refreshed.close_reason == "missing_ticket_id"
    assert "no recoverable ticket_id" in (refreshed.summary or "").lower()


# ---------------------------------------------------------------------------
# auto-drive promotable-draft branch
# ---------------------------------------------------------------------------


def test_draft_decision_comment_posted_helper() -> None:
    """The draft-decision guard fires only for a comment-posted, still-draft ticket."""
    from robotsix_chat.subsessions import SubsessionInfo, SubsessionKind

    def _info(checkpoint: dict[str, object] | None) -> SubsessionInfo:
        return SubsessionInfo(
            id="sub-guard",
            kind=SubsessionKind.PERIODIC,
            owner_session_id="sess-1",
            parent_id=None,
            depth=1,
            title="monitor",
            prompt="watch ticket",
            model_level=3,
            status=SubsessionStatus.RUNNING,
            created_at=0.0,
            last_activity_at=0.0,
            checkpoint=checkpoint,
        )

    # No checkpoint — not posted.
    assert not _draft_decision_comment_posted(_info(None))
    # Flag set but ticket left draft — not posted (state changed).
    assert not _draft_decision_comment_posted(
        _info(
            {
                "ticket_id": "T-1",
                "last_known_state": "ready",
                "auto_drive_comment_posted": True,
            }
        )
    )
    # Still draft but flag unset — comment not posted yet.
    assert not _draft_decision_comment_posted(
        _info({"ticket_id": "T-1", "last_known_state": "draft"})
    )
    # Draft + flag set — posted.
    assert _draft_decision_comment_posted(
        _info(
            {
                "ticket_id": "T-1",
                "last_known_state": "draft",
                "auto_drive_comment_posted": True,
            }
        )
    )


def test_build_periodic_input_promotable_draft_default_is_operator_decision() -> None:
    """Default (gate off): the prompt instructs the operator-decision comment.

    The gate defaults to False — the monitor must NEVER auto-promote a
    draft.  It must instead post exactly one [AUTO_DRIVE] operator-decision
    comment and then reply QUEUED.
    """
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-x",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch ticket TICKET-9",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        dedup_key="TICKET-9",
        checkpoint={"ticket_id": "TICKET-9", "last_known_state": "draft"},
    )

    result = _build_periodic_input(info, previous_result=None, steering=[])

    # Operator-decision branch present, auto-promote branch absent.
    assert "DRAFT TICKETS — OPERATOR-DECISION BRANCH" in result
    assert "AUTO-PROMOTE BRANCH" not in result
    # The classification and idempotency guarantees are spelled out.
    assert "PROMOTABLE DRAFT" in result
    assert "## Problem" in result
    assert "## Acceptance criteria" in result
    assert "[AUTO_DRIVE]" in result
    assert "auto_drive_comment_posted" in result
    assert "EXACTLY ONE" in result
    # The monitor must not burn runs after posting — it replies QUEUED.
    assert "QUEUED" in result
    # Non-promotable drafts are left untouched.
    assert "NOT promotable" in result


def test_build_periodic_input_promotable_draft_gate_on_pre_authorized_promotes() -> (
    None
):
    """Gate ON + pre-authorized ticket: the prompt instructs auto-promotion."""
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-x",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch ticket TICKET-9",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        dedup_key="TICKET-9",
        checkpoint={"ticket_id": "TICKET-9", "last_known_state": "draft"},
    )

    result = _build_periodic_input(
        info,
        previous_result=None,
        steering=[],
        pre_authorized_patterns=["TICKET-*"],
        auto_drive_promote_ready_drafts=True,
    )

    # Auto-promote branch present, operator-decision branch absent.
    assert "DRAFT TICKETS — AUTO-PROMOTE BRANCH" in result
    assert "OPERATOR-DECISION BRANCH" not in result
    assert "mark_ticket_ready" in result
    # The monitor still must not burn runs after the transition.
    assert "QUEUED" in result
    # Non-promotable drafts are left untouched.
    assert "NOT promotable" in result


def test_build_periodic_input_promotable_draft_gate_on_not_pre_authorized() -> None:
    """Gate ON but ticket NOT pre-authorized: still the comment branch.

    The auto-promote branch requires BOTH the opt-in gate and a matching
    pre_authorized_ticket_patterns entry — a gate without authorization
    must not promote.
    """
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-x",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch ticket OTHER-3",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        dedup_key="OTHER-3",
        checkpoint={"ticket_id": "OTHER-3", "last_known_state": "draft"},
    )

    result = _build_periodic_input(
        info,
        previous_result=None,
        steering=[],
        pre_authorized_patterns=["TICKET-*"],
        auto_drive_promote_ready_drafts=True,
    )

    assert "DRAFT TICKETS — OPERATOR-DECISION BRANCH" in result
    assert "AUTO-PROMOTE BRANCH" not in result
    assert "[AUTO_DRIVE]" in result


@pytest.mark.asyncio
async def test_periodic_draft_comment_posted_skips_agent_turns() -> None:
    """A comment-posted, unchanged draft consumes NO further runs.

    Once the monitor's checkpoint carries ``auto_drive_comment_posted``
    with ``last_known_state='draft'``, the worker must skip the agent
    turn and wait event-driven — the run budget (e.g. a 60-run cap) is
    never exhausted re-driving the unchanged draft.
    """
    agent = FakeAgent(["NO_CHANGE"] * 3)
    env = build_env(agent=agent, settings=make_settings())

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        title="draft monitor",
    )
    assert env.registry.update_checkpoint(
        sub_id,
        {
            "ticket_id": "TICKET-DRAFT",
            "last_known_state": "draft",
            "auto_drive_comment_posted": True,
        },
    )
    # Let the worker reach its first loop iteration.
    await asyncio.sleep(0.15)

    info = env.registry.get(sub_id)
    assert info is not None
    # No agent turn was run — the comment is already posted.
    assert agent.calls == []
    assert info.runs == 0
    # The worker is waiting (SLEEPING), not closed, not failed.
    assert info.status is SubsessionStatus.SLEEPING
    assert info.close_reason is None

    # Cancel the waiting worker to end the test cleanly.
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (SubsessionKind.PERIODIC, _MAX_PERIODIC_HISTORY_TURNS),
        (SubsessionKind.WAIT_FOR_EVENT, _MAX_PERIODIC_HISTORY_TURNS),
        (SubsessionKind.TASK, _MAX_WORKER_HISTORY_TURNS),
        (SubsessionKind.USER_CHAT, _MAX_WORKER_HISTORY_TURNS),
        (SubsessionKind.ON_CLOSE, _MAX_WORKER_HISTORY_TURNS),
    ],
)
def test_history_cap_bounds_monitor_replay(kind: SubsessionKind, expected: int) -> None:
    """Monitor kinds get the tight replay cap; other kinds keep the default."""
    assert _history_cap(kind) == expected
    # The monitor bound is meaningfully tighter than the default window.
    assert _MAX_PERIODIC_HISTORY_TURNS < _MAX_WORKER_HISTORY_TURNS


@pytest.mark.asyncio
async def test_run_turn_slices_history_to_cap() -> None:
    """``_run_turn`` replays only the last ``max_history_turns`` prior turns."""
    agent = FakeAgent(["reply"])
    history = [(f"in {i}", f"out {i}") for i in range(10)]

    reply = await _run_turn(
        agent,
        "now",
        history,
        "sub-1",
        max_history_turns=_MAX_PERIODIC_HISTORY_TURNS,
    )

    assert reply == "reply"
    assert agent.calls[0]["history"] == history[-_MAX_PERIODIC_HISTORY_TURNS:]
    assert len(agent.calls[0]["history"]) == _MAX_PERIODIC_HISTORY_TURNS

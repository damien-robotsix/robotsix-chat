"""Subsession agent-turn runners with transient retry and timeouts.

Wraps a single agent turn with history capping, a wall-clock timeout, the
monitor-turn concurrency gate and bounded transient-error retries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from robotsix_http import RetryConfig, acall_with_retry
from robotsix_llmio.openrouter import is_openrouter_transient

from .models import (
    InboxMessage,
    SubsessionInfo,
    SubsessionKind,
    SubsessionStatus,
)
from .subsession_types import SubsessionEnv
from .turn_errors import _is_github_rate_limit_error, _is_unexpected_model_behavior

if TYPE_CHECKING:
    from robotsix_chat.chat.server.routes import ChatAgent

logger = logging.getLogger(__name__)


# Prior turns replayed to the subsession agent are capped so a
# long-running periodic/user_chat subsession cannot grow its own prompt
# without bound.
_MAX_WORKER_HISTORY_TURNS = 20


# Periodic/wait-for-event monitors run on a fresh, never-compacted CLI
# transcript per tick, so replaying the full 20-turn window re-carries an
# ever-growing prefix as cache traffic on every step of every tool loop.
# Bound monitor replay much tighter — the current tick only needs a couple
# of prior turns for continuity.
_MAX_PERIODIC_HISTORY_TURNS = 3


# Backoff policy for transient turn retries.  Module-level rather than
# operator-configurable: robotsix_http owns retry policy fleet-wide, and the
# only turn-level knob that ever needed tuning is the attempt count
# (``transient_error_max_retries``).  Tests collapse the cap to 0 so the
# retry paths run instantly — see tests/subsessions/test_worker.py.
#
# These reproduce the delays the hand-rolled loop produced: it used
# ``min(1.0 * 2**attempt, 30.0)`` -> 1s, 2s, 4s, and RetryConfig computes
# ``min(2.0**attempt, 30.0)`` -> the same 1s, 2s, 4s, with up to 50% jitter
# subtracted (RetryConfig.jitter_factor), which the old loop lacked.
_TRANSIENT_BACKOFF_BASE = 2.0


_TRANSIENT_BACKOFF_CAP = 30.0


_monitor_turn_gate: asyncio.Semaphore | None = None


_monitor_turn_gate_size: int = -1


def _get_monitor_turn_gate(size: int) -> asyncio.Semaphore | None:
    """Return the process-wide monitor-turn gate for *size* (``None`` = off).

    Bounds how many periodic/wait_for_event agent turns run at once: each
    turn spawns a claudeSDK CLI subprocess (~240MB RSS), and a board-event
    storm waking many monitors simultaneously drove the container to its
    memory limit and an OOM kill (2026-09-02 15:22Z).  Recreated when the
    configured size changes (tests, config reload).
    """
    global _monitor_turn_gate, _monitor_turn_gate_size
    if size <= 0:
        return None
    if _monitor_turn_gate is None or _monitor_turn_gate_size != size:
        _monitor_turn_gate = asyncio.Semaphore(size)
        _monitor_turn_gate_size = size
    return _monitor_turn_gate


async def _run_turn_with_transient_retry(
    env: SubsessionEnv,
    agent: ChatAgent,
    turn_input: str,
    history: list[tuple[str, str]],
    sub_id: str,
    info: SubsessionInfo,
) -> str:
    """Run one agent turn, retrying on transient API errors for periodic subsessions.

    For PERIODIC subsessions only: transient errors (e.g. OpenRouter
    upstream hiccups, GitHub API rate-limit 403/429, pydantic-ai
    ``UnexpectedModelBehavior`` from malformed provider responses) are
    retried with exponential backoff.  When all retries are exhausted the
    function raises :class:`_TransientExhaustedError` so the worker loop
    can skip the cycle gracefully instead of permanently failing the
    subsession.

    For TASK / USER_CHAT subsessions the error propagates unchanged —
    the outer handler will fail the subsession.

    The loop itself is :func:`robotsix_http.acall_with_retry`; the
    transient classification stays here, since it is domain knowledge
    (which provider errors are worth another turn) rather than transport
    policy.  ``acall_with_retry`` re-raises the original exception when
    attempts run out, so the exhausted-and-still-transient case is
    translated to the sentinel below.
    """
    settings = env.settings.subsessions
    max_retries = settings.transient_error_max_retries

    def _is_transient(exc: BaseException) -> bool:
        """Whether *exc* earns another turn for THIS subsession kind.

        Kind is part of the predicate on purpose: the same provider error
        is retryable for a monitor (skip the cycle, keep the schedule) and
        fatal for a task / user_chat run.  Returning ``False`` makes
        ``acall_with_retry`` re-raise immediately, which is exactly the
        old behaviour for non-monitors.
        """
        is_monitor = info.kind in (
            SubsessionKind.PERIODIC,
            SubsessionKind.WAIT_FOR_EVENT,
        )
        if not is_monitor:
            return False
        return is_openrouter_transient(exc) or (
            _is_github_rate_limit_error(exc) or _is_unexpected_model_behavior(exc)
        )

    def _log_retry(attempt: int, exc: Exception, delay: float) -> None:
        logger.warning(
            "Subsession %s run %d: transient error on attempt %d/%d — "
            "retrying in %.1fs. Error: %s",
            sub_id,
            info.runs + 1,
            attempt,
            max_retries + 1,
            delay,
            exc,
        )

    async def _one_turn() -> str:
        gate = (
            _get_monitor_turn_gate(settings.monitor_turn_concurrency)
            if info.kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT)
            else None
        )
        if gate is None:
            return await _run_turn_with_timeout(
                env, agent, turn_input, history, sub_id, info
            )
        async with gate:
            return await _run_turn_with_timeout(
                env, agent, turn_input, history, sub_id, info
            )

    try:
        # cast: acall_with_retry's `Callable[..., T]` resolves T to the
        # coroutine type for an async fn, so the awaited value is str at
        # runtime but Coroutine to the checker. Same treatment as the
        # existing call site in repo/direct/__init__.py.
        return cast(
            "str",
            await acall_with_retry(
                _one_turn,
                config=RetryConfig(
                    max_retries=max_retries,
                    backoff_base=_TRANSIENT_BACKOFF_BASE,
                    backoff_cap=_TRANSIENT_BACKOFF_CAP,
                    on_retry=_log_retry,
                ),
                what=f"subsession {sub_id} agent turn",
                is_transient_fn=_is_transient,
            ),
        )
    except _RunTimeoutError:
        raise  # timeout is handled separately; never retried
    except Exception as exc:
        if not _is_transient(exc):
            raise
        # Every sanctioned attempt was spent and the error is still
        # transient — surface the sentinel so the worker loop skips this
        # cycle instead of permanently failing the subsession.
        logger.error(
            "Subsession %s run %d: all %d transient-error retries exhausted "
            "(last error: %s) — skipping this cycle.",
            sub_id,
            info.runs + 1,
            max_retries + 1,
            exc,
        )
        raise _TransientExhaustedError(
            f"transient error persisted across {max_retries + 1} attempts"
        ) from exc


class _TransientExhaustedError(Exception):
    """Sentinel raised when all transient-error retries are exhausted.

    Caught by the worker loop for periodic subsessions to skip the cycle
    gracefully rather than permanently failing the subsession.
    """


def _history_cap(kind: SubsessionKind) -> int:
    """Return the prior-turn replay cap for *kind*.

    Periodic and wait-for-event monitors get the tight
    :data:`_MAX_PERIODIC_HISTORY_TURNS` bound; every other kind keeps the
    default :data:`_MAX_WORKER_HISTORY_TURNS` window.
    """
    if kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT):
        return _MAX_PERIODIC_HISTORY_TURNS
    return _MAX_WORKER_HISTORY_TURNS


async def _run_turn(
    agent: ChatAgent,
    turn_input: str,
    history: list[tuple[str, str]],
    sub_id: str,
    *,
    max_history_turns: int = _MAX_WORKER_HISTORY_TURNS,
    trace_metadata: dict[str, str] | None = None,
    trace_name: str | None = None,
    skip_recall: bool = False,
) -> str:
    """Run one agent turn and return the reply text."""
    parts = [
        chunk
        async for chunk in agent.stream(
            turn_input,
            history=history[-max_history_turns:] or None,
            session_id=sub_id,
            client_id=sub_id,
            trace_metadata=trace_metadata,
            trace_name=trace_name,
            skip_recall=skip_recall,
        )
    ]
    return "".join(parts)


_RUN_TIMEOUT_GRACE = 5.0


async def _run_turn_with_timeout(
    env: SubsessionEnv,
    agent: ChatAgent,
    turn_input: str,
    history: list[tuple[str, str]],
    sub_id: str,
    info: SubsessionInfo,
) -> str:
    """Run one agent turn with a hard timeout guard.

    On timeout the run is marked failed for TASK/USER_CHAT kinds, or the
    schedule continues with the failure recorded for PERIODIC kinds.
    """
    timeout = (
        info.run_timeout_seconds
        if info.run_timeout_seconds is not None
        else env.settings.subsessions.run_timeout_seconds
    )
    try:
        async with asyncio.timeout(timeout + _RUN_TIMEOUT_GRACE):
            return await _run_turn(
                agent,
                turn_input,
                history,
                sub_id,
                max_history_turns=_history_cap(info.kind),
                trace_metadata={
                    "owner_session_id": info.owner_session_id,
                    "parent_session_id": info.parent_id or info.owner_session_id,
                },
                trace_name="subsession-turn",
                # Monitor wakes (periodic / wait_for_event) feed fixed,
                # machine-generated instruction boilerplate as the turn
                # input; recalling on it churns the memory component for
                # near-random, noise-only matches. Monitors are
                # self-contained by design, so skip automatic recall.
                skip_recall=info.kind
                in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT),
            )
    except TimeoutError:
        logger.warning(
            "Subsession %s run timed out after %.0fs; marking run as failed",
            sub_id,
            timeout,
        )
        raise _RunTimeoutError(
            f"subsession run exceeded {timeout:.0f}s timeout"
        ) from None


class _RunTimeoutError(Exception):
    """Raised when a single subsession turn exceeds the run timeout.

    Internal sentinel — caught by the worker loop to trigger kind-specific
    failure handling without conflating with other CancelledError sources.
    """


async def _run_task_turn(
    env: SubsessionEnv, sub_id: str, reply: str
) -> list[InboxMessage]:
    """Handle TASK post-turn: drain inbox; return pending messages or close.

    Returns a non-empty list if steering messages arrived mid-turn
    (the worker should continue), or an empty list after closing the
    subsession (the worker should stop).
    """
    pending = env.registry.drain_inbox(sub_id)
    if pending:
        return pending  # a steering message arrived mid-turn
    closed = env.registry.mark_closed(
        sub_id, summary=reply, reason="completed", closed_by="agent"
    )
    if closed is not None:
        await env.delivery.deliver_summary(closed, reply, "completed")
    return []


async def _run_user_chat_turn(env: SubsessionEnv, sub_id: str) -> list[InboxMessage]:
    """Handle USER_CHAT post-turn: wait for inbox, drain, return pending."""
    env.registry.set_status(sub_id, SubsessionStatus.WAITING)
    await env.registry.wait_for_inbox(sub_id, timeout=None)
    return env.registry.drain_inbox(sub_id)

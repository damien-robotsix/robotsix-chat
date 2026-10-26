"""In-flight agent-turn tracking for graceful self-restart.

A process restart kills every agent turn in flight with no resume (incident
2026-10-25: four operator-approved executions cut mid-turn).  The lifecycle
``self_restart`` / ``restart_lifecycle_service(<own service>)`` tools consult
this module before restarting: when other turns are still running the restart
is *deferred* — a background task waits (bounded by
:data:`RESTART_DRAIN_TIMEOUT_SECONDS`) until the process is idle and only then
issues the restart call.

Counting is probe-based so the lifecycle package stays decoupled from the
server: ``create_app`` registers one probe per source of agent turns (the
main-chat message coalescer — which also carries periodic turns — and the
subsession registry).  Turns that bypass both — the subsession ParentDelivery
reaction turns — wrap themselves in :func:`track_inflight_turn`.  With no
probe registered and no tracked turn the count is ``0`` and the restart
proceeds immediately, exactly as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "RESTART_DRAIN_POLL_SECONDS",
    "RESTART_DRAIN_TIMEOUT_SECONDS",
    "clear_inflight_probes",
    "deferred_restart_pending",
    "inflight_count",
    "register_inflight_probe",
    "schedule_deferred_restart",
    "track_inflight_turn",
    "wait_for_drain",
]

#: Upper bound on how long a deferred restart waits for in-flight turns.
RESTART_DRAIN_TIMEOUT_SECONDS: float = 600.0
#: How often the drain wait re-counts in-flight turns.
RESTART_DRAIN_POLL_SECONDS: float = 2.0

_probes: dict[str, Callable[[], int]] = {}
_deferred_task: asyncio.Task[None] | None = None
_tracked_turns: int = 0


def register_inflight_probe(name: str, probe: Callable[[], int]) -> None:
    """Register (or replace) a probe returning a number of in-flight turns."""
    _probes[name] = probe


def clear_inflight_probes() -> None:
    """Drop every registered probe (tests / app teardown)."""
    _probes.clear()


@contextlib.contextmanager
def track_inflight_turn() -> Iterator[None]:
    """Count the enclosed agent turn as in flight (for turns no probe sees).

    Used by agent turns that bypass the message coalescer — e.g. the
    subsession ParentDelivery reaction turns, which run the main agent (with
    every tool, ``self_restart`` included) directly.
    """
    global _tracked_turns
    _tracked_turns += 1
    try:
        yield
    finally:
        _tracked_turns -= 1


def inflight_count() -> int:
    """Return the number of agent turns currently running process-wide.

    Sums every registered probe plus the turns entered through
    :func:`track_inflight_turn`.  A probe that raises is logged and counted
    as ``0`` — the drain gate must never block a restart forever because of
    a broken probe.
    """
    total = max(0, _tracked_turns)
    for name, probe in list(_probes.items()):
        try:
            total += max(0, int(probe()))
        except Exception:
            logger.exception("In-flight probe %r failed — counting 0", name)
    return total


async def wait_for_drain(
    *,
    threshold: int = 0,
    timeout: float | None = None,
    poll: float | None = None,
) -> bool:
    """Wait until :func:`inflight_count` is ``<= threshold`` or *timeout* passes.

    *timeout* / *poll* default to :data:`RESTART_DRAIN_TIMEOUT_SECONDS` /
    :data:`RESTART_DRAIN_POLL_SECONDS` (read at call time).  Returns ``True``
    when drained, ``False`` when the timeout expired.
    """
    if timeout is None:
        timeout = RESTART_DRAIN_TIMEOUT_SECONDS
    if poll is None:
        poll = RESTART_DRAIN_POLL_SECONDS
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if inflight_count() <= threshold:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll, remaining))


def deferred_restart_pending() -> bool:
    """Return whether a deferred restart is already waiting to fire."""
    return _deferred_task is not None and not _deferred_task.done()


def schedule_deferred_restart(
    restart: Callable[[], Awaitable[str]],
    *,
    timeout: float | None = None,
    poll: float | None = None,
) -> bool:
    """Run *restart* in the background once every in-flight turn finished.

    The wait targets zero in-flight turns — the calling turn itself ends
    right after the tool returns, so it drains too.  After *timeout* the
    restart fires regardless.  Returns ``False`` (and schedules nothing) when
    a deferred restart is already pending, so repeated calls never stack.
    """
    global _deferred_task
    if deferred_restart_pending():
        return False
    if timeout is None:
        timeout = RESTART_DRAIN_TIMEOUT_SECONDS
    drain_timeout = timeout

    async def _run() -> None:
        drained = await wait_for_drain(threshold=0, timeout=drain_timeout, poll=poll)
        if drained:
            logger.info("In-flight agent turns drained — restarting now")
        else:
            logger.warning(
                "Restart drain timeout (%.0fs) expired with %d turn(s) still "
                "in flight — restarting anyway",
                drain_timeout,
                inflight_count(),
            )
        try:
            result = await restart()
        except Exception:
            logger.exception("Deferred restart call failed")
            return
        logger.info("Deferred restart issued: %s", result)

    _deferred_task = asyncio.create_task(_run())
    return True

"""Periodic health-check scheduler.

Runs on a configurable interval (default 300 s) and on demand at
session start.  Stores the latest :class:`HealthStatus` on
``app.state.health_status`` and logs a warning when degradation
is detected or cleared.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from robotsix_chat.health.checks import CHECKS
from robotsix_chat.health.models import CheckSeverity, HealthStatus

logger = logging.getLogger(__name__)


class HealthScheduler:
    """Runs health checks on a periodic interval and stores results.

    Attached to ``app.state.health_scheduler`` during startup.  Call
    ``start()`` to begin the background loop and ``stop()`` to cancel it.

    Attributes:
        interval_seconds: Seconds between scheduled check cycles.
        state: The Starlette app state (``request.app.state``).
        _task: The running ``asyncio.Task``, or ``None``.

    """

    def __init__(
        self,
        interval_seconds: float,
        state: Any,
    ) -> None:
        """Create a scheduler that runs checks every *interval_seconds*.

        Args:
            interval_seconds: Seconds between scheduled check cycles.
            state: The Starlette app state (``request.app.state``).

        """
        self.interval_seconds = interval_seconds
        self._state = state
        self._task: asyncio.Task[None] | None = None
        self._previous_overall: CheckSeverity | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the background loop (idempotent — no-op if already running)."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("health scheduler started (interval=%ss)", self.interval_seconds)

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("health scheduler stopped")

    # ------------------------------------------------------------------
    # run-once (called by the scheduler and externally at session start)
    # ------------------------------------------------------------------

    async def run_once(self) -> HealthStatus:
        """Execute every check once and return the aggregate status.

        Stores the result on ``app.state.health_status`` and logs
        degradation transitions.
        """
        results = []
        worst = CheckSeverity.OK

        for check_name, check_fn in CHECKS:
            try:
                result = await check_fn(self._state)
                results.append(result)
                # Severity ordering: ERROR > WARNING > OK
                if result.status == CheckSeverity.ERROR or (
                    result.status == CheckSeverity.WARNING and worst == CheckSeverity.OK
                ):
                    worst = result.status
            except Exception:
                logger.debug(
                    "health check %r raised unexpectedly", check_name, exc_info=True
                )

        status = HealthStatus(
            checks=results,
            overall=worst,
        )

        # Persist on app.state for the /health endpoint and UI.
        if hasattr(self._state, "health_status"):
            self._state.health_status = status

        # Log degradation transitions.
        previous = self._previous_overall
        if worst != previous:
            if worst == CheckSeverity.ERROR:
                logger.warning(
                    "health status DEGRADED → ERROR (%d checks)",
                    len(results),
                )
            elif worst == CheckSeverity.WARNING:
                logger.warning(
                    "health status DEGRADED → WARNING (%d checks)",
                    len(results),
                )
            elif previous is not None and previous != CheckSeverity.OK:
                logger.info(
                    "health status RECOVERED → OK (%d checks)",
                    len(results),
                )
            self._previous_overall = worst

        return status

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Run checks forever on *interval_seconds* cadence."""
        while True:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("health scheduler loop iteration raised", exc_info=True)

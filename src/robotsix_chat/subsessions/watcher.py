"""Background watcher that resumes paused periodic monitors.

When a periodic subsession is auto-paused by ``max_idle_runs`` (closed
with reason ``"paused"``), it stops ticking.  This module provides a
lightweight asyncio task that periodically polls the mill for each
paused monitor's ticket state.  When the ticket's ``state`` differs
from the checkpoint's ``last_known_state``, the monitor is reopened
and its worker re-spawned.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .worker import SubsessionEnv

logger = logging.getLogger(__name__)

# How many seconds between poll ticks when no paused monitors exist
# (avoids busy-waiting when the watcher has nothing to do).
_IDLE_POLL_INTERVAL_SECONDS: float = 30.0


async def _query_ticket_state(
    board_url: str, ticket_id: str, sub_id: str
) -> str | None:
    """Return the current state string for *ticket_id*, or ``None`` on error."""
    try:
        base = httpx.URL(board_url.rstrip("/"))
        ticket_url = base.copy_with(path=f"/tickets/{ticket_id}")
    except Exception:
        logger.exception("Could not construct ticket URL for subsession %s", sub_id)
        return None

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.get(str(ticket_url))
            response.raise_for_status()
            ticket_data: dict[str, object] = response.json()
    except httpx.HTTPStatusError as exc:
        logger.debug(
            "Watcher: mill returned %d for ticket %s (subsession %s)",
            exc.response.status_code,
            ticket_id,
            sub_id,
        )
        return None
    except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
        logger.debug(
            "Watcher: mill unreachable for ticket %s (subsession %s): %s",
            ticket_id,
            sub_id,
            exc,
        )
        return None
    except Exception:
        logger.exception(
            "Watcher: unexpected error querying mill for ticket %s (subsession %s)",
            ticket_id,
            sub_id,
        )
        return None

    state = ticket_data.get("state")
    return (
        state if isinstance(state, str) else str(state) if state is not None else None
    )


async def _resume_paused_monitor(
    env: SubsessionEnv,
    sub_id: str,
) -> None:
    """Reopen a paused monitor and re-spawn its worker task."""
    from .worker import _subsession_worker

    info = env.registry.reopen(sub_id)
    if info is None:
        return

    logger.info(
        "Watcher: resuming paused monitor %s (%s) — ticket state changed.",
        sub_id,
        info.title,
    )
    task = asyncio.create_task(
        _subsession_worker(env, sub_id), context=contextvars.Context()
    )
    env.registry.attach_task(sub_id, task)
    env._tasks.add(task)
    task.add_done_callback(env._tasks.discard)


async def watch_paused_monitors(env: SubsessionEnv) -> None:
    """Background task: poll paused monitors and resume on state change.

    Runs forever — cancelled on server shutdown.  Must be started as an
    asyncio task after the server is ready (e.g. via the Starlette
    lifespan ``startup`` phase).
    """
    direct_repo = getattr(env.settings, "direct_repo", None)
    board_url = (
        getattr(direct_repo, "board_api_base_url", "")
        if direct_repo is not None
        else ""
    )
    if not board_url:
        logger.debug(
            "Watcher: board_api_base_url is not configured — "
            "paused monitors will not auto-resume until the next restart."
        )
        return

    # Use the configured poll interval, falling back to a sensible default.
    subsessions_cfg = getattr(env.settings, "subsessions", None)
    poll_interval: float = getattr(
        subsessions_cfg, "paused_monitor_poll_interval_seconds", 60.0
    )
    if poll_interval <= 0:
        poll_interval = 60.0

    logger.info(
        "Watcher: started (poll interval %.0f s, board_url=%s)",
        poll_interval,
        board_url,
    )

    while True:
        try:
            paused = env.registry.find_paused_periodic()
            if not paused:
                await asyncio.sleep(_IDLE_POLL_INTERVAL_SECONDS)
                continue

            for info in paused:
                checkpoint = info.checkpoint
                if checkpoint is None:
                    continue
                ticket_id_raw = checkpoint.get("ticket_id")
                if not isinstance(ticket_id_raw, str) or not ticket_id_raw:
                    continue

                ticket_id = ticket_id_raw
                last_known = checkpoint.get("last_known_state")
                last_known_str = (
                    (last_known if isinstance(last_known, str) else str(last_known))
                    if last_known is not None
                    else None
                )

                current_state = await _query_ticket_state(board_url, ticket_id, info.id)
                if current_state is None:
                    # Mill unreachable or ticket gone — skip this monitor
                    # this round; we'll try again on the next poll cycle.
                    continue

                # Resume when the ticket state has changed from what the
                # monitor last observed.
                if last_known_str is not None and current_state != last_known_str:
                    logger.info(
                        "Watcher: subsession %s ticket %s state changed "
                        "from '%s' to '%s' — resuming.",
                        info.id,
                        ticket_id,
                        last_known_str,
                        current_state,
                    )
                    await _resume_paused_monitor(env, info.id)
                else:
                    logger.debug(
                        "Watcher: subsession %s ticket %s still '%s' — keeping paused.",
                        info.id,
                        ticket_id,
                        current_state,
                    )

            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            logger.info("Watcher: cancelled — shutting down.")
            raise
        except Exception:
            logger.exception("Watcher: unexpected error in poll loop — retrying.")
            await asyncio.sleep(poll_interval)

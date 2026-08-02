"""Background watcher that resumes paused periodic monitors.

When a periodic subsession is auto-paused by ``max_idle_runs`` (set to
``PAUSED`` status with ``close_reason="paused"``), or auto-escalated
while stuck in ``human_issue_approval`` (``CLOSED`` with reason
``"human_approval_timeout"``), or immediately escalated for a
pre-authorized ticket (``CLOSED`` with reason
``"pre_authorized_approval"``), it stops ticking.  This module provides
a lightweight asyncio task that periodically polls the mill for each
such monitor's ticket state **and** — when the monitor's checkpoint
records a tracked PR — polls GitHub for merge status.

For ``PAUSED`` monitors the worker is still alive, blocking on an inbox
event — the watcher sends an inbox message to wake it immediately
(event-driven resume).  For ``CLOSED`` monitors there is no worker; the
watcher reopens the record and spawns a fresh worker.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import TYPE_CHECKING

import httpx

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE

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
    """Reopen a paused/timeout monitor and re-spawn its worker task.

    Used for CLOSED monitors (legacy records from before the ``PAUSED``
    status existed) and for ``human_approval_timeout`` /
    ``pre_authorized_approval`` records which are always CLOSED.
    For live ``PAUSED`` monitors use :func:`_wake_paused_monitor` instead.
    """
    from .worker import _subsession_worker

    info = env.registry.reopen(sub_id)
    if info is None:
        return

    logger.info(
        "Watcher: resuming monitor %s (%s) — ticket state changed.",
        sub_id,
        info.title,
    )
    task = asyncio.create_task(
        _subsession_worker(env, sub_id), context=contextvars.Context()
    )
    env.registry.attach_task(sub_id, task)
    env._tasks.add(task)
    task.add_done_callback(env._tasks.discard)

    if env.event_sink is not None:
        ticket_id_raw = info.checkpoint.get("ticket_id") if info.checkpoint else ""
        ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
        env.event_sink.publish(
            info.owner_session_id,
            {
                "type": SSE_NOTIFICATION_TYPE,
                "title": f"Monitor resumed: {info.title}",
                "body": (
                    f"Monitor {sub_id[:8]} tracking ticket {ticket_id} "
                    f"resumed after ticket state change."
                ),
                "urgency": "low",
                "link": ticket_id,
            },
        )


async def _wake_paused_monitor(
    env: SubsessionEnv,
    sub_id: str,
    ticket_id: str,
    new_state: str,
) -> bool:
    """Send a wake message to a live PAUSED monitor's inbox.

    The monitor's worker is alive and blocking on ``wait_for_inbox`` —
    the message wakes it immediately.  Returns ``True`` on success,
    ``False`` when the subsession is unknown or the message could not
    be delivered.
    """
    message = (
        f"Ticket {ticket_id} state changed to '{new_state}' — ticket state "
        f"changed, resuming monitor."
    )
    ok = env.registry.enqueue_message(sub_id, "system", message)
    if ok:
        logger.info(
            "Watcher: sent wake message to paused monitor %s (ticket %s → %s).",
            sub_id,
            ticket_id,
            new_state,
        )
    else:
        logger.debug(
            "Watcher: could not deliver wake message to monitor %s — "
            "worker may be dead, falling back to reopen.",
            sub_id,
        )
    return ok


async def _resume_merged_pr_monitor(
    env: SubsessionEnv,
    sub_id: str,
    pr_number: int,
    repo_full_name: str,
) -> None:
    """Reopen a paused monitor whose tracked PR has been merged."""
    from .worker import _subsession_worker

    info = env.registry.reopen(sub_id)
    if info is None:
        return

    logger.info(
        "Watcher: resuming monitor %s (%s) — tracked PR #%d in %s was merged.",
        sub_id,
        info.title,
        pr_number,
        repo_full_name,
    )
    task = asyncio.create_task(
        _subsession_worker(env, sub_id), context=contextvars.Context()
    )
    env.registry.attach_task(sub_id, task)
    env._tasks.add(task)
    task.add_done_callback(env._tasks.discard)

    if env.event_sink is not None:
        env.event_sink.publish(
            info.owner_session_id,
            {
                "type": SSE_NOTIFICATION_TYPE,
                "title": f"Monitor resumed: {info.title}",
                "body": (
                    f"Monitor {sub_id[:8]} resumed after "
                    f"PR #{pr_number} in {repo_full_name} was merged."
                ),
                "urgency": "low",
                "link": f"https://github.com/{repo_full_name}/pull/{pr_number}",
            },
        )


async def watch_paused_monitors(env: SubsessionEnv) -> None:
    """Background task: poll auto-paused/timeout monitors and resume on state change.

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
        logger.info("Watcher: polling disabled — paused monitors will not auto-resume.")
        return

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
                    # PAUSED monitors have a live worker — wake it via
                    # inbox message for instant event-driven resume.
                    if info.status == "paused":
                        woken = await _wake_paused_monitor(
                            env, info.id, ticket_id, current_state
                        )
                        if not woken:
                            # Worker may be dead (e.g. after a restart) —
                            # fall back to reopen+spawn.
                            await _resume_paused_monitor(env, info.id)
                    else:
                        await _resume_paused_monitor(env, info.id)
                else:
                    logger.debug(
                        "Watcher: subsession %s ticket %s still '%s' — keeping closed.",
                        info.id,
                        ticket_id,
                        current_state,
                    )

            # Second pass: check GitHub PR merge status for paused
            # monitors whose checkpoint records a tracked PR.  This
            # catches merges that the mill ticket API may not reflect
            # (e.g. when a PR is merged but the ticket state is not
            # automatically updated by the board).
            paused_after_mill = env.registry.find_paused_periodic()
            if paused_after_mill:
                direct_repo_settings = getattr(env.settings, "direct_repo", None)
                if direct_repo_settings is not None and getattr(
                    direct_repo_settings, "enabled", False
                ):
                    try:
                        from robotsix_chat.repo.direct.client import (
                            DirectRepoClient,
                        )

                        gh_client = DirectRepoClient(direct_repo_settings)
                        token = await gh_client._token()
                    except Exception:
                        logger.debug(
                            "Watcher: could not create GitHub client — "
                            "skipping PR merge checks."
                        )
                        token = None

                    if token is not None:
                        for info in paused_after_mill:
                            checkpoint = info.checkpoint
                            if checkpoint is None:
                                continue
                            pr_number_raw = checkpoint.get("pr_number")
                            if not isinstance(pr_number_raw, int) or pr_number_raw <= 0:
                                continue
                            repo_raw = checkpoint.get("repo_full_name")
                            if not isinstance(repo_raw, str) or not repo_raw:
                                continue

                            try:
                                pr_data = await gh_client.get_pr(
                                    repo_full_name=repo_raw,
                                    pr_number=pr_number_raw,
                                )
                            except Exception:
                                logger.debug(
                                    "Watcher: could not fetch PR #%d in %s "
                                    "(subsession %s) — skipping.",
                                    pr_number_raw,
                                    repo_raw,
                                    info.id,
                                )
                                continue

                            merged = pr_data.get("merged")
                            if merged is True:
                                logger.info(
                                    "Watcher: subsession %s PR #%d in %s "
                                    "was merged — resuming.",
                                    info.id,
                                    pr_number_raw,
                                    repo_raw,
                                )
                                await _resume_merged_pr_monitor(
                                    env, info.id, pr_number_raw, repo_raw
                                )
                                continue

                            # When CI is failing on a paused monitor's PR,
                            # keep the monitor paused rather than
                            # resuming it — the auto-pause delivery
                            # already escalated to the operator.
                            # Resuming would only create a wasteful
                            # pause-resume-pause loop since the
                            # monitor's agent cannot fix source-code
                            # or dependency issues on its own.
                            logger.debug(
                                "Watcher: subsession %s PR #%d in %s "
                                "not yet merged — keeping paused.",
                                info.id,
                                pr_number_raw,
                                repo_raw,
                            )

            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            logger.info("Watcher: cancelled — shutting down.")
            raise
        except Exception:
            logger.exception("Watcher: unexpected error in poll loop — retrying.")
            await asyncio.sleep(poll_interval)

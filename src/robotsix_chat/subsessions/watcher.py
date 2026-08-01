"""Background watcher that resumes paused and timeout-escalated periodic monitors.

When a periodic subsession is auto-paused by ``max_idle_runs`` (closed
with reason ``"paused"``), auto-escalated while stuck in
``human_issue_approval`` (closed with reason ``"human_approval_timeout"``),
or immediately escalated for a pre-authorized ticket (closed with reason
``"pre_authorized_approval"``), it stops ticking.  This module provides
a lightweight asyncio task that periodically polls the mill for each
such monitor's ticket state **and** — when the monitor's checkpoint
records a tracked PR — polls GitHub for merge status.  When the
ticket's ``state`` differs from the checkpoint's ``last_known_state``
*or* the tracked PR has been merged, the monitor is reopened and its
worker re-spawned.
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
    """Reopen a paused/timeout monitor and re-spawn its worker task."""
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

                            # Check CI status: if any recent workflow run
                            # on the PR's head branch has failed, resume
                            # the monitor so it stays active and reports
                            # the failure rather than staying hidden.
                            head_obj = pr_data.get("head")
                            head_ref: str | None = None
                            if isinstance(head_obj, dict):
                                raw = head_obj.get("ref")
                                if isinstance(raw, str):
                                    head_ref = raw
                            if head_ref:
                                runs = await gh_client.list_workflow_runs(
                                    repo_full_name=repo_raw,
                                    branch=head_ref,
                                    per_page=3,
                                )
                                ci_failing = any(
                                    r.get("conclusion") == "failure" for r in runs
                                )
                                if ci_failing:
                                    logger.info(
                                        "Watcher: subsession %s PR #%d in %s "
                                        "has failing CI (branch %s) — resuming.",
                                        info.id,
                                        pr_number_raw,
                                        repo_raw,
                                        head_ref,
                                    )
                                    await _resume_paused_monitor(env, info.id)
                                    continue

                            logger.debug(
                                "Watcher: subsession %s PR #%d in %s "
                                "not yet merged, CI stable — keeping paused.",
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

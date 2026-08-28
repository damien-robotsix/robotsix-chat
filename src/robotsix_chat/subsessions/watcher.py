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
import time
from typing import TYPE_CHECKING, cast

import httpx

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE
from robotsix_chat.repo.direct.board_client import BoardClient
from robotsix_chat.subsessions.worker_mill import _TICKET_STATE_TERMINAL

if TYPE_CHECKING:
    from .worker import SubsessionEnv

logger = logging.getLogger(__name__)

# How many seconds between poll ticks when no paused monitors exist
# (avoids busy-waiting when the watcher has nothing to do).
_IDLE_POLL_INTERVAL_SECONDS: float = 30.0

# Consecutive 404s from the board API before the watcher closes a
# paused monitor — the ticket no longer exists (deleted, or the
# monitor was spawned with a stale/paraphrased ID).
_MAX_WATCHER_404_FAILURES = 3

# Seconds before the health-check probe is considered failed (must be
# shorter than the poll interval so one slow probe doesn't stall the
# whole loop).
_HEALTH_CHECK_TIMEOUT: float = 5.0


async def _check_api_healthy(board_url: str) -> bool:
    """Return ``True`` when the board API is reachable and returning 2xx.

    Queries ``GET /tickets?limit=1`` (a lightweight list endpoint) so a
    transient outage that returns 404 or 5xx for every endpoint is
    distinguished from a genuine 404 on a single ticket.
    """
    try:
        base = httpx.URL(board_url.rstrip("/"))
        health_url = base.copy_with(path="/tickets", params={"limit": "1"})
    except Exception:
        return False
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HEALTH_CHECK_TIMEOUT)
        ) as client:
            response = await client.get(str(health_url))
            return response.is_success
    except Exception:
        logger.debug("Watcher: API health check failed for %s", board_url)
        return False


async def _query_ticket_state(
    board_url: str, ticket_id: str, sub_id: str
) -> tuple[str | None, str | None, int | None]:
    """Return ``(state, pr_url, http_status)`` for *ticket_id*.

    *pr_url* is the ticket's linked PR URL from the board API (may be
    ``None`` when the field is absent or null).

    *http_status* is the HTTP status code on HTTP errors, or ``None``
    for non-HTTP errors (timeout, connection error, malformed URL).
    Successful responses return ``(state, pr_url, None)``.
    """
    try:
        base = httpx.URL(board_url.rstrip("/"))
        ticket_url = base.copy_with(path=f"/tickets/{ticket_id}")
    except Exception:
        logger.exception("Could not construct ticket URL for subsession %s", sub_id)
        return (None, None, None)

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
        return (None, None, exc.response.status_code)
    except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
        logger.debug(
            "Watcher: mill unreachable for ticket %s (subsession %s): %s",
            ticket_id,
            sub_id,
            exc,
        )
        return (None, None, None)
    except Exception:
        logger.exception(
            "Watcher: unexpected error querying mill for ticket %s (subsession %s)",
            ticket_id,
            sub_id,
        )
        return (None, None, None)

    state = ticket_data.get("state")
    state_str = (
        state if isinstance(state, str) else str(state) if state is not None else None
    )
    pr_url = ticket_data.get("pr_url")
    pr_url_str = pr_url if isinstance(pr_url, str) and pr_url else None
    return (state_str, pr_url_str, None)


async def _check_pr_merged(
    env: SubsessionEnv,
    repo_full_name: str,
    pr_number: int,
    sub_id: str,
) -> bool | None:
    """Check whether a GitHub PR was merged.

    Returns ``True`` when merged, ``False`` when not merged (or PR not
    found / 404), and ``None`` when the GitHub API is unreachable and
    the check should be retried on the next poll cycle.
    """
    direct_repo_settings = getattr(env.settings, "direct_repo", None)
    if direct_repo_settings is None or not getattr(
        direct_repo_settings, "enabled", False
    ):
        logger.debug(
            "Watcher: direct_repo not enabled — cannot verify PR #%d in %s.",
            pr_number,
            repo_full_name,
        )
        return None

    try:
        from robotsix_chat.repo.direct.client import DirectRepoClient

        gh_client = DirectRepoClient(direct_repo_settings)
        token = await gh_client._token()
    except Exception:
        logger.debug(
            "Watcher: could not create GitHub client for PR #%d in %s "
            "(subsession %s) — deferring.",
            pr_number,
            repo_full_name,
            sub_id,
        )
        return None

    if token is None:
        logger.debug(
            "Watcher: no GitHub token available — cannot verify PR #%d in %s.",
            pr_number,
            repo_full_name,
        )
        return None

    try:
        pr_data = await gh_client.get_pr(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
    except Exception:
        logger.debug(
            "Watcher: could not fetch PR #%d in %s (subsession %s) — deferring.",
            pr_number,
            repo_full_name,
            sub_id,
        )
        return None

    merged = pr_data.get("merged")
    return merged is True


async def _search_prs_referencing_ticket(
    env: SubsessionEnv,
    repo_full_name: str,
    ticket_id: str,
    sub_id: str,
) -> list[dict[str, object]]:
    """Search GitHub for PRs in *repo_full_name* that reference *ticket_id*.

    Uses the GitHub Search API with a query like
    ``type:pr repo:owner/repo <ticket_id>``.  Returns a (possibly empty)
    list of matching PR dicts, or an empty list when the GitHub API is
    unreachable or direct_repo is not enabled.

    This is a cross-reference guard: when the board API's ``pr_url``
    field is null but a PR was merged, this search can find it so the
    watcher doesn't falsely keep a monitor paused for "no PR evidence."
    """
    direct_repo_settings = getattr(env.settings, "direct_repo", None)
    if direct_repo_settings is None or not getattr(
        direct_repo_settings, "enabled", False
    ):
        return []

    try:
        from robotsix_chat.repo.direct.client import DirectRepoClient

        gh_client = DirectRepoClient(direct_repo_settings)
        token = await gh_client._token()
    except Exception:
        return []

    if token is None:
        return []

    try:
        from urllib.parse import quote

        query = f"type:pr repo:{repo_full_name} {ticket_id}"
        data = await gh_client._get_json(
            f"/search/issues?q={quote(query, safe=':/')}&per_page=5"
        )
        items: list[dict[str, object]] = data.get("items", [])
        return items
    except Exception:
        logger.debug(
            "Watcher: GitHub PR search failed for ticket %s in %s (subsession %s).",
            ticket_id,
            repo_full_name,
            sub_id,
        )
        return []


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


async def _verify_image_publish_on_main(
    env: SubsessionEnv,
    repo_full_name: str,
    merge_sha: str,
    pr_number: int,
    sub_id: str,
) -> tuple[bool, str]:
    """Check whether the image-publish workflow succeeded on *merge_sha*.

    Queries the GitHub Actions API for the most recent run of the
    configured ``image_publish_workflow_name`` on the repo's default
    branch, filtering by *merge_sha*.  Returns ``(True, detail)`` when
    the run succeeded, ``(False, detail)`` otherwise.

    When the workflow name is empty (verification disabled), returns
    ``(True, "image-publish verification disabled")`` immediately.
    """
    subsessions_cfg = getattr(env.settings, "subsessions", None)
    workflow_name = (
        getattr(subsessions_cfg, "image_publish_workflow_name", "")
        if subsessions_cfg is not None
        else ""
    )
    if not workflow_name:
        return (True, "image-publish verification disabled")

    direct_repo_settings = getattr(env.settings, "direct_repo", None)
    if direct_repo_settings is None or not getattr(
        direct_repo_settings, "enabled", False
    ):
        return (
            True,
            "direct_repo not enabled — cannot verify image publish",
        )

    try:
        from robotsix_chat.repo.direct import actions_client

        actions = actions_client.ActionsClient(direct_repo_settings)
    except Exception:
        logger.debug(
            "Watcher: could not create ActionsClient for image-publish "
            "verification (subsession %s, PR #%d in %s) — deferring.",
            sub_id,
            pr_number,
            repo_full_name,
        )
        return (
            True,
            "ActionsClient unavailable — cannot verify image publish",
        )

    try:
        runs = await actions.list_workflow_runs(
            repo_full_name,
            head_sha=merge_sha,
            per_page=5,
            raise_on_error=False,
        )
    except Exception:
        logger.debug(
            "Watcher: could not list workflow runs for image-publish "
            "verification (subsession %s, PR #%d in %s) — deferring.",
            sub_id,
            pr_number,
            repo_full_name,
        )
        return (
            True,
            "GitHub Actions API unreachable — cannot verify image publish",
        )

    # Filter runs matching the configured workflow name.
    matching = [
        r
        for r in runs
        if r.get("name") == workflow_name
        or r.get("path", "").endswith(workflow_name)
        or workflow_name in r.get("path", "")
    ]
    if not matching:
        # No run found for this workflow on the merge SHA.
        # Check if there are any runs at all — if so, the workflow
        # may not have been triggered; if not, the SHA may be too new.
        if not runs:
            # No runs yet — the workflow may not have been triggered.
            # Return "defer" so the watcher retries on next poll.
            return (
                False,
                f"No CI runs yet for {merge_sha[:12]} — image publish "
                f"workflow '{workflow_name}' may not have been triggered",
            )
        return (
            False,
            f"Image publish workflow '{workflow_name}' not found in "
            f"recent CI runs for {merge_sha[:12]} — expected it to "
            f"run after merge",
        )

    latest_run = matching[0]
    status = latest_run.get("status", "")
    conclusion = latest_run.get("conclusion", "")
    run_id = latest_run.get("id", "?")
    run_url = latest_run.get("html_url", "")

    if status == "completed" and conclusion == "success":
        return (
            True,
            f"Image publish workflow '{workflow_name}' succeeded (run {run_id})",
        )

    if status == "completed" and conclusion != "success":
        return (
            False,
            f"Image publish workflow '{workflow_name}' {conclusion} "
            f"(run {run_id}, {run_url})",
        )

    # Still in progress — check timeout.
    verify_timeout = (
        getattr(subsessions_cfg, "image_publish_verify_timeout_seconds", 1800.0)
        if subsessions_cfg is not None
        else 1800.0
    )
    checkpoint: dict[str, object] = {}
    info = env.registry.get(sub_id)
    if info is not None and info.checkpoint:
        checkpoint = dict(info.checkpoint)

    first_seen_key = "_image_publish_verify_since"
    first_seen = checkpoint.get(first_seen_key)
    if first_seen is None:
        # Record when we first noticed the run was in progress.
        checkpoint[first_seen_key] = time.monotonic()
        env.registry.update_checkpoint(sub_id, checkpoint)
        return (
            False,
            f"Image publish workflow '{workflow_name}' still in progress "
            f"(run {run_id}) — waiting for completion",
        )

    elapsed = time.monotonic() - float(cast("float", first_seen))
    if elapsed >= verify_timeout:
        # Timeout — resume with a warning.
        del checkpoint[first_seen_key]
        env.registry.update_checkpoint(sub_id, checkpoint)
        return (
            True,
            f"Image publish workflow '{workflow_name}' timed out after "
            f"{elapsed:.0f}s — resuming with warning",
        )

    return (
        False,
        f"Image publish workflow '{workflow_name}' still in progress "
        f"(run {run_id}, {elapsed:.0f}s elapsed) — waiting for completion",
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

    # Track which (sub_id, condition) pairs have already been notified
    # so we don't spam the user on every poll cycle.
    _notified_conditions: dict[str, set[str]] = {}

    logger.info(
        "Watcher: started (poll interval %.0f s, board_url=%s)",
        poll_interval,
        board_url,
    )

    # Track subsessions for which we have already emitted a CI health
    # notification, so we don't spam the operator on every poll cycle.
    _ci_health_notified: set[str] = set()

    # Track subsessions for which we have already emitted a
    # "ticket_deleted" close notification so we don't re-notify on
    # every subsequent poll cycle.
    _ticket_deleted_notified: set[str] = set()

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

                # -- prerequisite-wait branch: when a monitor was paused
                #    because its prerequisite ticket's monitor closed,
                #    poll the prerequisite (not this monitor's own
                #    ticket) and resume when the prerequisite is terminal.
                if (
                    info.close_reason == "waiting_for_prerequisite"
                    and info.depends_on_ticket_id
                ):
                    prereq_state, _pr_url, prereq_http = await _query_ticket_state(
                        board_url, info.depends_on_ticket_id, info.id
                    )
                    if prereq_state is not None and prereq_state.lower() in (
                        _TICKET_STATE_TERMINAL
                    ):
                        logger.info(
                            "Watcher: subsession %s prerequisite ticket "
                            "%s is terminal (%s) — resuming.",
                            info.id,
                            info.depends_on_ticket_id,
                            prereq_state,
                        )
                        await _resume_paused_monitor(env, info.id)
                    elif prereq_http == 404:
                        logger.warning(
                            "Watcher: prerequisite ticket %s returned "
                            "404 for subsession %s — prerequisite may "
                            "have been deleted; keeping paused.",
                            info.depends_on_ticket_id,
                            info.id,
                        )
                    else:
                        logger.debug(
                            "Watcher: subsession %s prerequisite ticket "
                            "%s still in state %s — keeping paused.",
                            info.id,
                            info.depends_on_ticket_id,
                            prereq_state,
                        )
                    continue

                current_state, pr_url, http_status = await _query_ticket_state(
                    board_url, ticket_id, info.id
                )
                if current_state is None:
                    if http_status == 404:
                        # -- transient-outage guard: before counting a 404
                        #    toward the close threshold, verify the board API
                        #    is actually reachable.  If the API itself is
                        #    down (returning 404 for every endpoint), this is
                        #    a transient outage, not a deleted ticket.
                        api_healthy = await _check_api_healthy(board_url)
                        if not api_healthy:
                            logger.debug(
                                "Watcher: ticket %s returned 404 but API "
                                "health check failed — treating as "
                                "transient outage (subsession %s).",
                                ticket_id,
                                info.id,
                            )
                            continue

                        # Ticket not found — track consecutive 404s and
                        # close the monitor after the threshold to stop
                        # futile polling (the ticket was deleted or the
                        # monitor was spawned with a stale/paraphrased ID).
                        raw_count = checkpoint.get("_watcher_404_count")
                        count = (
                            int(raw_count) if isinstance(raw_count, (int, float)) else 0
                        )
                        count += 1
                        checkpoint["_watcher_404_count"] = count
                        env.registry.update_checkpoint(info.id, checkpoint)
                        if count >= _MAX_WATCHER_404_FAILURES:
                            # -- escalation: try to resolve the ticket ID --
                            # A 404 can mean the ticket ID in the checkpoint
                            # is stale or paraphrased.  Try resolution before
                            # closing so we don't discard a live ticket.
                            resolved_id: str | None = None
                            try:
                                direct_repo_settings = getattr(
                                    env.settings, "direct_repo", None
                                )
                                if direct_repo_settings is not None:
                                    board = BoardClient(direct_repo_settings)
                                    resolved = await board.resolve_ticket_ids(
                                        [ticket_id]
                                    )
                                    resolved_id = resolved.get(ticket_id)
                            except Exception:
                                logger.debug(
                                    "Watcher: ticket ID resolution failed for "
                                    "monitor %s (ticket %s) — proceeding to close.",
                                    info.id,
                                    ticket_id,
                                )

                            if resolved_id is not None and resolved_id != ticket_id:
                                # The ticket ID was resolved to a different
                                # valid ID — update the checkpoint and keep
                                # monitoring instead of closing.
                                logger.info(
                                    "Watcher: resolved stale ticket ID %s → %s "
                                    "for monitor %s — updating checkpoint and "
                                    "continuing.",
                                    ticket_id,
                                    resolved_id,
                                    info.id,
                                )
                                checkpoint["ticket_id"] = resolved_id
                                checkpoint.pop("_watcher_404_count", None)
                                env.registry.update_checkpoint(info.id, checkpoint)
                                if env.event_sink is not None:
                                    env.event_sink.publish(
                                        info.owner_session_id,
                                        {
                                            "type": SSE_NOTIFICATION_TYPE,
                                            "title": (
                                                f"Monitor ticket ID resolved: "
                                                f"{info.title}"
                                            ),
                                            "body": (
                                                f"Stale ticket ID {ticket_id} "
                                                f"resolved to {resolved_id} — "
                                                f"monitor {info.id[:8]} "
                                                f"continuing."
                                            ),
                                            "urgency": "low",
                                            "link": (
                                                f"{board_url.rstrip('/')}"
                                                f"/tickets/{resolved_id}"
                                            ),
                                        },
                                    )
                                continue

                            # -- escalation: notify the operator before closing --
                            if (
                                env.event_sink is not None
                                and info.id not in _ticket_deleted_notified
                            ):
                                _ticket_deleted_notified.add(info.id)
                                env.event_sink.publish(
                                    info.owner_session_id,
                                    {
                                        "type": SSE_NOTIFICATION_TYPE,
                                        "title": (f"Monitor stalled: {info.title}"),
                                        "body": (
                                            f"Ticket {ticket_id} returned 404 "
                                            f"for {count} consecutive watcher "
                                            f"polls — the ticket no longer "
                                            f"exists on the board or the "
                                            f"monitor was spawned with a "
                                            f"stale/paraphrased ID.  Closing "
                                            f"monitor {info.id[:8]} to "
                                            f"prevent futile polling.  "
                                            f"Re-spawn the monitor with a "
                                            f"valid ticket ID if the ticket "
                                            f"still needs tracking."
                                        ),
                                        "urgency": "medium",
                                        "link": (
                                            f"{board_url.rstrip('/')}"
                                            f"/tickets/{ticket_id}"
                                        ),
                                    },
                                )

                            summary = (
                                f"Ticket {ticket_id} returned 404 for "
                                f"{count} consecutive watcher polls — "
                                f"the ticket no longer exists on the "
                                f"board.  Closing monitor to prevent "
                                f"futile polling."
                            )
                            logger.warning(
                                "Watcher: closing monitor %s (ticket %s) "
                                "after %d consecutive 404s.",
                                info.id,
                                ticket_id,
                                count,
                            )
                            closed = env.registry.mark_closed(
                                info.id,
                                summary=summary,
                                reason="ticket_deleted",
                                closed_by="system",
                            )
                            if closed is not None:
                                await env.delivery.deliver_summary(
                                    closed, summary, "ticket_deleted"
                                )
                            else:
                                # Record was already CLOSED (e.g. legacy
                                # paused record) — update close_reason
                                # in-place so find_paused_periodic stops
                                # returning it on subsequent polls.
                                info.close_reason = "ticket_deleted"
                                info.summary = summary
                                await env.delivery.deliver_summary(
                                    info, summary, "ticket_deleted"
                                )
                            continue
                        logger.info(
                            "Watcher: ticket %s returned 404 for "
                            "monitor %s (%d/%d consecutive).",
                            ticket_id,
                            info.id,
                            count,
                            _MAX_WATCHER_404_FAILURES,
                        )
                    else:
                        # Non-404 error or successful response without
                        # state — reset the 404 counter (the ticket may
                        # still exist; this was a transient error).
                        if "_watcher_404_count" in checkpoint:
                            del checkpoint["_watcher_404_count"]
                            env.registry.update_checkpoint(info.id, checkpoint)
                    # Mill unreachable or ticket gone — skip this monitor
                    # this round; we'll try again on the next poll cycle.
                    continue
                # Successful response with state — reset the 404 counter.
                if "_watcher_404_count" in checkpoint:
                    del checkpoint["_watcher_404_count"]
                    env.registry.update_checkpoint(info.id, checkpoint)

                # Resume when the ticket state has changed from what the
                # monitor last observed.
                if last_known_str is not None and current_state != last_known_str:
                    # When the ticket transitioned to a terminal state
                    # (closed/done), guard against bogus closes: if there
                    # is no PR evidence (checkpoint has no pr_number and
                    # the board ticket's pr_url is null), keep the monitor
                    # paused rather than silently accepting a terminal
                    # state with no code merged.
                    if current_state.lower() in _TICKET_STATE_TERMINAL:
                        pr_number_raw = checkpoint.get("pr_number")
                        repo_raw = checkpoint.get("repo_full_name")
                        if (
                            isinstance(pr_number_raw, int)
                            and pr_number_raw > 0
                            and isinstance(repo_raw, str)
                            and repo_raw
                        ):
                            # Checkpoint tracks a PR — verify it was
                            # actually merged on GitHub before accepting
                            # the terminal state.
                            merged = await _check_pr_merged(
                                env,
                                repo_full_name=repo_raw,
                                pr_number=pr_number_raw,
                                sub_id=info.id,
                            )
                            if merged is True:
                                logger.info(
                                    "Watcher: subsession %s ticket %s terminal "
                                    "(%s) — tracked PR #%d in %s is merged; "
                                    "resuming.",
                                    info.id,
                                    ticket_id,
                                    current_state,
                                    pr_number_raw,
                                    repo_raw,
                                )
                            elif merged is None:
                                # GitHub unreachable — defer; keep paused.
                                logger.warning(
                                    "Watcher: subsession %s ticket %s terminal "
                                    "(%s) — could not verify PR #%d in %s "
                                    "(GitHub unreachable); keeping paused.",
                                    info.id,
                                    ticket_id,
                                    current_state,
                                    pr_number_raw,
                                    repo_raw,
                                )
                                continue
                            else:
                                # PR exists but is NOT merged — the
                                # terminal state is bogus; keep paused.
                                logger.warning(
                                    "Watcher: subsession %s ticket %s terminal "
                                    "(%s) but tracked PR #%d in %s is NOT "
                                    "merged — keeping paused (possible "
                                    "premature close with no code merged).",
                                    info.id,
                                    ticket_id,
                                    current_state,
                                    pr_number_raw,
                                    repo_raw,
                                )
                                continue
                        elif pr_url:
                            # Board ticket has a recorded pr_url — let the
                            # resume proceed; the second-pass merge check
                            # will catch unmerged PRs on the next cycle.
                            logger.info(
                                "Watcher: subsession %s ticket %s terminal "
                                "(%s) — board pr_url is present; resuming.",
                                info.id,
                                ticket_id,
                                current_state,
                            )
                        else:
                            # No PR evidence at all — the ticket was
                            # closed with pr_url=null and no checkpoint PR
                            # info.
                            #
                            # Before keeping paused, try to cross-reference
                            # via the GitHub search API: if the checkpoint
                            # carries a repo_full_name (even without a
                            # pr_number), search for PRs in that repo that
                            # reference the ticket ID.  A PR may have been
                            # merged by a different agent, leaving
                            # pr_url null on the board.
                            repo_raw = checkpoint.get("repo_full_name")
                            if isinstance(repo_raw, str) and repo_raw:
                                prs = await _search_prs_referencing_ticket(
                                    env,
                                    repo_full_name=repo_raw,
                                    ticket_id=ticket_id,
                                    sub_id=info.id,
                                )
                                if prs:
                                    # Found at least one PR referencing
                                    # this ticket — pick the first one
                                    # and record it in the checkpoint
                                    # so the merge guard can verify it.
                                    first_pr = prs[0]
                                    pr_num = first_pr.get("number")
                                    if isinstance(pr_num, int) and pr_num > 0:
                                        logger.info(
                                            "Watcher: subsession %s ticket %s "
                                            "terminal (%s) — found PR #%d in %s "
                                            "via GitHub search; updating "
                                            "checkpoint and resuming.",
                                            info.id,
                                            ticket_id,
                                            current_state,
                                            pr_num,
                                            repo_raw,
                                        )
                                        checkpoint["pr_number"] = pr_num
                                        checkpoint["repo_full_name"] = repo_raw
                                        env.registry.update_checkpoint(
                                            info.id, checkpoint
                                        )
                                        # Fall through to the resume path
                                        # below — don't continue.
                                    else:
                                        logger.warning(
                                            "Watcher: subsession %s ticket %s "
                                            "terminal (%s) — GitHub search "
                                            "returned PR without a number; "
                                            "keeping paused.",
                                            info.id,
                                            ticket_id,
                                            current_state,
                                        )
                                        continue
                                else:
                                    logger.warning(
                                        "Watcher: subsession %s ticket %s "
                                        "terminal (%s) but pr_url is null, "
                                        "checkpoint has no pr_number, and "
                                        "GitHub search found no PRs "
                                        "referencing the ticket in %s — "
                                        "keeping paused (ticket was closed "
                                        "with no PR evidence).",
                                        info.id,
                                        ticket_id,
                                        current_state,
                                        repo_raw,
                                    )
                                    continue
                            else:
                                logger.warning(
                                    "Watcher: subsession %s ticket %s terminal "
                                    "(%s) but pr_url is null and checkpoint has "
                                    "no pr_number or repo_full_name — keeping "
                                    "paused (ticket was closed with no PR "
                                    "evidence).",
                                    info.id,
                                    ticket_id,
                                    current_state,
                                )
                                continue

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
                            pr_state = pr_data.get("state")
                            mergeable = pr_data.get("mergeable")

                            # --- PR closed without merging ---
                            # Detect when a PR is closed (not open) but was
                            # never merged — the change was lost silently.
                            if pr_state == "closed" and merged is not True:
                                cond_key = "closed_unmerged"
                                notified = _notified_conditions.setdefault(
                                    info.id, set()
                                )
                                if cond_key not in notified:
                                    notified.add(cond_key)
                                    logger.warning(
                                        "Watcher: subsession %s PR #%d in %s "
                                        "was CLOSED WITHOUT MERGING "
                                        "(state=%s, merged=%s).",
                                        info.id,
                                        pr_number_raw,
                                        repo_raw,
                                        pr_state,
                                        merged,
                                    )
                                    ticket_id_for_pr = checkpoint.get("ticket_id")
                                    ticket_id_str = (
                                        ticket_id_for_pr
                                        if isinstance(ticket_id_for_pr, str)
                                        else ""
                                    )
                                    # Check whether the owning ticket is
                                    # already in a terminal state — if so,
                                    # this is expected and we skip the alarm.
                                    ticket_terminal = False
                                    if ticket_id_str:
                                        ticket_state = await _query_ticket_state(
                                            board_url,
                                            ticket_id_str,
                                            info.id,
                                        )
                                        if ticket_state is not None:
                                            state_str = ticket_state[0]
                                            ticket_terminal = (
                                                state_str is not None
                                                and state_str.lower()
                                                in _TICKET_STATE_TERMINAL
                                            )
                                    if not ticket_terminal:
                                        if env.event_sink is not None:
                                            env.event_sink.publish(
                                                info.owner_session_id,
                                                {
                                                    "type": SSE_NOTIFICATION_TYPE,
                                                    "title": (
                                                        f"PR #{pr_number_raw} "
                                                        f"closed without merging"
                                                    ),
                                                    "body": (
                                                        f"PR #{pr_number_raw} in "
                                                        f"{repo_raw} was closed "
                                                        f"without being merged. "
                                                        f"The changes may be lost. "
                                                        f"Ticket: {ticket_id_str}"
                                                    ),
                                                    "urgency": "high",
                                                    "link": (
                                                        f"https://github.com/"
                                                        f"{repo_raw}/pull/"
                                                        f"{pr_number_raw}"
                                                    ),
                                                },
                                            )
                                        # Try to open a follow-up ticket on
                                        # the board so the operator sees it.
                                        try:
                                            board = BoardClient(
                                                env.settings.direct_repo
                                            )
                                            followup_id = await board.create_ticket(
                                                title=(
                                                    f"PR #{pr_number_raw} in "
                                                    f"{repo_raw} was closed "
                                                    f"without merging"
                                                ),
                                                description=(
                                                    f"PR [#{pr_number_raw}]"
                                                    f"(https://github.com/"
                                                    f"{repo_raw}/pull/"
                                                    f"{pr_number_raw}) in "
                                                    f"`{repo_raw}` was closed "
                                                    f"without being merged.\n\n"
                                                    f"Original ticket: "
                                                    f"{ticket_id_str}\n"
                                                    f"Monitor subsession: "
                                                    f"{info.id}\n\n"
                                                    f"The changes may have "
                                                    f"been lost — review and "
                                                    f"re-open if needed."
                                                ),
                                                kind="task",
                                                source="agent",
                                            )
                                            if followup_id:
                                                logger.info(
                                                    "Watcher: created follow-up "
                                                    "ticket %s for closed-"
                                                    "unmerged PR #%d in %s.",
                                                    followup_id,
                                                    pr_number_raw,
                                                    repo_raw,
                                                )
                                        except Exception:
                                            logger.debug(
                                                "Watcher: could not create "
                                                "follow-up ticket for "
                                                "closed-unmerged PR #%d "
                                                "in %s (board may be "
                                                "unreachable).",
                                                pr_number_raw,
                                                repo_raw,
                                            )
                                    else:
                                        logger.debug(
                                            "Watcher: subsession %s PR #%d "
                                            "in %s closed unmerged but "
                                            "ticket %s is terminal (%s) — "
                                            "no alarm.",
                                            info.id,
                                            pr_number_raw,
                                            repo_raw,
                                            ticket_id_str,
                                            ticket_state,
                                        )
                                # Resume the monitor so it can report the
                                # failure to the user and stop polling.
                                if info.status == "paused":
                                    woken = await _wake_paused_monitor(
                                        env,
                                        info.id,
                                        ticket_id_str,
                                        f"PR #{pr_number_raw} closed without merging",
                                    )
                                    if not woken:
                                        await _resume_paused_monitor(env, info.id)
                                else:
                                    await _resume_paused_monitor(env, info.id)
                                continue

                            # --- Merge conflict detection ---
                            # Flag merge conflicts as soon as they are
                            # detected instead of waiting for a monitor
                            # report.
                            if mergeable is False:
                                cond_key = "merge_conflict"
                                notified = _notified_conditions.setdefault(
                                    info.id, set()
                                )
                                if cond_key not in notified:
                                    notified.add(cond_key)
                                    logger.warning(
                                        "Watcher: subsession %s PR #%d in %s "
                                        "has MERGE CONFLICTS "
                                        "(mergeable=%s, mergeable_state=%s).",
                                        info.id,
                                        pr_number_raw,
                                        repo_raw,
                                        mergeable,
                                        pr_data.get("mergeable_state", "unknown"),
                                    )
                                    if env.event_sink is not None:
                                        env.event_sink.publish(
                                            info.owner_session_id,
                                            {
                                                "type": SSE_NOTIFICATION_TYPE,
                                                "title": (
                                                    f"PR #{pr_number_raw} "
                                                    f"has merge conflicts"
                                                ),
                                                "body": (
                                                    f"PR #{pr_number_raw} in "
                                                    f"{repo_raw} has merge "
                                                    f"conflicts and cannot be "
                                                    f"merged.  Resolve the "
                                                    f"conflicts or rebase the "
                                                    f"branch."
                                                ),
                                                "urgency": "high",
                                                "link": (
                                                    f"https://github.com/"
                                                    f"{repo_raw}/pull/"
                                                    f"{pr_number_raw}"
                                                ),
                                            },
                                        )

                            if merged is True:
                                # -- image-publish verification gate ----
                                # Before resuming, verify the image-publish
                                # workflow on the merge commit succeeded so
                                # the monitor sees the running service
                                # actually contains the new code.
                                merge_sha = pr_data.get("merge_commit_sha") or ""
                                if isinstance(merge_sha, str) and merge_sha:
                                    (
                                        pub_ok,
                                        pub_detail,
                                    ) = await _verify_image_publish_on_main(
                                        env,
                                        repo_raw,
                                        merge_sha,
                                        pr_number_raw,
                                        info.id,
                                    )
                                else:
                                    pub_ok, pub_detail = (
                                        True,
                                        "no merge_commit_sha — skipping "
                                        "image-publish verification",
                                    )
                                if not pub_ok:
                                    logger.info(
                                        "Watcher: subsession %s PR #%d in %s "
                                        "merged but image-publish not yet "
                                        "verified: %s",
                                        info.id,
                                        pr_number_raw,
                                        repo_raw,
                                        pub_detail,
                                    )
                                    if (
                                        env.event_sink is not None
                                        and info.id not in _ci_health_notified
                                    ):
                                        _ci_health_notified.add(info.id)
                                        env.event_sink.publish(
                                            info.owner_session_id,
                                            {
                                                "type": (SSE_NOTIFICATION_TYPE),
                                                "title": ("Image publish pending"),
                                                "body": (
                                                    f"PR #{pr_number_raw} in "
                                                    f"{repo_raw} merged, but "
                                                    f"the image-publish "
                                                    f"workflow has not "
                                                    f"succeeded yet: "
                                                    f"{pub_detail}.  "
                                                    f"Waiting for the "
                                                    f"workflow to complete "
                                                    f"before resuming "
                                                    f"monitor "
                                                    f"{info.id[:8]}."
                                                ),
                                                "urgency": "low",
                                                "link": (
                                                    f"https://github.com/"
                                                    f"{repo_raw}/pull/"
                                                    f"{pr_number_raw}"
                                                ),
                                            },
                                        )
                                    continue
                                logger.info(
                                    "Watcher: subsession %s PR #%d in %s "
                                    "was merged — resuming "
                                    "(image-publish verified: %s).",
                                    info.id,
                                    pr_number_raw,
                                    repo_raw,
                                    pub_detail,
                                )
                                await _resume_merged_pr_monitor(
                                    env, info.id, pr_number_raw, repo_raw
                                )
                                continue

                            # -- CI run tracking and infrastructure health check ----
                            # Track the latest workflow run id per poll
                            # cycle so we can detect when a push (e.g. a
                            # formatting fix) lands on the PR branch but
                            # does NOT trigger a new CI run — the watcher
                            # would otherwise re-evaluate the same run on
                            # every poll and report stale results.
                            pr_branch = pr_data.get("head", {}).get("ref")
                            pr_head_sha = pr_data.get("head", {}).get("sha")
                            if isinstance(pr_branch, str) and pr_branch:
                                try:
                                    from robotsix_chat.repo.direct import (
                                        actions_client,
                                    )

                                    actions = actions_client.ActionsClient(
                                        direct_repo_settings
                                    )

                                    # -- run-ID tracking: detect stale CI runs --
                                    if isinstance(pr_head_sha, str) and pr_head_sha:
                                        latest_runs = await actions.list_workflow_runs(
                                            repo_raw,
                                            branch=pr_branch,
                                            per_page=1,
                                        )
                                        if latest_runs:
                                            latest_run = latest_runs[0]
                                            latest_run_id = latest_run.get("id")
                                            latest_run_head = latest_run.get("head_sha")
                                            last_ci_run_id = checkpoint.get(
                                                "last_ci_run_id"
                                            )
                                            last_ci_head_sha = checkpoint.get(
                                                "last_ci_head_sha"
                                            )

                                            if latest_run_head != pr_head_sha:
                                                logger.warning(
                                                    "Watcher: subsession %s "
                                                    "PR #%d in %s — latest CI "
                                                    "run %s (head %s) does not "
                                                    "match PR HEAD %s.  The "
                                                    "most recent push may not "
                                                    "have triggered a new CI "
                                                    "run yet.",
                                                    info.id,
                                                    pr_number_raw,
                                                    repo_raw,
                                                    latest_run_id,
                                                    latest_run_head,
                                                    pr_head_sha,
                                                )

                                            if (
                                                isinstance(last_ci_run_id, int)
                                                and latest_run_id == last_ci_run_id
                                                and pr_head_sha != last_ci_head_sha
                                            ):
                                                logger.warning(
                                                    "Watcher: subsession %s "
                                                    "PR #%d in %s — CI run "
                                                    "%d unchanged since last "
                                                    "poll but PR HEAD changed "
                                                    "from %s to %s.  No new CI "
                                                    "run has been triggered "
                                                    "for the latest push.",
                                                    info.id,
                                                    pr_number_raw,
                                                    repo_raw,
                                                    latest_run_id,
                                                    last_ci_head_sha,
                                                    pr_head_sha,
                                                )

                                            checkpoint["last_ci_run_id"] = latest_run_id
                                            checkpoint["last_ci_head_sha"] = pr_head_sha
                                            env.registry.update_checkpoint(
                                                info.id, checkpoint
                                            )

                                    zero_job_msg = (
                                        await actions.check_latest_run_for_zero_jobs(
                                            repo_raw, pr_branch
                                        )
                                    )
                                    if zero_job_msg is not None:
                                        logger.critical(zero_job_msg)
                                        if (
                                            env.event_sink is not None
                                            and info.id not in _ci_health_notified
                                        ):
                                            _ci_health_notified.add(info.id)
                                            env.event_sink.publish(
                                                info.owner_session_id,
                                                {
                                                    "type": SSE_NOTIFICATION_TYPE,
                                                    "title": (
                                                        "CI infrastructure failure "
                                                        "detected"
                                                    ),
                                                    "body": zero_job_msg,
                                                    "urgency": "high",
                                                    "link": (
                                                        f"https://github.com/"
                                                        f"{repo_raw}/pull/"
                                                        f"{pr_number_raw}"
                                                    ),
                                                },
                                            )
                                except Exception:
                                    logger.debug(
                                        "Watcher: could not check CI health for "
                                        "PR #%d in %s (subsession %s) — skipping.",
                                        pr_number_raw,
                                        repo_raw,
                                        info.id,
                                    )

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

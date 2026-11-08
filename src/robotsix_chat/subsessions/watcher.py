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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE
from robotsix_chat.repo.direct.board_client import BoardClient
from robotsix_chat.ticket_poll.mill_states import MERGED_TERMINAL_STATES

if TYPE_CHECKING:
    from .models import SubsessionInfo
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


def _history_shows_implementation_and_pr(ticket_data: dict[str, object]) -> bool:
    """Return whether the ticket history shows implementation AND PR evidence.

    Scans the board ticket's ``events`` and ``history`` arrays (state
    transitions and lifecycle events).  A record counts as *implementation*
    evidence when its descriptive fields mention ``implement``, ``document``
    or ``deliver``; it counts as *PR* evidence when they mention a pull
    request (``open_pr``, ``pull_request``, ``/pull/``, ``merge``) or carry a
    non-empty ``pr_url`` / ``pr_number``.  Only when both kinds of evidence
    appear does the function return ``True``.

    This is the final cross-check before the watcher classifies a terminal
    ticket as "closed with no PR evidence": a ticket whose history shows an
    implement cycle *and* a PR-creation/merge event was almost certainly
    delivered, and the null ``pr_url`` is a board metadata gap rather than a
    genuine "closed without a PR".
    """
    records: list[dict[str, object]] = []
    for key in ("events", "history"):
        seq = ticket_data.get(key)
        if isinstance(seq, list):
            records.extend(rec for rec in seq if isinstance(rec, dict))
    if not records:
        return False

    saw_impl = False
    saw_pr = False
    for rec in records:
        blob = " ".join(
            str(rec.get(field, ""))
            for field in ("type", "action", "state", "to", "kind", "summary", "detail")
        ).lower()
        if "implement" in blob or "document" in blob or "deliver" in blob:
            saw_impl = True
        if (
            "open_pr" in blob
            or "pull_request" in blob
            or "/pull/" in blob
            or "merge" in blob
            or rec.get("pr_url")
            or rec.get("pr_number")
        ):
            saw_pr = True
        if saw_impl and saw_pr:
            return True
    return False


async def _ticket_history_has_merge_evidence(
    board_url: str,
    ticket_id: str,
    sub_id: str,
) -> bool:
    """Fetch the ticket JSON and return whether its history shows PR evidence.

    Delegates the classification to
    :func:`_history_shows_implementation_and_pr`.  Returns ``False`` on any
    fetch or parse error so the caller falls back to the existing
    "keep paused" behaviour rather than resuming on unverified data.
    """
    try:
        base = httpx.URL(board_url.rstrip("/"))
        ticket_url = base.copy_with(path=f"/tickets/{ticket_id}")
    except Exception:
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.get(str(ticket_url))
            response.raise_for_status()
            ticket_data: object = response.json()
    except Exception:
        logger.debug(
            "Watcher: could not fetch ticket %s history for merge-evidence "
            "cross-check (subsession %s).",
            ticket_id,
            sub_id,
        )
        return False
    if not isinstance(ticket_data, dict):
        return False
    return _history_shows_implementation_and_pr(ticket_data)


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
        checkpoint[first_seen_key] = time.time()
        env.registry.update_checkpoint(sub_id, checkpoint)
        return (
            False,
            f"Image publish workflow '{workflow_name}' still in progress "
            f"(run {run_id}) — waiting for completion",
        )

    elapsed = time.time() - cast(float, first_seen)
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


# -- watch_paused_monitors helpers -----------------------------------------

# Per-monitor decision returned by the pass-1 / pass-2 helpers:
# ``"resume"`` — the monitor was woken or reopened; ``"keep_paused"`` —
# it was evaluated and stays paused; ``"skip"`` — it was not evaluated
# this round (no usable checkpoint, transient error, …).
_MonitorDecision = Literal["resume", "keep_paused", "skip"]

# Outcome of :func:`_handle_ticket_404`.
_Ticket404Outcome = Literal["transient", "counted", "resolved", "closed"]


@dataclass
class _WatcherState:
    """Notification de-duplication state kept across watcher poll cycles.

    Attributes
    ----------
    notified_conditions : dict[str, set[str]]
        Per-subsession set of PR conditions (``"closed_unmerged"``,
        ``"merge_conflict"``) already notified, so the user is not
        spammed on every poll cycle.
    ci_health_notified : set[str]
        Subsessions for which a CI-health / image-publish notification
        was already emitted.
    ticket_deleted_notified : set[str]
        Subsessions for which a ``ticket_deleted`` close notification was
        already emitted.

    """

    notified_conditions: dict[str, set[str]] = field(default_factory=dict)
    ci_health_notified: set[str] = field(default_factory=set)
    ticket_deleted_notified: set[str] = field(default_factory=set)


def _resolve_watcher_config(env: SubsessionEnv) -> tuple[str, float] | None:
    """Resolve the board URL and poll interval the watcher runs with.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment whose ``settings`` are read.

    Returns
    -------
    tuple[str, float] or None
        ``(board_url, poll_interval)``, or ``None`` when the watcher must
        not run (no ``direct_repo.board_api_base_url`` configured, or a
        non-positive ``subsessions.paused_monitor_poll_interval_seconds``).

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
        return None

    # Use the configured poll interval, falling back to a sensible default.
    subsessions_cfg = getattr(env.settings, "subsessions", None)
    poll_interval: float = getattr(
        subsessions_cfg, "paused_monitor_poll_interval_seconds", 60.0
    )
    if poll_interval <= 0:
        logger.info("Watcher: polling disabled — paused monitors will not auto-resume.")
        return None
    return board_url, poll_interval


async def _resume_or_wake(
    env: SubsessionEnv,
    info: SubsessionInfo,
    ticket_id: str,
    new_state: str,
) -> None:
    """Wake a live ``PAUSED`` monitor, or reopen it when that is impossible.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    info : SubsessionInfo
        The paused monitor to resume.
    ticket_id : str
        Ticket id quoted in the wake message.
    new_state : str
        New state / condition quoted in the wake message.

    """
    # PAUSED monitors have a live worker — wake it via inbox message
    # for instant event-driven resume.
    if info.status == "paused":
        woken = await _wake_paused_monitor(env, info.id, ticket_id, new_state)
        if not woken:
            # Worker may be dead (e.g. after a restart) — fall back to
            # reopen+spawn.
            await _resume_paused_monitor(env, info.id)
    else:
        await _resume_paused_monitor(env, info.id)


async def _check_prerequisite_wait(
    env: SubsessionEnv,
    board_url: str,
    info: SubsessionInfo,
) -> _MonitorDecision:
    """Poll a monitor's prerequisite ticket and resume when it is terminal.

    Used for monitors paused with ``close_reason="waiting_for_prerequisite"``:
    the prerequisite ticket (``info.depends_on_ticket_id``) is polled
    instead of the monitor's own ticket.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    board_url : str
        Board API base URL.
    info : SubsessionInfo
        The waiting monitor; ``depends_on_ticket_id`` must be set.

    Returns
    -------
    {"resume", "keep_paused"}
        ``"resume"`` when the prerequisite is in a merged-terminal state
        and the monitor was reopened, ``"keep_paused"`` otherwise
        (including a 404 on the prerequisite).

    """
    prereq_id = info.depends_on_ticket_id or ""
    prereq_state, _pr_url, prereq_http = await _query_ticket_state(
        board_url, prereq_id, info.id
    )
    if prereq_state is not None and prereq_state.lower() in (MERGED_TERMINAL_STATES):
        logger.info(
            "Watcher: subsession %s prerequisite ticket "
            "%s is terminal (%s) — resuming.",
            info.id,
            prereq_id,
            prereq_state,
        )
        await _resume_paused_monitor(env, info.id)
        return "resume"
    if prereq_http == 404:
        logger.warning(
            "Watcher: prerequisite ticket %s returned "
            "404 for subsession %s — prerequisite may "
            "have been deleted; keeping paused.",
            prereq_id,
            info.id,
        )
    else:
        logger.debug(
            "Watcher: subsession %s prerequisite ticket "
            "%s still in state %s — keeping paused.",
            info.id,
            prereq_id,
            prereq_state,
        )
    return "keep_paused"


def _reset_404_counter(
    env: SubsessionEnv, sub_id: str, checkpoint: dict[str, object]
) -> None:
    """Drop the consecutive-404 counter from *checkpoint* when present.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment (its registry persists the checkpoint).
    sub_id : str
        Subsession id owning *checkpoint*.
    checkpoint : dict[str, object]
        The monitor's checkpoint, mutated in place.

    """
    if "_watcher_404_count" in checkpoint:
        del checkpoint["_watcher_404_count"]
        env.registry.update_checkpoint(sub_id, checkpoint)


async def _try_resolve_stale_ticket_id(
    env: SubsessionEnv,
    board_url: str,
    info: SubsessionInfo,
    checkpoint: dict[str, object],
    ticket_id: str,
) -> bool:
    """Try to resolve a 404ing ticket id to a different, valid id.

    A 404 can mean the ticket id in the checkpoint is stale or
    paraphrased.  When the board resolves it to another id the
    checkpoint is updated, the 404 counter reset and the owner notified.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    board_url : str
        Board API base URL (used for the notification link).
    info : SubsessionInfo
        The monitor whose ticket returned 404.
    checkpoint : dict[str, object]
        The monitor's checkpoint, mutated in place on success.
    ticket_id : str
        The ticket id that returned 404.

    Returns
    -------
    bool
        ``True`` when the id was resolved and the monitor keeps
        monitoring, ``False`` when resolution failed or found nothing new.

    """
    resolved_id: str | None = None
    try:
        direct_repo_settings = getattr(env.settings, "direct_repo", None)
        if direct_repo_settings is not None:
            board = BoardClient(direct_repo_settings)
            resolved = await board.resolve_ticket_ids([ticket_id])
            resolved_id = resolved.get(ticket_id)
    except Exception:
        logger.debug(
            "Watcher: ticket ID resolution failed for "
            "monitor %s (ticket %s) — proceeding to close.",
            info.id,
            ticket_id,
        )

    if resolved_id is None or resolved_id == ticket_id:
        return False

    # The ticket ID was resolved to a different valid ID — update the
    # checkpoint and keep monitoring instead of closing.
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
                "title": f"Monitor ticket ID resolved: {info.title}",
                "body": (
                    f"Stale ticket ID {ticket_id} "
                    f"resolved to {resolved_id} — "
                    f"monitor {info.id[:8]} "
                    f"continuing."
                ),
                "urgency": "low",
                "link": f"{board_url.rstrip('/')}/tickets/{resolved_id}",
            },
        )
    return True


async def _close_deleted_ticket_monitor(
    env: SubsessionEnv,
    board_url: str,
    info: SubsessionInfo,
    ticket_id: str,
    count: int,
    state: _WatcherState,
) -> None:
    """Notify the owner and close a monitor whose ticket no longer exists.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    board_url : str
        Board API base URL (used for the notification link).
    info : SubsessionInfo
        The monitor to close with reason ``"ticket_deleted"``.
    ticket_id : str
        The ticket id that kept returning 404.
    count : int
        Number of consecutive 404s observed.
    state : _WatcherState
        Watcher de-duplication state (``ticket_deleted_notified``).

    """
    # -- escalation: notify the operator before closing --
    if env.event_sink is not None and info.id not in state.ticket_deleted_notified:
        state.ticket_deleted_notified.add(info.id)
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
                "link": f"{board_url.rstrip('/')}/tickets/{ticket_id}",
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
        "Watcher: closing monitor %s (ticket %s) after %d consecutive 404s.",
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
        await env.delivery.deliver_summary(closed, summary, "ticket_deleted")
    else:
        # Record was already CLOSED (e.g. legacy paused record) — update
        # close_reason in-place so find_paused_periodic stops returning
        # it on subsequent polls.
        info.close_reason = "ticket_deleted"
        info.summary = summary
        await env.delivery.deliver_summary(info, summary, "ticket_deleted")


async def _handle_ticket_404(
    env: SubsessionEnv,
    board_url: str,
    info: SubsessionInfo,
    checkpoint: dict[str, object],
    ticket_id: str,
    state: _WatcherState,
) -> _Ticket404Outcome:
    """Count a 404 for a monitor's ticket and close it past the threshold.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    board_url : str
        Board API base URL.
    info : SubsessionInfo
        The monitor whose ticket returned 404.
    checkpoint : dict[str, object]
        The monitor's checkpoint; its ``_watcher_404_count`` is updated.
    ticket_id : str
        The ticket id that returned 404.
    state : _WatcherState
        Watcher de-duplication state.

    Returns
    -------
    {"transient", "counted", "resolved", "closed"}
        ``"transient"`` when the board health check failed (the 404 is not
        counted), ``"counted"`` when the counter is still below
        ``_MAX_WATCHER_404_FAILURES``, ``"resolved"`` when the stale id
        was resolved to a new one, ``"closed"`` when the monitor was
        closed with reason ``"ticket_deleted"``.

    """
    # -- transient-outage guard: before counting a 404 toward the close
    #    threshold, verify the board API is actually reachable.  If the
    #    API itself is down (returning 404 for every endpoint), this is a
    #    transient outage, not a deleted ticket.
    if not await _check_api_healthy(board_url):
        logger.debug(
            "Watcher: ticket %s returned 404 but API "
            "health check failed — treating as "
            "transient outage (subsession %s).",
            ticket_id,
            info.id,
        )
        return "transient"

    # Ticket not found — track consecutive 404s and close the monitor
    # after the threshold to stop futile polling (the ticket was deleted
    # or the monitor was spawned with a stale/paraphrased ID).
    raw_count = checkpoint.get("_watcher_404_count")
    count = int(raw_count) if isinstance(raw_count, (int, float)) else 0
    count += 1
    checkpoint["_watcher_404_count"] = count
    env.registry.update_checkpoint(info.id, checkpoint)
    if count < _MAX_WATCHER_404_FAILURES:
        logger.info(
            "Watcher: ticket %s returned 404 for monitor %s (%d/%d consecutive).",
            ticket_id,
            info.id,
            count,
            _MAX_WATCHER_404_FAILURES,
        )
        return "counted"

    # -- escalation: try to resolve the ticket ID before closing so we
    #    don't discard a live ticket.
    if await _try_resolve_stale_ticket_id(env, board_url, info, checkpoint, ticket_id):
        return "resolved"
    await _close_deleted_ticket_monitor(env, board_url, info, ticket_id, count, state)
    return "closed"


async def _verify_tracked_pr_merged(
    env: SubsessionEnv,
    info: SubsessionInfo,
    ticket_id: str,
    current_state: str,
    pr_number: int,
    repo_full_name: str,
) -> bool:
    """Accept a terminal ticket state only when its tracked PR is merged.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    info : SubsessionInfo
        The paused monitor.
    ticket_id : str
        The monitor's ticket id (for logging).
    current_state : str
        The ticket's terminal state (for logging).
    pr_number : int
        Tracked PR number from the checkpoint.
    repo_full_name : str
        Tracked repository (``owner/repo``) from the checkpoint.

    Returns
    -------
    bool
        ``True`` when GitHub reports the PR merged; ``False`` when it is
        not merged or GitHub is unreachable (keep paused).

    """
    merged = await _check_pr_merged(
        env,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
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
            pr_number,
            repo_full_name,
        )
        return True
    if merged is None:
        # GitHub unreachable — defer; keep paused.
        logger.warning(
            "Watcher: subsession %s ticket %s terminal "
            "(%s) — could not verify PR #%d in %s "
            "(GitHub unreachable); keeping paused.",
            info.id,
            ticket_id,
            current_state,
            pr_number,
            repo_full_name,
        )
        return False
    # PR exists but is NOT merged — the terminal state is bogus.
    logger.warning(
        "Watcher: subsession %s ticket %s terminal "
        "(%s) but tracked PR #%d in %s is NOT "
        "merged — keeping paused (possible "
        "premature close with no code merged).",
        info.id,
        ticket_id,
        current_state,
        pr_number,
        repo_full_name,
    )
    return False


async def _search_terminal_pr_evidence(
    env: SubsessionEnv,
    board_url: str,
    info: SubsessionInfo,
    checkpoint: dict[str, object],
    ticket_id: str,
    current_state: str,
    repo_full_name: str,
) -> bool:
    """Look for PR evidence of a terminal ticket via GitHub search / history.

    The board's ``pr_url`` is null and the checkpoint carries no
    ``pr_number``, but it does carry a ``repo_full_name``: search that repo
    for PRs referencing the ticket (a PR may have been merged by a
    different agent), then fall back to the ticket history.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    board_url : str
        Board API base URL.
    info : SubsessionInfo
        The paused monitor.
    checkpoint : dict[str, object]
        The monitor's checkpoint; records the found PR on success.
    ticket_id : str
        The monitor's ticket id.
    current_state : str
        The ticket's terminal state (for logging).
    repo_full_name : str
        Repository (``owner/repo``) to search.

    Returns
    -------
    bool
        ``True`` when PR/merge evidence was found (resume), else ``False``.

    """
    prs = await _search_prs_referencing_ticket(
        env,
        repo_full_name=repo_full_name,
        ticket_id=ticket_id,
        sub_id=info.id,
    )
    if prs:
        # Found at least one PR referencing this ticket — pick the first
        # one and record it in the checkpoint so the merge guard can
        # verify it.
        pr_num = prs[0].get("number")
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
                repo_full_name,
            )
            checkpoint["pr_number"] = pr_num
            checkpoint["repo_full_name"] = repo_full_name
            env.registry.update_checkpoint(info.id, checkpoint)
            return True
        logger.warning(
            "Watcher: subsession %s ticket %s "
            "terminal (%s) — GitHub search "
            "returned PR without a number; "
            "keeping paused.",
            info.id,
            ticket_id,
            current_state,
        )
        return False
    if await _ticket_history_has_merge_evidence(board_url, ticket_id, info.id):
        logger.info(
            "Watcher: subsession %s ticket %s "
            "terminal (%s) — pr_url is null and "
            "GitHub search found no PRs, but the "
            "ticket history shows implementation "
            "and PR/merge evidence; resuming "
            "(avoiding false 'no PR' warning).",
            info.id,
            ticket_id,
            current_state,
        )
        return True
    logger.warning(
        "Watcher: subsession %s ticket %s "
        "terminal (%s) but pr_url is null, "
        "checkpoint has no pr_number, "
        "GitHub search found no PRs "
        "referencing the ticket in %s, and "
        "the ticket history shows no "
        "implementation/PR evidence — "
        "keeping paused (ticket was closed "
        "with no PR evidence).",
        info.id,
        ticket_id,
        current_state,
        repo_full_name,
    )
    return False


async def _terminal_state_has_pr_evidence(
    env: SubsessionEnv,
    board_url: str,
    info: SubsessionInfo,
    checkpoint: dict[str, object],
    ticket_id: str,
    current_state: str,
    pr_url: str | None,
) -> bool:
    """Guard against bogus closes: require PR evidence for a terminal state.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    board_url : str
        Board API base URL.
    info : SubsessionInfo
        The paused monitor.
    checkpoint : dict[str, object]
        The monitor's checkpoint.
    ticket_id : str
        The monitor's ticket id.
    current_state : str
        The ticket's (merged-terminal) state.
    pr_url : str or None
        The board ticket's ``pr_url``.

    Returns
    -------
    bool
        ``True`` when the terminal state is backed by PR evidence and the
        monitor may resume, ``False`` to keep it paused.

    """
    pr_number_raw = checkpoint.get("pr_number")
    repo_raw = checkpoint.get("repo_full_name")
    if (
        isinstance(pr_number_raw, int)
        and pr_number_raw > 0
        and isinstance(repo_raw, str)
        and repo_raw
    ):
        # Checkpoint tracks a PR — verify it was actually merged on
        # GitHub before accepting the terminal state.
        return await _verify_tracked_pr_merged(
            env, info, ticket_id, current_state, pr_number_raw, repo_raw
        )
    if pr_url:
        # Board ticket has a recorded pr_url — let the resume proceed;
        # the second-pass merge check will catch unmerged PRs on the next
        # cycle.
        logger.info(
            "Watcher: subsession %s ticket %s terminal "
            "(%s) — board pr_url is present; resuming.",
            info.id,
            ticket_id,
            current_state,
        )
        return True
    # No PR evidence at all — the ticket was closed with pr_url=null and
    # no checkpoint PR info.
    if isinstance(repo_raw, str) and repo_raw:
        return await _search_terminal_pr_evidence(
            env, board_url, info, checkpoint, ticket_id, current_state, repo_raw
        )
    if await _ticket_history_has_merge_evidence(board_url, ticket_id, info.id):
        logger.info(
            "Watcher: subsession %s ticket %s terminal "
            "(%s) — pr_url is null and checkpoint has "
            "no pr_number or repo_full_name, but the "
            "ticket history shows implementation and "
            "PR/merge evidence; resuming (avoiding "
            "false 'no PR' warning).",
            info.id,
            ticket_id,
            current_state,
        )
        return True
    logger.warning(
        "Watcher: subsession %s ticket %s terminal "
        "(%s) but pr_url is null, checkpoint has "
        "no pr_number or repo_full_name, and the "
        "ticket history shows no implementation/PR "
        "evidence — keeping paused (ticket was "
        "closed with no PR evidence).",
        info.id,
        ticket_id,
        current_state,
    )
    return False


async def _check_ticket_state_change(
    env: SubsessionEnv,
    board_url: str,
    info: SubsessionInfo,
    checkpoint: dict[str, object],
    ticket_id: str,
    current_state: str,
    pr_url: str | None,
) -> _MonitorDecision:
    """Resume a monitor when its ticket state differs from the last known one.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    board_url : str
        Board API base URL.
    info : SubsessionInfo
        The paused monitor.
    checkpoint : dict[str, object]
        The monitor's checkpoint (``last_known_state`` is read from it).
    ticket_id : str
        The monitor's ticket id.
    current_state : str
        The ticket state the board just reported.
    pr_url : str or None
        The board ticket's ``pr_url``.

    Returns
    -------
    {"resume", "keep_paused"}
        ``"resume"`` when the monitor was woken/reopened.

    """
    last_known = checkpoint.get("last_known_state")
    last_known_str = (
        (last_known if isinstance(last_known, str) else str(last_known))
        if last_known is not None
        else None
    )
    if last_known_str is None or current_state == last_known_str:
        logger.debug(
            "Watcher: subsession %s ticket %s still '%s' — keeping closed.",
            info.id,
            ticket_id,
            current_state,
        )
        return "keep_paused"

    # When the ticket transitioned to a terminal state (closed/done),
    # guard against bogus closes: without PR evidence keep the monitor
    # paused rather than silently accepting a terminal state with no
    # code merged.
    if current_state.lower() in MERGED_TERMINAL_STATES and not (
        await _terminal_state_has_pr_evidence(
            env, board_url, info, checkpoint, ticket_id, current_state, pr_url
        )
    ):
        return "keep_paused"

    logger.info(
        "Watcher: subsession %s ticket %s state changed from '%s' to '%s' — resuming.",
        info.id,
        ticket_id,
        last_known_str,
        current_state,
    )
    await _resume_or_wake(env, info, ticket_id, current_state)
    return "resume"


async def _check_paused_monitor(
    env: SubsessionEnv,
    board_url: str,
    info: SubsessionInfo,
    state: _WatcherState,
) -> _MonitorDecision:
    """First-pass check of one paused monitor against the board.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    board_url : str
        Board API base URL.
    info : SubsessionInfo
        The paused monitor.
    state : _WatcherState
        Watcher de-duplication state.

    Returns
    -------
    {"resume", "keep_paused", "skip"}
        The decision taken for this monitor this round.

    """
    checkpoint = info.checkpoint
    if checkpoint is None:
        return "skip"
    ticket_id = checkpoint.get("ticket_id")
    if not isinstance(ticket_id, str) or not ticket_id:
        return "skip"

    # -- prerequisite-wait branch: when a monitor was paused because its
    #    prerequisite ticket's monitor closed, poll the prerequisite (not
    #    this monitor's own ticket) and resume when it is terminal.
    if info.close_reason == "waiting_for_prerequisite" and info.depends_on_ticket_id:
        return await _check_prerequisite_wait(env, board_url, info)

    current_state, pr_url, http_status = await _query_ticket_state(
        board_url, ticket_id, info.id
    )
    if current_state is None:
        if http_status == 404:
            await _handle_ticket_404(env, board_url, info, checkpoint, ticket_id, state)
        else:
            # Non-404 error or successful response without state — reset
            # the 404 counter (the ticket may still exist; this was a
            # transient error).
            _reset_404_counter(env, info.id, checkpoint)
        # Mill unreachable or ticket gone — skip this monitor this round;
        # we'll try again on the next poll cycle.
        return "skip"
    # Successful response with state — reset the 404 counter.
    _reset_404_counter(env, info.id, checkpoint)
    return await _check_ticket_state_change(
        env, board_url, info, checkpoint, ticket_id, current_state, pr_url
    )


async def _alert_closed_unmerged_pr(
    env: SubsessionEnv,
    board_url: str,
    info: SubsessionInfo,
    ticket_id: str,
    pr_number: int,
    repo_full_name: str,
) -> None:
    """Alert the owner and file a follow-up for a PR closed without merging.

    No alarm is raised when the owning ticket is already terminal.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    board_url : str
        Board API base URL.
    info : SubsessionInfo
        The paused monitor tracking the PR.
    ticket_id : str
        The monitor's ticket id (may be empty).
    pr_number : int
        The closed PR's number.
    repo_full_name : str
        The PR's repository (``owner/repo``).

    """
    # Check whether the owning ticket is already in a terminal state —
    # if so, this is expected and we skip the alarm.
    ticket_terminal = False
    ticket_state = None
    if ticket_id:
        ticket_state = await _query_ticket_state(board_url, ticket_id, info.id)
        if ticket_state is not None:
            state_str = ticket_state[0]
            ticket_terminal = (
                state_str is not None and state_str.lower() in MERGED_TERMINAL_STATES
            )
    if ticket_terminal:
        logger.debug(
            "Watcher: subsession %s PR #%d "
            "in %s closed unmerged but "
            "ticket %s is terminal (%s) — "
            "no alarm.",
            info.id,
            pr_number,
            repo_full_name,
            ticket_id,
            ticket_state,
        )
        return

    pr_link = f"https://github.com/{repo_full_name}/pull/{pr_number}"
    if env.event_sink is not None:
        env.event_sink.publish(
            info.owner_session_id,
            {
                "type": SSE_NOTIFICATION_TYPE,
                "title": f"PR #{pr_number} closed without merging",
                "body": (
                    f"PR #{pr_number} in "
                    f"{repo_full_name} was closed "
                    f"without being merged. "
                    f"The changes may be lost. "
                    f"Ticket: {ticket_id}"
                ),
                "urgency": "high",
                "link": pr_link,
            },
        )
    # Try to open a follow-up ticket on the board so the operator sees it.
    try:
        board = BoardClient(env.settings.direct_repo)
        followup_id = await board.create_ticket(
            title=f"PR #{pr_number} in {repo_full_name} was closed without merging",
            description=(
                f"PR [#{pr_number}]({pr_link}) in "
                f"`{repo_full_name}` was closed "
                f"without being merged.\n\n"
                f"Original ticket: "
                f"{ticket_id}\n"
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
                "Watcher: created follow-up ticket %s for "
                "closed-unmerged PR #%d in %s.",
                followup_id,
                pr_number,
                repo_full_name,
            )
    except Exception:
        logger.debug(
            "Watcher: could not create "
            "follow-up ticket for "
            "closed-unmerged PR #%d "
            "in %s (board may be "
            "unreachable).",
            pr_number,
            repo_full_name,
        )


def _notify_merge_conflict(
    env: SubsessionEnv,
    info: SubsessionInfo,
    pr_data: Any,
    pr_number: int,
    repo_full_name: str,
    state: _WatcherState,
) -> None:
    """Flag a tracked PR's merge conflicts once per monitor.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    info : SubsessionInfo
        The paused monitor tracking the PR.
    pr_data : Any
        The GitHub PR object.
    pr_number : int
        The PR number.
    repo_full_name : str
        The PR's repository (``owner/repo``).
    state : _WatcherState
        Watcher de-duplication state (``notified_conditions``).

    """
    notified = state.notified_conditions.setdefault(info.id, set())
    if "merge_conflict" in notified:
        return
    notified.add("merge_conflict")
    logger.warning(
        "Watcher: subsession %s PR #%d in %s "
        "has MERGE CONFLICTS "
        "(mergeable=%s, mergeable_state=%s).",
        info.id,
        pr_number,
        repo_full_name,
        pr_data.get("mergeable"),
        pr_data.get("mergeable_state", "unknown"),
    )
    if env.event_sink is not None:
        env.event_sink.publish(
            info.owner_session_id,
            {
                "type": SSE_NOTIFICATION_TYPE,
                "title": f"PR #{pr_number} has merge conflicts",
                "body": (
                    f"PR #{pr_number} in "
                    f"{repo_full_name} has merge "
                    f"conflicts and cannot be "
                    f"merged.  Resolve the "
                    f"conflicts or rebase the "
                    f"branch."
                ),
                "urgency": "high",
                "link": f"https://github.com/{repo_full_name}/pull/{pr_number}",
            },
        )


async def _handle_merged_pr(
    env: SubsessionEnv,
    info: SubsessionInfo,
    pr_data: Any,
    pr_number: int,
    repo_full_name: str,
    state: _WatcherState,
) -> _MonitorDecision:
    """Resume a monitor whose tracked PR merged, once the image is published.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    info : SubsessionInfo
        The paused monitor tracking the PR.
    pr_data : Any
        The GitHub PR object (``merge_commit_sha`` is read from it).
    pr_number : int
        The merged PR's number.
    repo_full_name : str
        The PR's repository (``owner/repo``).
    state : _WatcherState
        Watcher de-duplication state (``ci_health_notified``).

    Returns
    -------
    {"resume", "keep_paused"}
        ``"keep_paused"`` while the image-publish workflow on the merge
        commit has not succeeded yet, ``"resume"`` once the monitor was
        reopened.

    """
    # -- image-publish verification gate: before resuming, verify the
    #    image-publish workflow on the merge commit succeeded so the
    #    monitor sees the running service actually contains the new code.
    merge_sha = pr_data.get("merge_commit_sha") or ""
    if isinstance(merge_sha, str) and merge_sha:
        pub_ok, pub_detail = await _verify_image_publish_on_main(
            env, repo_full_name, merge_sha, pr_number, info.id
        )
    else:
        pub_ok, pub_detail = (
            True,
            "no merge_commit_sha — skipping image-publish verification",
        )
    if not pub_ok:
        logger.info(
            "Watcher: subsession %s PR #%d in %s "
            "merged but image-publish not yet "
            "verified: %s",
            info.id,
            pr_number,
            repo_full_name,
            pub_detail,
        )
        if env.event_sink is not None and info.id not in state.ci_health_notified:
            state.ci_health_notified.add(info.id)
            env.event_sink.publish(
                info.owner_session_id,
                {
                    "type": (SSE_NOTIFICATION_TYPE),
                    "title": ("Image publish pending"),
                    "body": (
                        f"PR #{pr_number} in "
                        f"{repo_full_name} merged, but "
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
                    "link": f"https://github.com/{repo_full_name}/pull/{pr_number}",
                },
            )
        return "keep_paused"
    logger.info(
        "Watcher: subsession %s PR #%d in %s "
        "was merged — resuming "
        "(image-publish verified: %s).",
        info.id,
        pr_number,
        repo_full_name,
        pub_detail,
    )
    await _resume_merged_pr_monitor(env, info.id, pr_number, repo_full_name)
    return "resume"


async def _track_ci_run_id(
    env: SubsessionEnv,
    actions: Any,
    info: SubsessionInfo,
    checkpoint: dict[str, object],
    pr_number: int,
    repo_full_name: str,
    pr_branch: str,
    pr_head_sha: str,
) -> None:
    """Record the PR branch's latest CI run and warn about stale runs.

    Detects when a push lands on the PR branch but does NOT trigger a new
    CI run — the watcher would otherwise re-evaluate the same run on
    every poll and report stale results.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    actions : Any
        An ``ActionsClient`` instance.
    info : SubsessionInfo
        The paused monitor tracking the PR.
    checkpoint : dict[str, object]
        The monitor's checkpoint; ``last_ci_run_id`` /
        ``last_ci_head_sha`` are updated.
    pr_number : int
        The PR number.
    repo_full_name : str
        The PR's repository (``owner/repo``).
    pr_branch : str
        The PR's head branch.
    pr_head_sha : str
        The PR's head commit SHA.

    """
    latest_runs = await actions.list_workflow_runs(
        repo_full_name, branch=pr_branch, per_page=1
    )
    if not latest_runs:
        return
    latest_run = latest_runs[0]
    latest_run_id = latest_run.get("id")
    latest_run_head = latest_run.get("head_sha")
    last_ci_run_id = checkpoint.get("last_ci_run_id")
    last_ci_head_sha = checkpoint.get("last_ci_head_sha")

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
            pr_number,
            repo_full_name,
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
            pr_number,
            repo_full_name,
            latest_run_id,
            last_ci_head_sha,
            pr_head_sha,
        )

    checkpoint["last_ci_run_id"] = latest_run_id
    checkpoint["last_ci_head_sha"] = pr_head_sha
    env.registry.update_checkpoint(info.id, checkpoint)


async def _check_pr_ci_health(
    env: SubsessionEnv,
    direct_repo_settings: Any,
    info: SubsessionInfo,
    checkpoint: dict[str, object],
    pr_data: Any,
    pr_number: int,
    repo_full_name: str,
    state: _WatcherState,
) -> None:
    """Track CI runs and detect zero-job CI infrastructure failures for a PR.

    Errors are logged at debug level and swallowed.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    direct_repo_settings : Any
        The ``direct_repo`` settings used to build the ``ActionsClient``.
    info : SubsessionInfo
        The paused monitor tracking the PR.
    checkpoint : dict[str, object]
        The monitor's checkpoint.
    pr_data : Any
        The GitHub PR object (``head.ref`` / ``head.sha`` are read).
    pr_number : int
        The PR number.
    repo_full_name : str
        The PR's repository (``owner/repo``).
    state : _WatcherState
        Watcher de-duplication state (``ci_health_notified``).

    """
    pr_branch = pr_data.get("head", {}).get("ref")
    pr_head_sha = pr_data.get("head", {}).get("sha")
    if not isinstance(pr_branch, str) or not pr_branch:
        return
    try:
        from robotsix_chat.repo.direct import actions_client

        actions = actions_client.ActionsClient(direct_repo_settings)

        # -- run-ID tracking: detect stale CI runs --
        if isinstance(pr_head_sha, str) and pr_head_sha:
            await _track_ci_run_id(
                env,
                actions,
                info,
                checkpoint,
                pr_number,
                repo_full_name,
                pr_branch,
                pr_head_sha,
            )

        zero_job_msg = await actions.check_latest_run_for_zero_jobs(
            repo_full_name, pr_branch
        )
        if zero_job_msg is not None:
            logger.critical(zero_job_msg)
            if env.event_sink is not None and info.id not in state.ci_health_notified:
                state.ci_health_notified.add(info.id)
                env.event_sink.publish(
                    info.owner_session_id,
                    {
                        "type": SSE_NOTIFICATION_TYPE,
                        "title": "CI infrastructure failure detected",
                        "body": zero_job_msg,
                        "urgency": "high",
                        "link": (
                            f"https://github.com/{repo_full_name}/pull/{pr_number}"
                        ),
                    },
                )
    except Exception:
        logger.debug(
            "Watcher: could not check CI health for "
            "PR #%d in %s (subsession %s) — skipping.",
            pr_number,
            repo_full_name,
            info.id,
        )


async def _check_tracked_pr(
    env: SubsessionEnv,
    board_url: str,
    gh_client: Any,
    direct_repo_settings: Any,
    info: SubsessionInfo,
    state: _WatcherState,
) -> _MonitorDecision:
    """Second-pass check of one paused monitor's tracked GitHub PR.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    board_url : str
        Board API base URL.
    gh_client : Any
        A ``DirectRepoClient`` with a valid token.
    direct_repo_settings : Any
        The ``direct_repo`` settings.
    info : SubsessionInfo
        The paused monitor.
    state : _WatcherState
        Watcher de-duplication state.

    Returns
    -------
    {"resume", "keep_paused", "skip"}
        ``"skip"`` when the checkpoint tracks no PR or GitHub failed,
        ``"resume"`` when the PR merged (image published) or was closed
        unmerged, ``"keep_paused"`` otherwise.

    """
    checkpoint = info.checkpoint
    if checkpoint is None:
        return "skip"
    pr_number = checkpoint.get("pr_number")
    if not isinstance(pr_number, int) or pr_number <= 0:
        return "skip"
    repo_full_name = checkpoint.get("repo_full_name")
    if not isinstance(repo_full_name, str) or not repo_full_name:
        return "skip"

    try:
        pr_data = await gh_client.get_pr(
            repo_full_name=repo_full_name, pr_number=pr_number
        )
    except Exception:
        logger.debug(
            "Watcher: could not fetch PR #%d in %s (subsession %s) — skipping.",
            pr_number,
            repo_full_name,
            info.id,
        )
        return "skip"

    merged = pr_data.get("merged")
    pr_state = pr_data.get("state")

    # --- PR closed without merging ---
    # Detect when a PR is closed (not open) but was never merged — the
    # change was lost silently.
    if pr_state == "closed" and merged is not True:
        ticket_id_raw = checkpoint.get("ticket_id")
        ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
        notified = state.notified_conditions.setdefault(info.id, set())
        if "closed_unmerged" not in notified:
            notified.add("closed_unmerged")
            logger.warning(
                "Watcher: subsession %s PR #%d in %s "
                "was CLOSED WITHOUT MERGING "
                "(state=%s, merged=%s).",
                info.id,
                pr_number,
                repo_full_name,
                pr_state,
                merged,
            )
            await _alert_closed_unmerged_pr(
                env, board_url, info, ticket_id, pr_number, repo_full_name
            )
        # Resume the monitor so it can report the failure to the user and
        # stop polling.
        await _resume_or_wake(
            env, info, ticket_id, f"PR #{pr_number} closed without merging"
        )
        return "resume"

    # --- Merge conflict detection ---
    # Flag merge conflicts as soon as they are detected instead of
    # waiting for a monitor report.
    if pr_data.get("mergeable") is False:
        _notify_merge_conflict(env, info, pr_data, pr_number, repo_full_name, state)

    if merged is True:
        return await _handle_merged_pr(
            env, info, pr_data, pr_number, repo_full_name, state
        )

    # -- CI run tracking and infrastructure health check ----
    await _check_pr_ci_health(
        env,
        direct_repo_settings,
        info,
        checkpoint,
        pr_data,
        pr_number,
        repo_full_name,
        state,
    )

    # When CI is failing on a paused monitor's PR, keep the monitor
    # paused rather than resuming it — the auto-pause delivery already
    # escalated to the operator.  Resuming would only create a wasteful
    # pause-resume-pause loop since the monitor's agent cannot fix
    # source-code or dependency issues on its own.
    logger.debug(
        "Watcher: subsession %s PR #%d in %s not yet merged — keeping paused.",
        info.id,
        pr_number,
        repo_full_name,
    )
    return "keep_paused"


async def _check_tracked_pr_merges(
    env: SubsessionEnv,
    board_url: str,
    state: _WatcherState,
) -> None:
    """Second pass: check GitHub PR status for paused monitors tracking a PR.

    This catches merges that the mill ticket API may not reflect (e.g.
    when a PR is merged but the ticket state is not automatically updated
    by the board).  Does nothing unless ``direct_repo`` is enabled and a
    GitHub token can be obtained.

    Parameters
    ----------
    env : SubsessionEnv
        Subsession environment.
    board_url : str
        Board API base URL.
    state : _WatcherState
        Watcher de-duplication state.

    """
    paused_after_mill = env.registry.find_paused_periodic()
    if not paused_after_mill:
        return
    direct_repo_settings = getattr(env.settings, "direct_repo", None)
    if direct_repo_settings is None or not getattr(
        direct_repo_settings, "enabled", False
    ):
        return
    try:
        from robotsix_chat.repo.direct.client import DirectRepoClient

        gh_client = DirectRepoClient(direct_repo_settings)
        token = await gh_client._token()
    except Exception:
        logger.debug(
            "Watcher: could not create GitHub client — skipping PR merge checks."
        )
        return
    if token is None:
        return

    for info in paused_after_mill:
        await _check_tracked_pr(
            env, board_url, gh_client, direct_repo_settings, info, state
        )


async def watch_paused_monitors(env: SubsessionEnv) -> None:
    """Background task: poll auto-paused/timeout monitors and resume on state change.

    Runs forever — cancelled on server shutdown.  Must be started as an
    asyncio task after the server is ready (e.g. via the Starlette
    lifespan ``startup`` phase).

    Each poll cycle runs two passes over the paused monitors: the first
    checks board ticket state (:func:`_check_paused_monitor`), the second
    checks tracked GitHub PRs (:func:`_check_tracked_pr_merges`).
    """
    config = _resolve_watcher_config(env)
    if config is None:
        return
    board_url, poll_interval = config

    logger.info(
        "Watcher: started (poll interval %.0f s, board_url=%s)",
        poll_interval,
        board_url,
    )

    # Notification de-duplication across poll cycles.
    state = _WatcherState()

    while True:
        try:
            paused = env.registry.find_paused_periodic()
            if not paused:
                await asyncio.sleep(_IDLE_POLL_INTERVAL_SECONDS)
                continue

            for info in paused:
                await _check_paused_monitor(env, board_url, info, state)

            await _check_tracked_pr_merges(env, board_url, state)

            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            logger.info("Watcher: cancelled — shutting down.")
            raise
        except Exception:
            logger.exception("Watcher: unexpected error in poll loop — retrying.")
            await asyncio.sleep(poll_interval)

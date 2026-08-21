"""Tests for the paused-monitor watcher (``watch_paused_monitors``)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE
from robotsix_chat.subsessions import (
    SubsessionEnv,
    SubsessionInfo,
    SubsessionKind,
    SubsessionStatus,
)
from robotsix_chat.subsessions.watcher import (
    _query_ticket_state,
    _resume_merged_pr_monitor,
    _resume_paused_monitor,
    watch_paused_monitors,
)
from tests.common.subsession_fakes import (
    RecordingSink,
    build_env,
    make_settings,
)

OWNER = "sess-main"


# -- _query_ticket_state ---------------------------------------------------


@pytest.mark.asyncio
async def test_query_ticket_state_returns_state_string() -> None:
    """Returns the 'state' field from a valid ticket response."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"state": "in_progress"}
    mock_response.raise_for_status.return_value = None

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        state, pr_url, http_status = await _query_ticket_state(
            "https://mill.example.com", "TICKET-1", "sub-1"
        )

    assert state == "in_progress"
    assert pr_url is None
    assert http_status is None


@pytest.mark.asyncio
async def test_query_ticket_state_returns_pr_url() -> None:
    """Returns pr_url when present in the ticket response."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "state": "closed",
        "pr_url": "https://github.com/owner/repo/pull/42",
    }
    mock_response.raise_for_status.return_value = None

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        state, pr_url, http_status = await _query_ticket_state(
            "https://mill.example.com", "TICKET-1", "sub-1"
        )

    assert state == "closed"
    assert pr_url == "https://github.com/owner/repo/pull/42"
    assert http_status is None


@pytest.mark.asyncio
async def test_query_ticket_state_returns_none_on_http_error() -> None:
    """Returns (None, None, status_code) when the mill returns a 4xx/5xx status."""
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "not found", request=MagicMock(), response=MagicMock(status_code=404)
    )

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        state, pr_url, http_status = await _query_ticket_state(
            "https://mill.example.com", "TICKET-1", "sub-1"
        )

    assert state is None
    assert pr_url is None
    assert http_status == 404


@pytest.mark.asyncio
async def test_query_ticket_state_returns_none_on_timeout() -> None:
    """Returns (None, None, None) when the mill times out."""
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        state, pr_url, http_status = await _query_ticket_state(
            "https://mill.example.com", "TICKET-1", "sub-1"
        )

    assert state is None
    assert pr_url is None
    assert http_status is None


@pytest.mark.asyncio
async def test_query_ticket_state_returns_none_on_connect_error() -> None:
    """Returns (None, None, None) when the mill connection is refused.

    Verifies that ``ConnectError`` is caught by its dedicated handler,
    not swallowed by the generic ``except Exception`` block — the
    separate handler was once merged into the ``HTTPStatusError``
    handler by mistake, leaving dead code.  This test guards against
    that regression.
    """
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        state, pr_url, http_status = await _query_ticket_state(
            "https://mill.example.com", "TICKET-1", "sub-1"
        )

    assert state is None
    assert pr_url is None
    assert http_status is None


@pytest.mark.asyncio
async def test_query_ticket_state_returns_none_on_os_error() -> None:
    """Returns (None, None, None) when a low-level OS error occurs."""
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=OSError("socket error"))

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        state, pr_url, http_status = await _query_ticket_state(
            "https://mill.example.com", "TICKET-1", "sub-1"
        )

    assert state is None
    assert pr_url is None
    assert http_status is None


@pytest.mark.asyncio
async def test_query_ticket_state_returns_none_on_bad_url() -> None:
    """Returns (None, None, None) when the board URL is malformed."""
    state, pr_url, http_status = await _query_ticket_state(
        "not a url", "TICKET-1", "sub-1"
    )
    assert state is None
    assert pr_url is None
    assert http_status is None


# -- _resume_paused_monitor ------------------------------------------------


@pytest.mark.asyncio
async def test_resume_paused_monitor_reopens_and_spawns_worker() -> None:
    """_resume_paused_monitor reopens, spawns a worker, and emits a notification."""
    sink = RecordingSink()
    env = build_env(event_sink=sink)
    info = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="ticket monitor",
        prompt="monitor",
        model_level=3,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "T-1", "last_known_state": "open"},
    )
    env.registry.mark_closed(
        info.id, summary="paused", reason="paused", closed_by="system"
    )

    await _resume_paused_monitor(env, info.id)

    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.RUNNING
    assert reopened.close_reason is None

    # A worker task should have been spawned.
    assert info.id in env.registry._running

    # Assert an SSE notification was published for the resume.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    assert len(notifications) == 1
    _sid, frame = notifications[0]
    assert frame["title"] == f"Monitor resumed: {info.title}"
    assert "ticket state change" in str(frame["body"])
    assert frame["urgency"] == "low"
    assert frame["link"] == "T-1"


@pytest.mark.asyncio
async def test_resume_paused_monitor_noop_for_unknown_id() -> None:
    """``_resume_paused_monitor`` is a no-op for unknown subsessions."""
    env = build_env()
    await _resume_paused_monitor(env, "nonexistent")
    # Should not raise or create any tasks.
    assert len(env.registry._running) == 0


@pytest.mark.asyncio
async def test_resume_paused_monitor_noop_for_non_paused() -> None:
    """``_resume_paused_monitor`` does nothing when the sub wasn't paused."""
    env = build_env()
    info = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="ticket monitor",
        prompt="monitor",
        model_level=3,
        interval_seconds=60.0,
    )
    env.registry.mark_closed(
        info.id, summary="done", reason="completed", closed_by="system"
    )

    await _resume_paused_monitor(env, info.id)

    # Still CLOSED.
    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.CLOSED


# -- _resume_merged_pr_monitor -------------------------------------------


@pytest.mark.asyncio
async def test_resume_merged_pr_monitor_emits_notification() -> None:
    """``_resume_merged_pr_monitor`` reopens the record and emits a notification."""
    sink = RecordingSink()
    env = build_env(event_sink=sink)
    info = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="pr monitor",
        prompt="monitor",
        model_level=3,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "T-2", "pr_number": 42, "repo_full_name": "org/repo"},
    )
    env.registry.mark_closed(
        info.id, summary="paused", reason="paused", closed_by="system"
    )

    await _resume_merged_pr_monitor(
        env, info.id, pr_number=42, repo_full_name="org/repo"
    )

    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.RUNNING
    assert reopened.close_reason is None

    # Assert an SSE notification was published for the PR-merge resume.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    assert len(notifications) == 1
    _sid, frame = notifications[0]
    assert frame["title"] == f"Monitor resumed: {info.title}"
    assert "PR #42 in org/repo was merged" in str(frame["body"])
    assert frame["urgency"] == "low"
    assert frame["link"] == "https://github.com/org/repo/pull/42"


# -- watch_paused_monitors -------------------------------------------------


def _make_paused_monitor(env, ticket_id="TICKET-1", last_known="open", title="mon"):
    """Create a paused periodic monitor in *env*'s registry."""
    info = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title=title,
        prompt="monitor",
        model_level=3,
        interval_seconds=60.0,
        checkpoint={"ticket_id": ticket_id, "last_known_state": last_known},
    )
    env.registry.mark_closed(
        info.id, summary="paused", reason="paused", closed_by="system"
    )
    return info


def _mock_ticket_client(*, state="open", pr_url=None):
    """Build a mock ``httpx.AsyncClient`` returning the given ticket state.

    When *pr_url* is provided it is included in the JSON response;
    when ``None`` (the default) the key is absent.
    """
    body: dict[str, Any] = {"state": state}
    if pr_url is not None:
        body["pr_url"] = pr_url

    mock_response = MagicMock()
    mock_response.json.return_value = body
    mock_response.raise_for_status.return_value = None

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=mock_instance)


@pytest.mark.asyncio
async def test_watcher_resumes_when_state_changes() -> None:
    """The watcher resumes a paused monitor on state change and emits a notification."""
    settings = make_settings()
    settings.direct_repo = type(
        "_ns", (), {"board_api_base_url": "https://mill.example.com"}
    )()
    # Short poll interval so the test doesn"t hang.
    settings.subsessions.paused_monitor_poll_interval_seconds = 0.01
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    info = _make_paused_monitor(env, ticket_id="T-1", last_known="open")

    # Mock the mill to return a changed state.
    mock_client = _mock_ticket_client(state="in_progress")

    # Run the watcher for a couple of ticks to let it detect the change.
    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with patch("httpx.AsyncClient", mock_client):
        # Give the watcher time to poll and resume.
        await asyncio.sleep(0.15)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # The monitor should now be active (RUNNING or SLEEPING).
    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.is_active

    # Assert an SSE notification was published for the state-change resume.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    assert len(notifications) == 1
    _sid, frame = notifications[0]
    assert "Monitor resumed:" in str(frame["title"])
    assert "ticket state change" in str(frame["body"])


@pytest.mark.asyncio
async def test_watcher_keeps_paused_when_state_unchanged() -> None:
    """The watcher leaves a monitor paused when the ticket state is unchanged."""
    settings = make_settings()
    settings.direct_repo = type(
        "_ns", (), {"board_api_base_url": "https://mill.example.com"}
    )()
    settings.subsessions.paused_monitor_poll_interval_seconds = 0.01
    env = build_env(settings=settings)

    info = _make_paused_monitor(env, ticket_id="T-1", last_known="open")

    # Mock the mill to return the SAME state.
    mock_client = _mock_ticket_client(state="open")

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with patch("httpx.AsyncClient", mock_client):
        await asyncio.sleep(0.15)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # The monitor should still be CLOSED.
    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.CLOSED


@pytest.mark.asyncio
async def test_watcher_skips_when_no_board_url() -> None:
    """The watcher returns immediately when board_api_base_url is not configured."""
    settings = make_settings()
    settings.direct_repo = type("_ns", (), {"board_api_base_url": ""})()
    env = build_env(settings=settings)

    _make_paused_monitor(env)

    # The watcher should return immediately (not loop).
    task = asyncio.create_task(watch_paused_monitors(env))
    await asyncio.wait_for(task, timeout=0.5)

    # The monitor should still be paused.
    paused = env.registry.find_paused_periodic()
    assert len(paused) == 1


@pytest.mark.asyncio
async def test_watcher_exits_when_poll_interval_zero() -> None:
    """The watcher returns immediately when poll interval is set to 0."""
    settings = make_settings()
    settings.direct_repo = type(
        "_ns", (), {"board_api_base_url": "https://mill.example.com"}
    )()
    settings.subsessions.paused_monitor_poll_interval_seconds = 0
    env = build_env(settings=settings)

    _make_paused_monitor(env)

    # The watcher should return immediately (not loop).
    task = asyncio.create_task(watch_paused_monitors(env))
    await asyncio.wait_for(task, timeout=0.5)

    # The monitor should still be paused.
    paused = env.registry.find_paused_periodic()
    assert len(paused) == 1


@pytest.mark.asyncio
async def test_watcher_handles_mill_unreachable_gracefully() -> None:
    """The watcher does not crash when the mill is unreachable."""
    settings = make_settings()
    settings.direct_repo = type(
        "_ns", (), {"board_api_base_url": "https://mill.example.com"}
    )()
    settings.subsessions.paused_monitor_poll_interval_seconds = 0.01
    env = build_env(settings=settings)

    _make_paused_monitor(env, ticket_id="T-1", last_known="open")

    # Mock the mill to raise a connection error.
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        await asyncio.sleep(0.15)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # The monitor should still be paused (no crash, no resume).
    paused = env.registry.find_paused_periodic()
    assert len(paused) == 1


@pytest.mark.asyncio
async def test_watcher_resumes_human_approval_timeout_when_state_changes() -> None:
    """Watcher resumes a ``human_approval_timeout`` monitor on state change."""
    settings = make_settings()
    settings.direct_repo = type(
        "_ns", (), {"board_api_base_url": "https://mill.example.com"}
    )()
    settings.subsessions.paused_monitor_poll_interval_seconds = 0.01
    env = build_env(settings=settings)

    info = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="monitor",
        model_level=3,
        interval_seconds=60.0,
        checkpoint={
            "ticket_id": "T-1",
            "last_known_state": "human_issue_approval",
            "human_approval_since": 999999.0,
        },
    )
    env.registry.mark_closed(
        info.id,
        summary="human approval timeout",
        reason="human_approval_timeout",
        closed_by="system",
    )

    # Mock the mill to return a changed state (e.g. PR merged, ticket
    # moved to in_progress).
    mock_client = _mock_ticket_client(state="in_progress")

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with patch("httpx.AsyncClient", mock_client):
        await asyncio.sleep(0.15)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.is_active
    # human_approval_since must be cleared on reopen.
    assert reopened.checkpoint is not None
    assert "human_approval_since" not in reopened.checkpoint


# -- PR-merge pass helpers --------------------------------------------------


def _make_paused_monitor_with_pr(
    env: SubsessionEnv,
    *,
    ticket_id: str = "T-PR",
    last_known: str = "open",
    pr_number: int = 42,
    repo_full_name: str = "org/repo",
    title: str = "pr monitor",
) -> SubsessionInfo:
    """Create a paused periodic monitor with a PR checkpoint."""
    info = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title=title,
        prompt="monitor",
        model_level=3,
        interval_seconds=60.0,
        checkpoint={
            "ticket_id": ticket_id,
            "last_known_state": last_known,
            "pr_number": pr_number,
            "repo_full_name": repo_full_name,
        },
    )
    env.registry.mark_closed(
        info.id, summary="paused", reason="paused", closed_by="system"
    )
    return info


def _settings_with_direct_repo(**direct_repo_kw: Any) -> SimpleNamespace:
    """Return settings with ``direct_repo`` enabled and a board URL set."""
    settings = make_settings()
    settings.subsessions.paused_monitor_poll_interval_seconds = 0.01
    kwargs: dict[str, Any] = dict(
        board_api_base_url="https://mill.example.com",
        enabled=True,
        github_api_base_url="https://api.github.com",
        github_app_id="app-id",
        github_app_private_key="key",  # pragma: allowlist secret
        github_app_installation_id="inst-id",
        board_api_token="token",  # pragma: allowlist secret
        timeout=10.0,
    )
    kwargs.update(direct_repo_kw)
    settings.direct_repo = SimpleNamespace(**kwargs)
    return settings


def _mock_direct_repo_client(
    *,
    merged: bool | None = None,
    state: str = "open",
    mergeable: bool = True,
    get_pr_side_effect: Any = None,
) -> MagicMock:
    """Build a mock ``DirectRepoClient`` with a valid token.

    The ``get_pr`` return value is configurable via *merged*, *state*,
    *mergeable*, or *get_pr_side_effect* (for exception cases).
    """
    mock = MagicMock()
    mock._token = AsyncMock(return_value="fake-token")
    if get_pr_side_effect is not None:
        mock.get_pr = AsyncMock(side_effect=get_pr_side_effect)
    else:
        mock.get_pr = AsyncMock(
            return_value={
                "merged": merged,
                "state": state,
                "mergeable": mergeable,
                "mergeable_state": "clean" if mergeable else "dirty",
                "title": "Test PR",
                "html_url": "https://github.com/org/repo/pull/42",
            }
        )
    return mock


# -- CI health check (zero-job detection) ----------------------------------


@pytest.mark.asyncio
async def test_watcher_emits_high_urgency_on_zero_job_ci() -> None:
    """The watcher emits a high-urgency notification when CI has zero jobs."""
    settings = _settings_with_direct_repo()
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    info = _make_paused_monitor_with_pr(env)

    # Mock the mill to return unchanged state (first pass no-op).
    mock_mill = _mock_ticket_client(state="open")

    # Mock DirectRepoClient to return an unmerged PR with a head ref.
    mock_pr_data = {
        "merged": False,
        "head": {"ref": "feature-branch"},
    }
    mock_gh_client = MagicMock()
    mock_gh_client.get_pr = AsyncMock(return_value=mock_pr_data)
    mock_gh_client._token = AsyncMock(return_value="fake-token")

    # Mock ActionsClient to detect zero jobs.
    mock_actions = MagicMock()
    mock_actions.check_latest_run_for_zero_jobs = AsyncMock(
        return_value=(
            "CI INFRASTRUCTURE FAILURE: workflow run 'CI' (id 1) on "
            "org/repo branch 'feature-branch' has ZERO jobs"
        )
    )

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with (
        patch(
            "robotsix_chat.repo.direct.client.DirectRepoClient",
            return_value=mock_gh_client,
        ),
        patch(
            "robotsix_chat.repo.direct.actions_client.ActionsClient",
            return_value=mock_actions,
        ),
        patch("httpx.AsyncClient", mock_mill),
    ):
        await asyncio.sleep(0.15)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # Monitor should still be paused (not resumed).
    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.CLOSED

    # A high-urgency notification should have been emitted.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    ci_notifications = [n for n in notifications if n[1].get("urgency") == "high"]
    assert len(ci_notifications) == 1
    _sid, frame = ci_notifications[0]
    assert "CI infrastructure failure" in str(frame["title"])
    assert "ZERO jobs" in str(frame["body"])


@pytest.mark.asyncio
async def test_watcher_no_ci_notification_when_jobs_present() -> None:
    """No CI notification is emitted when the workflow has jobs (normal CI)."""
    settings = _settings_with_direct_repo()
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    _make_paused_monitor_with_pr(env)

    mock_mill = _mock_ticket_client(state="open")

    mock_pr_data = {
        "merged": False,
        "head": {"ref": "feature-branch"},
    }
    mock_gh_client = MagicMock()
    mock_gh_client.get_pr = AsyncMock(return_value=mock_pr_data)
    mock_gh_client._token = AsyncMock(return_value="fake-token")

    # ActionsClient returns None (no zero-job issue).
    mock_actions = MagicMock()
    mock_actions.check_latest_run_for_zero_jobs = AsyncMock(return_value=None)

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with (
        patch(
            "robotsix_chat.repo.direct.client.DirectRepoClient",
            return_value=mock_gh_client,
        ),
        patch(
            "robotsix_chat.repo.direct.actions_client.ActionsClient",
            return_value=mock_actions,
        ),
        patch("httpx.AsyncClient", mock_mill),
    ):
        await asyncio.sleep(0.15)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # No high-urgency notifications.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    ci_notifications = [n for n in notifications if n[1].get("urgency") == "high"]
    assert len(ci_notifications) == 0


@pytest.mark.asyncio
async def test_watcher_ci_health_check_graceful_on_actions_error() -> None:
    """The watcher does not crash when ActionsClient raises an error."""
    settings = _settings_with_direct_repo()
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    _make_paused_monitor_with_pr(env)

    mock_mill = _mock_ticket_client(state="open")

    mock_pr_data = {
        "merged": False,
        "head": {"ref": "feature-branch"},
    }
    mock_gh_client = MagicMock()
    mock_gh_client.get_pr = AsyncMock(return_value=mock_pr_data)
    mock_gh_client._token = AsyncMock(return_value="fake-token")

    # ActionsClient constructor raises.
    with (
        patch(
            "robotsix_chat.repo.direct.client.DirectRepoClient",
            return_value=mock_gh_client,
        ),
        patch(
            "robotsix_chat.repo.direct.actions_client.ActionsClient",
            side_effect=RuntimeError("no network"),
        ),
        patch("httpx.AsyncClient", mock_mill),
    ):
        watcher_task = asyncio.create_task(watch_paused_monitors(env))
        await asyncio.sleep(0.15)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # No crash, no high-urgency notifications.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    ci_notifications = [n for n in notifications if n[1].get("urgency") == "high"]
    assert len(ci_notifications) == 0


# -- PR-merge pass: resume on merged PR ------------------------------------


@pytest.mark.asyncio
async def test_watcher_resumes_when_pr_merged() -> None:
    """The watcher resumes a paused monitor when its tracked PR is merged."""
    settings = _settings_with_direct_repo()
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    info = _make_paused_monitor_with_pr(env, ticket_id="T-1", last_known="open")

    # Ticket state unchanged → first pass keeps it paused.
    mock_ticket_client = _mock_ticket_client(state="open")
    # PR is merged → second pass resumes.
    mock_gh = _mock_direct_repo_client(merged=True)

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with (
        patch("httpx.AsyncClient", mock_ticket_client),
        patch(
            "robotsix_chat.repo.direct.client.DirectRepoClient",
            return_value=mock_gh,
        ),
    ):
        await asyncio.sleep(0.2)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.is_active

    # Assert a PR-merge notification was published.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    assert len(notifications) == 1
    _sid, frame = notifications[0]
    assert "Monitor resumed:" in str(frame["title"])
    assert "PR #42 in org/repo was merged" in str(frame["body"])


# -- PR-merge pass: keep paused when PR not merged -------------------------


@pytest.mark.asyncio
async def test_watcher_keeps_paused_when_pr_not_merged() -> None:
    """The watcher leaves a monitor paused when its tracked PR is not merged."""
    settings = _settings_with_direct_repo()
    env = build_env(settings=settings)

    info = _make_paused_monitor_with_pr(env, ticket_id="T-1", last_known="open")

    # Ticket state unchanged → first pass keeps it paused.
    mock_ticket_client = _mock_ticket_client(state="open")
    # PR not merged → second pass keeps it paused.
    mock_gh = _mock_direct_repo_client(merged=False)

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with (
        patch("httpx.AsyncClient", mock_ticket_client),
        patch(
            "robotsix_chat.repo.direct.client.DirectRepoClient",
            return_value=mock_gh,
        ),
    ):
        await asyncio.sleep(0.2)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.CLOSED


# -- PR-merge pass: get_pr() exception path ---------------------------------


@pytest.mark.asyncio
async def test_watcher_handles_get_pr_exception_gracefully() -> None:
    """The watcher skips a monitor when ``get_pr()`` raises an exception."""
    settings = _settings_with_direct_repo()
    env = build_env(settings=settings)

    info = _make_paused_monitor_with_pr(env, ticket_id="T-1", last_known="open")

    # Ticket state unchanged → first pass keeps it paused.
    mock_ticket_client = _mock_ticket_client(state="open")
    # get_pr raises → caught, debug logged, monitor skipped.
    mock_gh = _mock_direct_repo_client(
        get_pr_side_effect=RuntimeError("API unavailable")
    )

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with (
        patch("httpx.AsyncClient", mock_ticket_client),
        patch(
            "robotsix_chat.repo.direct.client.DirectRepoClient",
            return_value=mock_gh,
        ),
    ):
        await asyncio.sleep(0.2)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # Monitor should still be paused — exception is logged, not raised.
    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.CLOSED


# -- PR closed-unmerged / merge-conflict detection --------------------------


@pytest.mark.asyncio
async def test_watcher_notifies_on_pr_closed_unmerged() -> None:
    """When a tracked PR is closed without merging, the watcher notifies.

    It publishes a high-urgency SSE notification, tries to create a follow-up
    ticket on the board, and resumes the monitor.
    """
    settings = _settings_with_direct_repo(
        board_api_token=type("_st", (), {"get_secret_value": lambda: ""})(),
        timeout=10.0,
    )
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    info = _make_paused_monitor_with_pr(env, ticket_id="T-PR", last_known="in_progress")

    # Mock the mill to return a non-terminal ticket state.
    mock_ticket_client = _mock_ticket_client(state="in_progress")
    # Mock the DirectRepoClient to return a closed-unmerged PR.
    mock_gh = _mock_direct_repo_client(merged=False, state="closed")

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with (
        patch(
            "robotsix_chat.repo.direct.client.DirectRepoClient",
            MagicMock(return_value=mock_gh),
        ),
        patch("httpx.AsyncClient", mock_ticket_client),
        patch(
            "robotsix_chat.subsessions.watcher.BoardClient",
            MagicMock(
                return_value=MagicMock(
                    create_ticket=AsyncMock(return_value="FOLLOWUP-1")
                )
            ),
        ),
    ):
        await asyncio.sleep(0.20)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # The monitor should be resumed.
    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.is_active

    # An SSE notification should have been published with high urgency.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    assert len(notifications) >= 1
    notification_bodies = [str(frame["body"]) for _sid, frame in notifications]
    closed_unmerged_notification = [
        b for b in notification_bodies if "closed without being merged" in b
    ]
    assert len(closed_unmerged_notification) == 1

    urgency_values = [
        frame["urgency"]
        for _sid, frame in notifications
        if "closed without being merged" in str(frame["body"])
    ]
    assert urgency_values == ["high"]


@pytest.mark.asyncio
async def test_watcher_no_alarm_on_closed_unmerged_terminal_ticket() -> None:
    """When the PR is closed unmerged but the ticket is terminal, no alarm fires.

    The watcher logs a debug message but does NOT publish an SSE
    notification or create a follow-up ticket.
    """
    settings = _settings_with_direct_repo(
        board_api_token=type("_st", (), {"get_secret_value": lambda: ""})(),
        timeout=10.0,
    )
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    info = _make_paused_monitor_with_pr(env, ticket_id="T-PR", last_known="in_progress")

    # The ticket is already in a terminal state ("done").
    mock_ticket_client = _mock_ticket_client(state="done")
    # Mock the DirectRepoClient to return a closed-unmerged PR.
    mock_gh = _mock_direct_repo_client(merged=False, state="closed")

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with (
        patch(
            "robotsix_chat.repo.direct.client.DirectRepoClient",
            MagicMock(return_value=mock_gh),
        ),
        patch("httpx.AsyncClient", mock_ticket_client),
    ):
        await asyncio.sleep(0.20)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # The monitor should no longer be in its original paused state
    # (it was resumed, the worker detected a terminal ticket, and
    # closed the monitor again with a terminal reason).
    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.close_reason != "paused"

    # No "closed without merging" notification should be published.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    closed_unmerged_notifications = [
        frame
        for _sid, frame in notifications
        if "closed without being merged" in str(frame["body"])
    ]
    assert len(closed_unmerged_notifications) == 0


# -- Terminal-state guard branches (watch_paused_monitors) ------------------


@pytest.mark.asyncio
async def test_watcher_terminal_pr_merged_resumes() -> None:
    """Branch 1: checkpoint has pr_number, PR is merged, terminal ticket → resume."""
    settings = _settings_with_direct_repo(
        board_api_token=type("_st", (), {"get_secret_value": lambda: ""})(),
        timeout=10.0,
    )
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    info = _make_paused_monitor_with_pr(env, ticket_id="T-PR", last_known="in_progress")

    mock_ticket_client = _mock_ticket_client(state="done")
    mock_gh = _mock_direct_repo_client(merged=True)

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with (
        patch(
            "robotsix_chat.repo.direct.client.DirectRepoClient",
            MagicMock(return_value=mock_gh),
        ),
        patch("httpx.AsyncClient", mock_ticket_client),
    ):
        await asyncio.sleep(0.20)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    reopened = env.registry.get(info.id)
    assert reopened is not None
    # The monitor was resumed from pause; the worker then detected the
    # terminal ticket and closed it with a terminal reason.
    assert reopened.close_reason != "paused"


@pytest.mark.asyncio
async def test_watcher_terminal_github_unreachable_keeps_paused() -> None:
    """Branch 2: checkpoint pr_number, GitHub unreachable, terminal → keep paused."""
    settings = _settings_with_direct_repo(enabled=False)
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    info = _make_paused_monitor_with_pr(env, ticket_id="T-PR", last_known="in_progress")

    mock_ticket_client = _mock_ticket_client(state="closed")

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with patch("httpx.AsyncClient", mock_ticket_client):
        await asyncio.sleep(0.20)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert not reopened.is_active


@pytest.mark.asyncio
async def test_watcher_terminal_no_pr_number_board_pr_url_resumes() -> None:
    """Branch 4: no checkpoint pr_number, board pr_url present, terminal → resume."""
    settings = _settings_with_direct_repo(
        board_api_token=type("_st", (), {"get_secret_value": lambda: ""})(),
        timeout=10.0,
    )
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    info = _make_paused_monitor(env, ticket_id="T-1", last_known="in_progress")

    mock_ticket_client = _mock_ticket_client(
        state="done", pr_url="https://github.com/org/repo/pull/1"
    )

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with patch("httpx.AsyncClient", mock_ticket_client):
        await asyncio.sleep(0.20)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    reopened = env.registry.get(info.id)
    assert reopened is not None
    # The monitor was resumed from pause; the worker then detected the
    # terminal ticket and closed it with a terminal reason.
    assert reopened.close_reason != "paused"


@pytest.mark.asyncio
async def test_watcher_terminal_no_pr_evidence_keeps_paused() -> None:
    """Branch 5: no checkpoint pr_number, no board pr_url, terminal → keep paused."""
    settings = _settings_with_direct_repo(
        board_api_token=type("_st", (), {"get_secret_value": lambda: ""})(),
        timeout=10.0,
    )
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    info = _make_paused_monitor(env, ticket_id="T-1", last_known="in_progress")

    mock_ticket_client = _mock_ticket_client(state="closed")

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with patch("httpx.AsyncClient", mock_ticket_client):
        await asyncio.sleep(0.20)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert not reopened.is_active


@pytest.mark.asyncio
async def test_watcher_notifies_on_merge_conflict() -> None:
    """When a tracked PR has merge conflicts, the watcher notifies with high urgency."""
    settings = _settings_with_direct_repo(
        board_api_token=type("_st", (), {"get_secret_value": lambda: ""})(),
        timeout=10.0,
    )
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    _make_paused_monitor_with_pr(env, ticket_id="T-PR", last_known="in_progress")

    # Mock the mill to return a non-terminal ticket state.
    mock_ticket_client = _mock_ticket_client(state="in_progress")
    # Mock the DirectRepoClient to return a PR with merge conflicts.
    mock_gh = _mock_direct_repo_client(merged=False, state="open", mergeable=False)

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with (
        patch(
            "robotsix_chat.repo.direct.client.DirectRepoClient",
            MagicMock(return_value=mock_gh),
        ),
        patch("httpx.AsyncClient", mock_ticket_client),
    ):
        await asyncio.sleep(0.20)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # An SSE notification should be published for merge conflicts.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    merge_conflict_notifications = [
        frame
        for _sid, frame in notifications
        if "merge conflicts" in str(frame["body"])
    ]
    assert len(merge_conflict_notifications) == 1

    urgency_values = [frame["urgency"] for frame in merge_conflict_notifications]
    assert urgency_values == ["high"]


# -- 404-tracking auto-close -------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_closes_monitor_after_consecutive_404s() -> None:
    """After ``_MAX_WATCHER_404_FAILURES`` consecutive 404s the monitor is closed.

    The watcher tracks ``_watcher_404_count`` in the checkpoint; when it
    reaches the threshold the monitor is closed with reason
    ``"ticket_deleted"`` and a summary is delivered via the delivery
    channel.
    """
    settings = _settings_with_direct_repo()
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    info = _make_paused_monitor(env, ticket_id="T-DELETED", last_known="open")

    # Mock the mill to return 404 on every call.
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "not found",
        request=MagicMock(),
        response=mock_response,
    )

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    # Poll interval short enough for several ticks.
    settings.subsessions.paused_monitor_poll_interval_seconds = 0.01

    # Explicitly resolve nothing so the close path is exercised without
    # depending on the real BoardClient's behaviour behind the mocked
    # httpx.AsyncClient.
    resolved_board = MagicMock()
    resolved_board.resolve_ticket_ids = AsyncMock(return_value={})

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with (
        patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)),
        patch(
            "robotsix_chat.subsessions.watcher.BoardClient",
            MagicMock(return_value=resolved_board),
        ),
    ):
        # Let the watcher poll enough ticks to accumulate 3+ consecutive 404s.
        await asyncio.sleep(0.2)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # The monitor should be CLOSED with reason "ticket_deleted".
    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.CLOSED
    assert reopened.close_reason == "ticket_deleted"
    assert "404" in (reopened.summary or "")

    # Verify the stall notification was published before closing.
    stall_notifications = [
        (sid, f)
        for sid, f in sink.frames
        if f.get("type") == SSE_NOTIFICATION_TYPE
        and "Monitor stalled" in (f.get("title") or "")
    ]
    assert len(stall_notifications) == 1
    _, stall_frame = stall_notifications[0]
    assert stall_frame["urgency"] == "medium"
    assert "T-DELETED" in (stall_frame.get("body") or "")
    assert stall_frame["link"] == "https://mill.example.com/tickets/T-DELETED"


@pytest.mark.asyncio
async def test_watcher_resolves_stale_ticket_id_and_keeps_monitoring() -> None:
    """Consecutive 404s that resolve to a different valid ID keep the monitor alive.

    When ``BoardClient.resolve_ticket_ids`` maps the stored ID to a fresh
    one, the watcher rewrites the checkpoint (new ID, cleared 404 counter),
    publishes a low-urgency resolution notification with a full ticket URL,
    and continues monitoring instead of closing.
    """
    settings = _settings_with_direct_repo()
    sink = RecordingSink()
    env = build_env(settings=settings, event_sink=sink)

    info = _make_paused_monitor(env, ticket_id="T-STALE", last_known="open")

    # The mill 404s for the stale ID but serves the resolved ID normally.
    def _mill_get(url: str) -> MagicMock:
        response = MagicMock()
        if url.endswith("/T-STALE"):
            response.status_code = 404
            response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "not found", request=MagicMock(), response=response
            )
        else:
            response.json.return_value = {"state": "open"}
            response.raise_for_status.return_value = None
        return response

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=_mill_get)

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    resolved_board = MagicMock()
    resolved_board.resolve_ticket_ids = AsyncMock(return_value={"T-STALE": "T-LIVE"})

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with (
        patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)),
        patch(
            "robotsix_chat.subsessions.watcher.BoardClient",
            MagicMock(return_value=resolved_board),
        ),
    ):
        # Enough ticks to accumulate 3 consecutive 404s and then poll the
        # resolved ID successfully.
        await asyncio.sleep(0.2)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # The monitor must still be paused (not closed as ticket_deleted),
    # now tracking the resolved ID with a cleared 404 counter.
    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.CLOSED
    assert reopened.close_reason == "paused"
    assert reopened.checkpoint is not None
    assert reopened.checkpoint["ticket_id"] == "T-LIVE"
    assert "_watcher_404_count" not in reopened.checkpoint

    # Exactly one low-urgency resolution notification, linked to the
    # resolved ticket's full board URL.
    resolved_notifications = [
        (sid, f)
        for sid, f in sink.frames
        if f.get("type") == SSE_NOTIFICATION_TYPE
        and "ticket ID resolved" in (f.get("title") or "")
    ]
    assert len(resolved_notifications) == 1
    _, resolved_frame = resolved_notifications[0]
    assert resolved_frame["urgency"] == "low"
    assert "T-STALE" in (resolved_frame.get("body") or "")
    assert "T-LIVE" in (resolved_frame.get("body") or "")
    assert resolved_frame["link"] == "https://mill.example.com/tickets/T-LIVE"


@pytest.mark.asyncio
async def test_watcher_resets_404_counter_on_successful_response() -> None:
    """The 404 counter is reset when the mill returns a successful response.

    One or two 404s followed by a successful response should reset the
    counter so the threshold never accumulates across transient outages.
    """
    settings = _settings_with_direct_repo()
    env = build_env(settings=settings)

    info = _make_paused_monitor(env, ticket_id="T-TRANSIENT", last_known="open")

    # Pre-seed the checkpoint with 2 prior 404s.
    assert info.checkpoint is not None
    info.checkpoint["_watcher_404_count"] = 2
    env.registry.update_checkpoint(info.id, info.checkpoint)

    # Mock the mill to return a successful response.
    mock_response = MagicMock()
    mock_response.json.return_value = {"state": "open"}
    mock_response.raise_for_status.return_value = None

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    settings.subsessions.paused_monitor_poll_interval_seconds = 0.01

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        await asyncio.sleep(0.15)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # The 404 counter should have been removed from the checkpoint.
    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.checkpoint is not None
    assert "_watcher_404_count" not in reopened.checkpoint
    # The monitor should still be paused (state unchanged).
    assert reopened.status is SubsessionStatus.CLOSED


@pytest.mark.asyncio
async def test_watcher_404_keeps_paused_below_threshold() -> None:
    """One or two 404s do NOT close the monitor — only the threshold triggers."""
    settings = _settings_with_direct_repo()
    env = build_env(settings=settings)

    info = _make_paused_monitor(env, ticket_id="T-FLEETING", last_known="open")

    # Mock the mill to return 404.
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "not found",
        request=MagicMock(),
        response=mock_response,
    )

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    settings.subsessions.paused_monitor_poll_interval_seconds = 0.01

    watcher_task = asyncio.create_task(watch_paused_monitors(env))

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        # Short sleep — only ~2 ticks, below the threshold of 3.
        await asyncio.sleep(0.02)

    watcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await watcher_task

    # The monitor should still be paused.
    reopened = env.registry.get(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.CLOSED
    assert reopened.close_reason == "paused"
    # The counter should have been incremented but not yet reached threshold.
    assert reopened.checkpoint is not None
    count = reopened.checkpoint.get("_watcher_404_count", 0)
    assert 1 <= count < 3

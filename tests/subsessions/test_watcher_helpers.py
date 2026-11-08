"""Unit tests for the helpers extracted from ``watch_paused_monitors``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE
from robotsix_chat.subsessions import (
    SubsessionEnv,
    SubsessionInfo,
    SubsessionKind,
    SubsessionStatus,
    watcher,
)
from robotsix_chat.subsessions.watcher import (
    _MAX_WATCHER_404_FAILURES,
    _check_paused_monitor,
    _check_prerequisite_wait,
    _check_tracked_pr,
    _check_tracked_pr_merges,
    _handle_ticket_404,
    _resolve_watcher_config,
    _WatcherState,
)
from robotsix_chat.ticket_poll.mill_states import MERGED_TERMINAL_STATES
from tests.common.subsession_fakes import RecordingSink, build_env, make_settings

OWNER = "sess-main"
BOARD = "https://mill.example.com"
TERMINAL_STATE = min(MERGED_TERMINAL_STATES)


def _settings(**direct_repo_kw: Any) -> Any:
    settings = make_settings()
    settings.subsessions.paused_monitor_poll_interval_seconds = 5.0
    kwargs: dict[str, Any] = {"board_api_base_url": BOARD, "enabled": True}
    kwargs.update(direct_repo_kw)
    settings.direct_repo = SimpleNamespace(**kwargs)
    return settings


def _env(sink: RecordingSink | None = None, **direct_repo_kw: Any) -> SubsessionEnv:
    return build_env(settings=_settings(**direct_repo_kw), event_sink=sink)


def _paused(env: SubsessionEnv, **checkpoint: object) -> SubsessionInfo:
    cp: dict[str, object] = {"ticket_id": "T-1", "last_known_state": "open"}
    cp.update(checkpoint)
    info = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="mon",
        prompt="monitor",
        model_level=3,
        interval_seconds=60.0,
        checkpoint=cp,
    )
    env.registry.mark_closed(
        info.id, summary="paused", reason="paused", closed_by="system"
    )
    reloaded = env.registry.get(info.id)
    assert reloaded is not None
    return reloaded


def _notifications(sink: RecordingSink) -> list[dict[str, object]]:
    return [f for _sid, f in sink.frames if f.get("type") == SSE_NOTIFICATION_TYPE]


# -- _resolve_watcher_config ------------------------------------------------


def test_resolve_watcher_config_returns_url_and_interval() -> None:
    """A configured board URL and positive interval are returned."""
    env = _env()
    assert _resolve_watcher_config(env) == (BOARD, 5.0)


def test_resolve_watcher_config_none_without_board_url() -> None:
    """No board URL disables the watcher."""
    env = _env(board_api_base_url="")
    assert _resolve_watcher_config(env) is None


def test_resolve_watcher_config_none_when_polling_disabled() -> None:
    """A non-positive poll interval disables the watcher."""
    env = _env()
    env.settings.subsessions.paused_monitor_poll_interval_seconds = 0
    assert _resolve_watcher_config(env) is None


# -- _check_prerequisite_wait -----------------------------------------------


def _waiting(env: SubsessionEnv) -> SubsessionInfo:
    info = _paused(env)
    info.close_reason = "waiting_for_prerequisite"
    info.depends_on_ticket_id = "PRE-1"
    return info


@pytest.mark.asyncio
async def test_prerequisite_wait_resumes_when_prerequisite_terminal() -> None:
    """A terminal prerequisite resumes the waiting monitor."""
    env = _env()
    info = _waiting(env)
    query = AsyncMock(return_value=(TERMINAL_STATE, None, 200))
    resume = AsyncMock()
    with (
        patch.object(watcher, "_query_ticket_state", query),
        patch.object(watcher, "_resume_paused_monitor", resume),
    ):
        decision = await _check_prerequisite_wait(env, BOARD, info)
    assert decision == "resume"
    query.assert_awaited_once_with(BOARD, "PRE-1", info.id)
    resume.assert_awaited_once_with(env, info.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [(None, None, 404), ("in_progress", None, 200), (None, None, None)],
)
async def test_prerequisite_wait_keeps_paused(result: tuple[Any, ...]) -> None:
    """A missing (404), non-terminal or unreachable prerequisite keeps paused."""
    env = _env()
    info = _waiting(env)
    resume = AsyncMock()
    with (
        patch.object(watcher, "_query_ticket_state", AsyncMock(return_value=result)),
        patch.object(watcher, "_resume_paused_monitor", resume),
    ):
        decision = await _check_prerequisite_wait(env, BOARD, info)
    assert decision == "keep_paused"
    resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_paused_monitor_routes_to_prerequisite_wait() -> None:
    """A waiting monitor polls its prerequisite, not its own ticket."""
    env = _env()
    info = _waiting(env)
    query = AsyncMock(return_value=("in_progress", None, 200))
    with patch.object(watcher, "_query_ticket_state", query):
        decision = await _check_paused_monitor(env, BOARD, info, _WatcherState())
    assert decision == "keep_paused"
    query.assert_awaited_once_with(BOARD, "PRE-1", info.id)


# -- _handle_ticket_404 / 404 counter ---------------------------------------


@pytest.mark.asyncio
async def test_handle_ticket_404_transient_when_api_unhealthy() -> None:
    """An unhealthy board API does not count the 404."""
    env = _env()
    info = _paused(env)
    assert info.checkpoint is not None
    with patch.object(watcher, "_check_api_healthy", AsyncMock(return_value=False)):
        outcome = await _handle_ticket_404(
            env, BOARD, info, info.checkpoint, "T-1", _WatcherState()
        )
    assert outcome == "transient"
    assert "_watcher_404_count" not in info.checkpoint


@pytest.mark.asyncio
async def test_handle_ticket_404_counts_below_threshold() -> None:
    """A 404 below the threshold increments the counter and keeps paused."""
    env = _env()
    info = _paused(env)
    assert info.checkpoint is not None
    with patch.object(watcher, "_check_api_healthy", AsyncMock(return_value=True)):
        outcome = await _handle_ticket_404(
            env, BOARD, info, info.checkpoint, "T-1", _WatcherState()
        )
    assert outcome == "counted"
    assert info.checkpoint["_watcher_404_count"] == 1
    stored = env.registry.get(info.id)
    assert stored is not None
    assert stored.close_reason == "paused"


@pytest.mark.asyncio
async def test_handle_ticket_404_resolves_stale_ticket_id() -> None:
    """At the threshold a resolvable stale id is swapped in instead of closing."""
    sink = RecordingSink()
    env = _env(sink)
    info = _paused(env, _watcher_404_count=_MAX_WATCHER_404_FAILURES - 1)
    assert info.checkpoint is not None
    board = MagicMock()
    board.resolve_ticket_ids = AsyncMock(return_value={"T-1": "T-1-real"})
    with (
        patch.object(watcher, "_check_api_healthy", AsyncMock(return_value=True)),
        patch.object(watcher, "BoardClient", MagicMock(return_value=board)),
    ):
        outcome = await _handle_ticket_404(
            env, BOARD, info, info.checkpoint, "T-1", _WatcherState()
        )
    assert outcome == "resolved"
    assert info.checkpoint["ticket_id"] == "T-1-real"
    assert "_watcher_404_count" not in info.checkpoint
    frames = _notifications(sink)
    assert len(frames) == 1
    assert frames[0]["link"] == f"{BOARD}/tickets/T-1-real"


@pytest.mark.asyncio
async def test_handle_ticket_404_closes_at_threshold() -> None:
    """At the threshold an unresolvable ticket closes the monitor once."""
    sink = RecordingSink()
    env = _env(sink)
    info = _paused(env, _watcher_404_count=_MAX_WATCHER_404_FAILURES - 1)
    assert info.checkpoint is not None
    board = MagicMock()
    board.resolve_ticket_ids = AsyncMock(return_value={})
    state = _WatcherState()
    with (
        patch.object(watcher, "_check_api_healthy", AsyncMock(return_value=True)),
        patch.object(watcher, "BoardClient", MagicMock(return_value=board)),
    ):
        outcome = await _handle_ticket_404(
            env, BOARD, info, info.checkpoint, "T-1", state
        )
    assert outcome == "closed"
    assert info.id in state.ticket_deleted_notified
    stored = env.registry.get(info.id)
    assert stored is not None
    assert stored.status is SubsessionStatus.CLOSED
    assert stored.close_reason == "ticket_deleted"
    frames = _notifications(sink)
    assert len(frames) == 1
    assert "Monitor stalled" in str(frames[0]["title"])


@pytest.mark.asyncio
async def test_check_paused_monitor_resets_404_counter_on_non_404_error() -> None:
    """A non-404 failure resets the consecutive-404 counter and skips."""
    env = _env()
    info = _paused(env, _watcher_404_count=2)
    assert info.checkpoint is not None
    with patch.object(
        watcher, "_query_ticket_state", AsyncMock(return_value=(None, None, 500))
    ):
        decision = await _check_paused_monitor(env, BOARD, info, _WatcherState())
    assert decision == "skip"
    assert "_watcher_404_count" not in info.checkpoint


@pytest.mark.asyncio
async def test_check_paused_monitor_skips_without_ticket_id() -> None:
    """A checkpoint without a ticket id is skipped without querying."""
    env = _env()
    info = _paused(env, ticket_id="")
    query = AsyncMock()
    with patch.object(watcher, "_query_ticket_state", query):
        decision = await _check_paused_monitor(env, BOARD, info, _WatcherState())
    assert decision == "skip"
    query.assert_not_awaited()


# -- _check_tracked_pr / PR-merge pass --------------------------------------


def _gh(pr_data: dict[str, Any] | None = None, exc: Exception | None = None) -> Any:
    client = MagicMock()
    client.get_pr = AsyncMock(return_value=pr_data, side_effect=exc)
    return client


@pytest.mark.asyncio
async def test_check_tracked_pr_skips_without_pr_number() -> None:
    """A checkpoint without a tracked PR is skipped."""
    env = _env()
    info = _paused(env)
    gh = _gh({})
    decision = await _check_tracked_pr(
        env, BOARD, gh, env.settings.direct_repo, info, _WatcherState()
    )
    assert decision == "skip"
    gh.get_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_tracked_pr_skips_on_github_error() -> None:
    """A GitHub error while fetching the PR skips the monitor."""
    env = _env()
    info = _paused(env, pr_number=42, repo_full_name="org/repo")
    gh = _gh(exc=RuntimeError("boom"))
    decision = await _check_tracked_pr(
        env, BOARD, gh, env.settings.direct_repo, info, _WatcherState()
    )
    assert decision == "skip"


@pytest.mark.asyncio
async def test_check_tracked_pr_resumes_on_merge() -> None:
    """A merged PR with a verified image publish resumes the monitor."""
    env = _env()
    info = _paused(env, pr_number=42, repo_full_name="org/repo")
    gh = _gh({"merged": True, "state": "closed", "merge_commit_sha": "abc"})
    verify = AsyncMock(return_value=(True, "published"))
    resume = AsyncMock()
    with (
        patch.object(watcher, "_verify_image_publish_on_main", verify),
        patch.object(watcher, "_resume_merged_pr_monitor", resume),
    ):
        decision = await _check_tracked_pr(
            env, BOARD, gh, env.settings.direct_repo, info, _WatcherState()
        )
    assert decision == "resume"
    verify.assert_awaited_once_with(env, "org/repo", "abc", 42, info.id)
    resume.assert_awaited_once_with(env, info.id, 42, "org/repo")


@pytest.mark.asyncio
async def test_check_tracked_pr_waits_for_image_publish() -> None:
    """A merged PR whose image is not yet published stays paused, notified once."""
    sink = RecordingSink()
    env = _env(sink)
    info = _paused(env, pr_number=42, repo_full_name="org/repo")
    gh = _gh({"merged": True, "state": "closed", "merge_commit_sha": "abc"})
    state = _WatcherState()
    resume = AsyncMock()
    with (
        patch.object(
            watcher,
            "_verify_image_publish_on_main",
            AsyncMock(return_value=(False, "in progress")),
        ),
        patch.object(watcher, "_resume_merged_pr_monitor", resume),
    ):
        for _ in range(2):
            decision = await _check_tracked_pr(
                env, BOARD, gh, env.settings.direct_repo, info, state
            )
            assert decision == "keep_paused"
    resume.assert_not_awaited()
    frames = _notifications(sink)
    assert len(frames) == 1
    assert frames[0]["title"] == "Image publish pending"


@pytest.mark.asyncio
async def test_check_tracked_pr_closed_unmerged_alerts_and_resumes() -> None:
    """A PR closed without merging alerts the owner and resumes the monitor."""
    sink = RecordingSink()
    env = _env(sink)
    info = _paused(env, pr_number=42, repo_full_name="org/repo")
    gh = _gh({"merged": False, "state": "closed"})
    board = MagicMock()
    board.create_ticket = AsyncMock(return_value="FOLLOW-1")
    resume = AsyncMock()
    with (
        patch.object(
            watcher,
            "_query_ticket_state",
            AsyncMock(return_value=("in_progress", None, 200)),
        ),
        patch.object(watcher, "BoardClient", MagicMock(return_value=board)),
        patch.object(watcher, "_resume_paused_monitor", resume),
    ):
        decision = await _check_tracked_pr(
            env, BOARD, gh, env.settings.direct_repo, info, _WatcherState()
        )
    assert decision == "resume"
    resume.assert_awaited_once_with(env, info.id)
    board.create_ticket.assert_awaited_once()
    frames = _notifications(sink)
    assert len(frames) == 1
    assert frames[0]["urgency"] == "high"
    assert "closed without merging" in str(frames[0]["title"])


@pytest.mark.asyncio
async def test_check_tracked_pr_keeps_paused_when_open() -> None:
    """An open, unmerged PR keeps the monitor paused."""
    env = _env()
    info = _paused(env, pr_number=42, repo_full_name="org/repo")
    gh = _gh({"merged": False, "state": "open", "mergeable": True})
    decision = await _check_tracked_pr(
        env, BOARD, gh, env.settings.direct_repo, info, _WatcherState()
    )
    assert decision == "keep_paused"


@pytest.mark.asyncio
async def test_check_tracked_pr_merges_noop_when_direct_repo_disabled() -> None:
    """The PR-merge pass does nothing when direct_repo is disabled."""
    env = _env(enabled=False)
    _paused(env, pr_number=42, repo_full_name="org/repo")
    client_cls = MagicMock()
    with patch("robotsix_chat.repo.direct.client.DirectRepoClient", client_cls):
        await _check_tracked_pr_merges(env, BOARD, _WatcherState())
    client_cls.assert_not_called()

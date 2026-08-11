"""Tests for mill-board resume status — _check_resume_status, _handle_mill_unreachable,
_reset_mill_failure_counter, and _get_mill_started_at.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robotsix_chat.subsessions import SubsessionKind, SubsessionStatus
from robotsix_chat.subsessions.worker_mill import (
    _check_resume_status,
    _get_mill_started_at,
    _handle_mill_unreachable,
    _reset_mill_failure_counter,
)
from tests.common.subsession_fakes import build_env, make_settings

OWNER = "sess-main"

# ============================================================================
# resume status check (_check_resume_status, _handle_mill_unreachable,
# _reset_mill_failure_counter)
# ============================================================================

# _MAX_MILL_FAILURES = 2 in the worker module (private constant).


# -- helpers ----------------------------------------------------------------


def _make_checkpoint_info(env, **checkpoint_kwargs):
    """Register a periodic subsession with a checkpoint and return info."""
    sub_id = env.registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="ticket monitor",
        prompt="monitor TICKET-1",
        model_level=3,
        interval_seconds=60.0,
        checkpoint=checkpoint_kwargs or None,
    ).id
    return env.registry.get(sub_id)


def _env_with_board(board_url="https://mill.example.com"):
    """Build an env with ``board_api_base_url`` configured.

    The resume status check actually makes HTTP calls instead of
    short-circuiting on a missing/empty URL.
    """
    settings = make_settings()
    settings.direct_repo = type("_ns", (), {"board_api_base_url": board_url})()
    return build_env(settings=settings)


def _mock_async_client(response_json=None, side_effect=None):
    """Build a mock ``httpx.AsyncClient`` that returns a controlled response.

    Returns a MagicMock suitable for ``patch("httpx.AsyncClient", ...)``.
    The mock client is an async context manager whose ``__aenter__``
    returns a mock with ``.get`` returning either *response_json* (via a
    mock response) or raising *side_effect*.
    """
    # Use MagicMock (NOT AsyncMock) for the response — raise_for_status()
    # and json() are sync methods on httpx.Response.
    mock_response = MagicMock()
    mock_response.json.return_value = response_json or {}
    mock_response.raise_for_status.return_value = None

    # mock_client holds the async get method.
    mock_client = MagicMock()
    get_mock = AsyncMock()
    if side_effect is not None:
        get_mock.side_effect = side_effect
    else:
        get_mock.return_value = mock_response
    mock_client.get = get_mock

    # mock_instance is the async context manager (returned by AsyncClient()).
    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=mock_instance)


def _make_response(json_body):
    """Build a MagicMock httpx.Response with the given JSON body."""
    resp = MagicMock()
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


def _mock_async_client_dual(*, ticket_json=None, health_json=None):
    """Build a mock AsyncClient dispatching on URL path.

    ``mock_client.get(url)`` inspects the URL path and returns:
    - *ticket_json* for URLs containing ``/tickets/``
    - *health_json* for URLs containing ``/health``
    - An empty dict otherwise.
    """

    async def _dispatch(url, **kwargs):
        url_str = str(url)
        if "/health" in url_str:
            return _make_response(health_json or {})
        if "/tickets/" in url_str:
            return _make_response(ticket_json or {})
        return _make_response({})

    mock_client = MagicMock()
    mock_client.get = _dispatch

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=mock_instance)


# -- no-checkpoint / no-ticket-id / no-board-url paths -----------------------


@pytest.mark.asyncio
async def test_check_resume_status_no_checkpoint_continues():
    """When info.checkpoint is None, return (True, None) — normal resume."""
    env = build_env()
    info = _make_checkpoint_info(env)  # no checkpoint
    info.checkpoint = None

    should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is None


@pytest.mark.asyncio
async def test_check_resume_status_no_ticket_id_continues():
    """Checkpoint without 'ticket_id' key → continue."""
    env = build_env()
    info = _make_checkpoint_info(env, other_field="value")

    should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is None


@pytest.mark.asyncio
async def test_check_resume_status_no_board_url_continues():
    """When board_api_base_url is not configured, skip the check."""
    settings = make_settings()
    settings.direct_repo = type("_ns", (), {"board_api_base_url": ""})()
    env = build_env(settings=settings)
    info = _make_checkpoint_info(env, ticket_id="TICKET-1")

    should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is None


# -- terminal / blocked / open state branches --------------------------------


@pytest.mark.asyncio
async def test_check_resume_status_terminal_closes_and_delivers():
    """A ticket in a terminal state closes the subsession and delivers summary."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(
        response_json={
            "state": "closed",
            "pr_url": "https://github.com/owner/repo/pull/42",
        }
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert context_msg is not None
    assert "terminal" in context_msg
    assert "TICKET-1" in context_msg

    # Delivery is fire-and-forget — let the background task run.
    await asyncio.sleep(0)

    # Registry is now closed.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "ticket_terminal_on_resume"

    # Summary was delivered to the conversation store.
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    label, reply = history[0]
    assert "ticket_terminal" in label
    assert "TICKET-1" in reply


@pytest.mark.asyncio
async def test_check_resume_status_blocked_injects_context():
    """A blocked ticket returns (True, context_message)."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(response_json={"state": "blocked"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg
    assert "TICKET-1" in context_msg


# -- stale worker detection on blocked resume ---------------------------------


@pytest.mark.asyncio
async def test_check_resume_status_blocked_stale_worker_first_attempt():
    """First stale-worker resume: injects strong warning context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        worker_started_at="2024-01-01T00:00:00Z",
    )

    mock = _mock_async_client_dual(
        ticket_json={"state": "blocked"},
        health_json={"status": "alive", "started_at": "2024-01-01T00:00:00Z"},
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg
    assert "NOT been redeployed" in context_msg
    assert "1/2" in context_msg
    assert "TICKET-1" in context_msg

    # Checkpoint should have been updated with stale_worker_resume_count.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("stale_worker_resume_count") == 1


@pytest.mark.asyncio
async def test_check_resume_status_blocked_stale_worker_at_cap_closes():
    """Second stale-worker resume: closes the subsession."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        worker_started_at="2024-01-01T00:00:00Z",
        stale_worker_resume_count=1,
    )

    mock = _mock_async_client_dual(
        ticket_json={"state": "blocked"},
        health_json={"status": "alive", "started_at": "2024-01-01T00:00:00Z"},
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert context_msg is not None
    assert "not been redeployed" in context_msg
    assert "TICKET-1" in context_msg

    await asyncio.sleep(0)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "stale_worker"


@pytest.mark.asyncio
async def test_check_resume_status_blocked_worker_redeployed_resets_counter():
    """Worker redeployed (different started_at): resets counter, normal context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        worker_started_at="2024-01-01T00:00:00Z",
        stale_worker_resume_count=1,
    )

    mock = _mock_async_client_dual(
        ticket_json={"state": "blocked"},
        health_json={"status": "alive", "started_at": "2024-06-15T12:00:00Z"},
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg
    # Should NOT contain the stale-worker warning.
    assert "NOT been redeployed" not in context_msg

    # Checkpoint should have new started_at and NO stale counter.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("worker_started_at") == "2024-06-15T12:00:00Z"
    assert "stale_worker_resume_count" not in updated.checkpoint


@pytest.mark.asyncio
async def test_check_resume_status_blocked_health_probe_fails_graceful():
    """When the health probe fails, proceed with normal blocked context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    # Health endpoint returns 503; ticket endpoint returns blocked.
    async def _dispatch(url, **kwargs):
        url_str = str(url)
        if "/health" in url_str:
            resp = MagicMock()
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "boom", request=MagicMock(), response=MagicMock(status_code=503)
            )
            return resp
        return _make_response({"state": "blocked"})

    mock_client = MagicMock()
    mock_client.get = _dispatch
    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)
    mock = MagicMock(return_value=mock_instance)

    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg
    # Should be the normal context, not the stale-worker variant.
    assert "NOT been redeployed" not in context_msg


@pytest.mark.asyncio
async def test_check_resume_status_blocked_no_previous_started_at_stores_it():
    """First resume with no stored worker_started_at: stores it, normal context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        # No worker_started_at key.
    )

    mock = _mock_async_client_dual(
        ticket_json={"state": "blocked"},
        health_json={"status": "alive", "started_at": "2024-01-01T00:00:00Z"},
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg
    assert "NOT been redeployed" not in context_msg

    # worker_started_at should be stored for next time.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("worker_started_at") == "2024-01-01T00:00:00Z"


# -- blocked-resume threshold detection --------------------------------------


@pytest.mark.asyncio
async def test_check_resume_status_blocked_increments_blocked_resume_count():
    """First blocked resume increments blocked_resume_count and returns context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(response_json={"state": "blocked"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "BLOCKED" in context_msg

    # Counter should be 1 after first blocked resume.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("blocked_resume_count") == 1


@pytest.mark.asyncio
async def test_check_resume_status_blocked_second_attempt_adds_warning():
    """Second blocked resume adds a repeated-block warning to the context."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        blocked_resume_count=1,
    )

    mock = _mock_async_client(response_json={"state": "blocked"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "Repeated block" in context_msg
    assert "2/3" in context_msg
    assert "1 remaining" in context_msg

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("blocked_resume_count") == 2


@pytest.mark.asyncio
async def test_check_resume_status_blocked_at_cap_closes():
    """Third consecutive blocked resume closes the subsession."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        blocked_resume_count=2,
    )

    mock = _mock_async_client(response_json={"state": "blocked"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert context_msg is not None
    assert "3 consecutive" in context_msg
    assert "TICKET-1" in context_msg

    await asyncio.sleep(0)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "repeated_blocked"


@pytest.mark.asyncio
async def test_check_resume_status_blocked_resets_counter_on_non_blocked():
    """When ticket transitions to a non-blocked state, the counter resets."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="blocked",
        blocked_resume_count=2,
    )

    mock = _mock_async_client(response_json={"state": "open"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "Continue monitoring" in context_msg

    # Counter should be reset to 0.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("blocked_resume_count") == 0


@pytest.mark.asyncio
async def test_check_resume_status_blocked_stale_and_blocked_caps_independent():
    """Stale-worker cap closes independently of blocked-resume cap.

    When the stale-worker cap fires first (at 2), the blocked-resume
    counter is still tracked but the stale-worker close takes precedence.
    """
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
        worker_started_at="2024-01-01T00:00:00Z",
        stale_worker_resume_count=1,
        blocked_resume_count=1,
    )

    mock = _mock_async_client_dual(
        ticket_json={"state": "blocked"},
        health_json={"status": "alive", "started_at": "2024-01-01T00:00:00Z"},
    )
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    # Stale-worker cap (2) fires before blocked-resume cap (3).
    assert should_continue is False
    assert context_msg is not None
    assert "not been redeployed" in context_msg

    await asyncio.sleep(0)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "stale_worker"


# -- _get_mill_started_at ----------------------------------------------------


@pytest.mark.asyncio
async def test_get_mill_started_at_returns_timestamp():
    """When health returns started_at, it is returned as a string."""
    mock = _mock_async_client(
        response_json={"status": "alive", "started_at": "2024-06-15T12:00:00Z"}
    )
    with patch("httpx.AsyncClient", mock):
        result = await _get_mill_started_at("https://mill.example.com")
    assert result == "2024-06-15T12:00:00Z"


@pytest.mark.asyncio
async def test_get_mill_started_at_missing_key_returns_none():
    """When health response lacks started_at, returns None."""
    mock = _mock_async_client(response_json={"status": "alive"})
    with patch("httpx.AsyncClient", mock):
        result = await _get_mill_started_at("https://mill.example.com")
    assert result is None


@pytest.mark.asyncio
async def test_get_mill_started_at_http_error_returns_none():
    """When health endpoint errors, returns None."""
    mock = _mock_async_client(
        side_effect=httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=MagicMock(status_code=500)
        )
    )
    with patch("httpx.AsyncClient", mock):
        result = await _get_mill_started_at("https://mill.example.com")
    assert result is None


@pytest.mark.asyncio
async def test_get_mill_started_at_connect_error_returns_none():
    """When health endpoint is unreachable, returns None."""
    mock = _mock_async_client(side_effect=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient", mock):
        result = await _get_mill_started_at("https://mill.example.com")
    assert result is None


@pytest.mark.asyncio
async def test_check_resume_status_human_issue_approval_injects_context():
    """human_issue_approval ticket injects context and updates checkpoint."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(response_json={"state": "human_issue_approval"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "HUMAN_ISSUE_APPROVAL" in context_msg
    assert "TICKET-1" in context_msg

    # Checkpoint was updated with the current state.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("last_known_state") == "human_issue_approval"


@pytest.mark.asyncio
async def test_check_resume_status_pre_authorized_escalates_immediately():
    """Pre-authorized ticket in human_issue_approval escalates on resume."""
    env = _env_with_board()
    # Set pre_authorized_ticket_patterns on the subsession settings.
    env.settings.subsessions.pre_authorized_ticket_patterns = ["TICKET-*"]
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(response_json={"state": "human_issue_approval"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert context_msg is not None
    assert "pre-authorized" in (context_msg or "").lower()
    assert "TICKET-1" in (context_msg or "")

    # Subsessions should be closed.
    closed_info = env.registry.get(info.id)
    assert closed_info is not None
    assert closed_info.status is SubsessionStatus.CLOSED
    assert closed_info.close_reason == "pre_authorized_approval"


@pytest.mark.asyncio
async def test_check_resume_status_pre_authorized_no_match_injects_context():
    """Non-matching pre-authorized pattern falls through to normal context injection."""
    env = _env_with_board()
    env.settings.subsessions.pre_authorized_ticket_patterns = ["OTHER-*"]
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(response_json={"state": "human_issue_approval"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "HUMAN_ISSUE_APPROVAL" in context_msg


@pytest.mark.asyncio
async def test_check_resume_status_open_injects_context():
    """An open/in_progress/pending ticket continues with a context note."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="in_progress",
    )

    mock = _mock_async_client(response_json={"state": "open"})
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is not None
    assert "Continue monitoring" in context_msg
    assert "TICKET-1" in context_msg


# -- HTTP error handling -----------------------------------------------------


@pytest.mark.asyncio
async def test_check_resume_status_http_404_closes_immediately():
    """A 404 response closes the subsession immediately (not counted as unreachable)."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    error_response = AsyncMock()
    error_response.status_code = 404
    http_error = httpx.HTTPStatusError(
        "not found", request=AsyncMock(), response=error_response
    )

    mock = _mock_async_client(side_effect=http_error)
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert "deleted" in (context_msg or "")
    # Check that checkpoint was NOT updated with a failure counter (404 is not
    # counted as unreachable).
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "ticket_unreachable"

    # Delivery is fire-and-forget — let the background task run.
    await asyncio.sleep(0)

    # Summary was delivered.
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    assert "deleted" in history[0][1]


@pytest.mark.asyncio
async def test_check_resume_status_http_401_closes_immediately():
    """A 401/403 closes immediately with an auth-error message."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    error_response = AsyncMock()
    error_response.status_code = 401
    http_error = httpx.HTTPStatusError(
        "unauthorized", request=AsyncMock(), response=error_response
    )

    mock = _mock_async_client(side_effect=http_error)
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is False
    assert "Authentication error" in (context_msg or "")

    # Delivery is fire-and-forget — let the background task run.
    await asyncio.sleep(0)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED

    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    assert "Authentication" in history[0][1]


@pytest.mark.asyncio
async def test_check_resume_status_http_5xx_counts_as_unreachable():
    """A 5xx response is treated as transient — increments the failure counter."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    error_response = AsyncMock()
    error_response.status_code = 503
    http_error = httpx.HTTPStatusError(
        "server error", request=AsyncMock(), response=error_response
    )

    mock = _mock_async_client(side_effect=http_error)
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    # Should still continue (first failure, below cap).
    assert should_continue is True
    assert context_msg is None

    # Checkpoint was updated with failure counter = 1.
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("consecutive_mill_failures") == 1


# -- network errors ----------------------------------------------------------


@pytest.mark.asyncio
async def test_check_resume_status_connect_error_counts_as_unreachable():
    """A ConnectError is treated as transient (same as 5xx)."""
    env = _env_with_board()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        last_known_state="open",
    )

    mock = _mock_async_client(side_effect=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient", mock):
        should_continue, context_msg = await _check_resume_status(env, info, info.id)

    assert should_continue is True
    assert context_msg is None
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("consecutive_mill_failures") == 1


# -- _handle_mill_unreachable unit tests -------------------------------------


@pytest.mark.asyncio
async def test_handle_mill_unreachable_increments_counter():
    """Each call increments consecutive_mill_failures by 1."""
    env = build_env()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=0,
    )

    should_continue = await _handle_mill_unreachable(env, info, info.id)

    assert should_continue is True
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("consecutive_mill_failures") == 1


@pytest.mark.asyncio
async def test_handle_mill_unreachable_cap_enters_recovery():
    """At the cap the subsession enters recovery instead of closing.

    With ``consecutive_mill_failures`` already at (cap - 1), the next
    call reaches the cap, sleeps with backoff, probes health, and
    returns ``True`` (continue) when the health probe fails — it does
    NOT close the subsession.
    """
    env = build_env()
    # _MAX_MILL_FAILURES is 2, so one below the cap is 1.
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=1,
    )

    # Patch asyncio.sleep so the recovery backoff is instant.
    with patch("robotsix_chat.subsessions.worker.asyncio.sleep", new=AsyncMock()):
        should_continue = await _handle_mill_unreachable(env, info, info.id)

    assert should_continue is True
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.SLEEPING  # not CLOSED
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("consecutive_mill_failures") == 2


@pytest.mark.asyncio
async def test_handle_mill_unreachable_recovery_success_resets_counter():
    """When the health probe succeeds after recovery sleep, reset counter.

    The subsession returns to normal (counter cleared, continues).
    """
    env = _env_with_board("https://mill.example.com")
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=1,
    )

    # Patch asyncio.sleep and the health probe to simulate mill recovery.
    with (
        patch("robotsix_chat.subsessions.worker.asyncio.sleep", new=AsyncMock()),
        patch(
            "robotsix_chat.subsessions.worker_mill._get_mill_started_at",
            new=AsyncMock(return_value="2025-01-01T00:00:00Z"),
        ),
    ):
        should_continue = await _handle_mill_unreachable(env, info, info.id)

    assert should_continue is True
    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.SLEEPING  # set during sleep
    assert updated.checkpoint is not None
    # Counter was reset on successful health probe.
    assert updated.checkpoint.get("consecutive_mill_failures") == 0


@pytest.mark.asyncio
async def test_handle_mill_unreachable_recovery_exhausted_closes():
    """After mill_recovery_max_retries retries the subsession is closed.

    Default max retries is 10, so with the cap at 2, closing happens
    at failure count = 2 + 10 = 12.
    """
    env = build_env()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=11,  # cap(2) + 9 retries = one below close
    )

    should_continue = await _handle_mill_unreachable(env, info, info.id)

    assert should_continue is False
    # Let the fire-and-forget delivery background task run.
    await asyncio.sleep(0)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.status is SubsessionStatus.CLOSED
    assert updated.close_reason == "mill_unreachable"
    assert updated.summary is not None
    assert "Mill unreachable" in updated.summary

    # Summary was delivered to the conversation store.
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    label, reply = history[0]
    assert "mill_unreachable" in label
    assert "Mill unreachable" in reply


# -- _reset_mill_failure_counter ---------------------------------------------


@pytest.mark.asyncio
async def test_reset_mill_failure_counter_clears_on_success():
    """After a successful mill query the failure counter is reset to 0."""
    env = build_env()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=1,
    )

    _reset_mill_failure_counter(env, info, info.id)

    updated = env.registry.get(info.id)
    assert updated is not None
    assert updated.checkpoint is not None
    assert updated.checkpoint.get("consecutive_mill_failures") == 0


@pytest.mark.asyncio
async def test_reset_mill_failure_counter_noop_when_already_zero():
    """Calling reset when counter is already 0 is harmless (no error)."""
    env = build_env()
    info = _make_checkpoint_info(
        env,
        ticket_id="TICKET-1",
        consecutive_mill_failures=0,
    )

    # Should not raise.
    _reset_mill_failure_counter(env, info, info.id)

    updated = env.registry.get(info.id)
    assert updated is not None
    # Counter stays 0 (or is absent from checkpoint if already 0/absent).
    ck = updated.checkpoint or {}
    assert ck.get("consecutive_mill_failures", 0) == 0

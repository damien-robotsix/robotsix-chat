"""Tests for the ``POST /mill-events`` endpoint."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from robotsix_chat.chat.server.routes.mill_events import mill_events_endpoint
from tests.conftest import mock_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mill_event_request(
    body: dict[str, object] | None,
    *,
    app: object | None = None,
) -> Request:
    """Build a minimal Starlette ``Request`` for ``POST /mill-events``."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/mill-events",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    if app is not None:
        scope["app"] = app

    body_bytes = json.dumps(body).encode() if body is not None else b""

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    return Request(scope, receive)


def _make_mill_event_request_raw(
    raw_body: bytes,
    *,
    app: object | None = None,
) -> Request:
    """Build a ``Request`` with a raw (non-JSON or malformed) byte body."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/mill-events",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    if app is not None:
        scope["app"] = app

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return Request(scope, receive)


def _mock_registry(return_value: int = 0) -> MagicMock:
    """Return a MagicMock with ``route_mill_event`` returning *return_value*."""
    registry = MagicMock()
    registry.route_mill_event.return_value = return_value
    return registry


# ---------------------------------------------------------------------------
# Tests — error paths (400)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_required_fields_all() -> None:
    """Body with none of the required fields returns 400."""
    request = _make_mill_event_request({"extra": "data"})

    with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
        response = await mill_events_endpoint(request)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["status"] == "error"
    assert "missing required fields" in body["detail"]
    # All three required fields should be listed
    for field in ("new_state", "old_state", "ticket_id"):
        assert field in body["detail"]
    mock_cache.put_from_mill_event.assert_not_called()


@pytest.mark.asyncio
async def test_missing_partial_fields() -> None:
    """Body missing only some required fields returns 400 and lists them."""
    request = _make_mill_event_request({"ticket_id": "t1"})

    with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
        response = await mill_events_endpoint(request)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["status"] == "error"
    assert "missing required fields" in body["detail"]
    assert "new_state" in body["detail"]
    assert "old_state" in body["detail"]
    assert "ticket_id" not in body["detail"]
    mock_cache.put_from_mill_event.assert_not_called()


@pytest.mark.asyncio
async def test_non_string_ticket_id() -> None:
    """A numeric ticket_id returns 400."""
    request = _make_mill_event_request(
        {
            "ticket_id": 123,
            "old_state": "open",
            "new_state": "closed",
        }
    )

    with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
        response = await mill_events_endpoint(request)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["status"] == "error"
    assert body["detail"] == "ticket_id must be a non-empty string"
    mock_cache.put_from_mill_event.assert_not_called()


@pytest.mark.asyncio
async def test_blank_ticket_id() -> None:
    """An empty-string ticket_id returns 400."""
    request = _make_mill_event_request(
        {
            "ticket_id": "",
            "old_state": "open",
            "new_state": "closed",
        }
    )

    with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
        response = await mill_events_endpoint(request)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["status"] == "error"
    assert body["detail"] == "ticket_id must be a non-empty string"
    mock_cache.put_from_mill_event.assert_not_called()


@pytest.mark.asyncio
async def test_whitespace_ticket_id() -> None:
    """A whitespace-only ticket_id returns 400."""
    request = _make_mill_event_request(
        {
            "ticket_id": "   ",
            "old_state": "open",
            "new_state": "closed",
        }
    )

    with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
        response = await mill_events_endpoint(request)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["status"] == "error"
    assert body["detail"] == "ticket_id must be a non-empty string"
    mock_cache.put_from_mill_event.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_json() -> None:
    """Malformed JSON body raises 400 HTTPException via ``_parse_json_body``."""
    from starlette.exceptions import HTTPException

    request = _make_mill_event_request_raw(b"not valid json {")

    with pytest.raises(HTTPException) as exc_info:
        await mill_events_endpoint(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "invalid JSON body"


# ---------------------------------------------------------------------------
# Tests — no-registry no-op path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_registry_available() -> None:
    """When subsession_registry is None, returns 200 with woken: 0."""
    async with mock_app() as f:
        request = _make_mill_event_request(
            {
                "ticket_id": "t1",
                "old_state": "open",
                "new_state": "closed",
            },
            app=f.app,
        )

        with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
            response = await mill_events_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body == {"status": "ok", "woken": 0}
    mock_cache.put_from_mill_event.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_woken_zero() -> None:
    """Registry returns 0 woken monitors → 200 with woken: 0."""
    async with mock_app() as f:
        registry = _mock_registry(return_value=0)
        f.app.state.subsession_registry = registry

        request = _make_mill_event_request(
            {
                "ticket_id": "t1",
                "old_state": "open",
                "new_state": "closed",
            },
            app=f.app,
        )

        with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
            response = await mill_events_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body == {"status": "ok", "woken": 0}
    registry.route_mill_event.assert_called_once()
    mock_cache.put_from_mill_event.assert_called_once()


@pytest.mark.asyncio
async def test_happy_path_woken_one() -> None:
    """Registry returns 1 woken monitor → 200 with woken: 1."""
    async with mock_app() as f:
        registry = _mock_registry(return_value=1)
        f.app.state.subsession_registry = registry

        request = _make_mill_event_request(
            {
                "ticket_id": "t2",
                "old_state": "in_progress",
                "new_state": "done",
            },
            app=f.app,
        )

        with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
            response = await mill_events_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body == {"status": "ok", "woken": 1}
    registry.route_mill_event.assert_called_once()
    mock_cache.put_from_mill_event.assert_called_once()


@pytest.mark.asyncio
async def test_happy_path_woken_multiple() -> None:
    """Registry returns multiple woken monitors → 200 with correct count."""
    async with mock_app() as f:
        registry = _mock_registry(return_value=3)
        f.app.state.subsession_registry = registry

        request = _make_mill_event_request(
            {
                "ticket_id": "t3",
                "old_state": "draft",
                "new_state": "implementing",
                "board_id": "b1",
                "repo_id": "r1",
                "timestamp": "2025-01-01T00:00:00Z",
            },
            app=f.app,
        )

        with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
            response = await mill_events_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body == {"status": "ok", "woken": 3}
    registry.route_mill_event.assert_called_once()
    mock_cache.put_from_mill_event.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — event payload forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_payload_passed_to_registry() -> None:
    """The full event payload is forwarded to ``route_mill_event``."""
    async with mock_app() as f:
        registry = _mock_registry(return_value=1)
        f.app.state.subsession_registry = registry

        payload = {
            "ticket_id": "t-abc",
            "old_state": "draft",
            "new_state": "in_progress",
            "board_id": "board-1",
            "repo_id": "repo-2",
            "timestamp": "2025-06-15T12:00:00Z",
        }
        request = _make_mill_event_request(payload, app=f.app)

        with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
            response = await mill_events_endpoint(request)

    assert response.status_code == 200
    registry.route_mill_event.assert_called_once_with("t-abc", payload)
    mock_cache.put_from_mill_event.assert_called_once_with(payload)


@pytest.mark.asyncio
async def test_optional_fields_default_to_empty_string() -> None:
    """Missing optional fields (board_id, repo_id, timestamp) default to ''."""
    async with mock_app() as f:
        registry = _mock_registry(return_value=0)
        f.app.state.subsession_registry = registry

        request = _make_mill_event_request(
            {
                "ticket_id": "t-min",
                "old_state": "open",
                "new_state": "closed",
            },
            app=f.app,
        )

        with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
            response = await mill_events_endpoint(request)

    assert response.status_code == 200
    expected_payload: dict[str, object] = {
        "ticket_id": "t-min",
        "old_state": "open",
        "new_state": "closed",
        "board_id": "",
        "repo_id": "",
        "timestamp": "",
    }
    registry.route_mill_event.assert_called_once_with("t-min", expected_payload)
    mock_cache.put_from_mill_event.assert_called_once_with(expected_payload)


# ---------------------------------------------------------------------------
# Tests — blocked-transition notification
# ---------------------------------------------------------------------------


def _mock_event_bus() -> MagicMock:
    """Return a MagicMock for the EventBus to capture publish calls."""
    return MagicMock()


def _mock_registry_with_owners(
    owner_session_ids: set[str] | None = None,
    *,
    route_return_value: int = 0,
) -> MagicMock:
    """Registry mock with route_mill_event and get_owner_session_ids_for_ticket."""
    registry = MagicMock()
    registry.route_mill_event.return_value = route_return_value
    registry.get_owner_session_ids_for_ticket.return_value = (
        owner_session_ids if owner_session_ids is not None else set()
    )
    return registry


@pytest.mark.asyncio
async def test_blocked_transition_notifies_owner_sessions() -> None:
    """Blocked transition publishes SSE notification + agent_message."""
    async with mock_app() as f:
        event_bus = _mock_event_bus()
        f.app.state.event_bus = event_bus
        registry = _mock_registry_with_owners(
            {"session-1", "session-2"}, route_return_value=1
        )
        f.app.state.subsession_registry = registry

        request = _make_mill_event_request(
            {
                "ticket_id": "t-blocked-1",
                "old_state": "implementing",
                "new_state": "blocked",
            },
            app=f.app,
        )

        with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache") as mock_cache:
            response = await mill_events_endpoint(request)

    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ok", "woken": 1}
    registry.get_owner_session_ids_for_ticket.assert_called_once_with("t-blocked-1")

    # Both session-1 and session-2 should get two publishes each
    # (notification + agent_message).
    assert event_bus.publish.call_count == 4
    published_session_ids = {call.args[0] for call in event_bus.publish.call_args_list}
    assert published_session_ids == {"session-1", "session-2"}

    # First call to each session should be the SSE notification.
    session1_calls = [
        c for c in event_bus.publish.call_args_list if c.args[0] == "session-1"
    ]
    assert len(session1_calls) == 2
    notification_frame = session1_calls[0].args[1]
    assert notification_frame["type"] == "notification"
    assert "BLOCKED" in notification_frame["title"]
    assert notification_frame["urgency"] == "high"

    agent_message = session1_calls[1].args[1]
    assert agent_message["type"] == "agent_message"
    assert "BLOCKED" in agent_message["text"]

    mock_cache.put_from_mill_event.assert_called_once()


@pytest.mark.asyncio
async def test_blocked_transition_no_owners_still_returns_ok() -> None:
    """Blocked transition with zero owner sessions returns 200 gracefully."""
    async with mock_app() as f:
        event_bus = _mock_event_bus()
        f.app.state.event_bus = event_bus
        registry = _mock_registry_with_owners(set(), route_return_value=0)
        f.app.state.subsession_registry = registry

        request = _make_mill_event_request(
            {
                "ticket_id": "t-blocked-2",
                "old_state": "in_progress",
                "new_state": "blocked",
            },
            app=f.app,
        )

        with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache"):
            response = await mill_events_endpoint(request)

    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ok", "woken": 0}
    # No publishes because there are no owner sessions.
    event_bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_to_blocked_transition_no_notification() -> None:
    """old_state already blocked → no notification (transition was not INTO blocked)."""
    async with mock_app() as f:
        event_bus = _mock_event_bus()
        f.app.state.event_bus = event_bus
        registry = _mock_registry_with_owners({"session-1"}, route_return_value=1)
        f.app.state.subsession_registry = registry

        request = _make_mill_event_request(
            {
                "ticket_id": "t-blocked-3",
                "old_state": "blocked",
                "new_state": "blocked",
            },
            app=f.app,
        )

        with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache"):
            response = await mill_events_endpoint(request)

    assert response.status_code == 200
    # Should still route (monitors get woken even for blocked→blocked),
    # but should NOT publish blocked notifications.
    registry.route_mill_event.assert_called_once()
    event_bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_non_blocked_transition_no_notification() -> None:
    """A normal open→closed transition should not trigger blocked notification."""
    async with mock_app() as f:
        event_bus = _mock_event_bus()
        f.app.state.event_bus = event_bus
        registry = _mock_registry_with_owners({"session-1"}, route_return_value=1)
        f.app.state.subsession_registry = registry

        request = _make_mill_event_request(
            {
                "ticket_id": "t-normal",
                "old_state": "open",
                "new_state": "closed",
            },
            app=f.app,
        )

        with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache"):
            response = await mill_events_endpoint(request)

    assert response.status_code == 200
    event_bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_transition_no_event_bus_returns_ok() -> None:
    """Blocked transition without event_bus on app state → graceful no-op."""
    async with mock_app() as f:
        # Do NOT set f.app.state.event_bus.
        registry = _mock_registry_with_owners({"session-1"}, route_return_value=1)
        f.app.state.subsession_registry = registry

        request = _make_mill_event_request(
            {
                "ticket_id": "t-blocked-4",
                "old_state": "implementing",
                "new_state": "blocked",
            },
            app=f.app,
        )

        with patch("robotsix_chat.ticket_poll.cache.ticket_state_cache"):
            response = await mill_events_endpoint(request)

    assert response.status_code == 200
    # No event_bus → no publish → graceful skip.
    assert json.loads(response.body) == {"status": "ok", "woken": 1}

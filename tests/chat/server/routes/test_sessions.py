"""Unit tests for the session lifecycle endpoint handlers in ``sessions.py``."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

from robotsix_chat.chat.events import SSE_AUTONOMOUS_STATE_TYPE
from robotsix_chat.chat.server.routes.sessions import (
    _cleanup_session,
    _require_owner_id,
    history_endpoint,
    sessions_approve_endpoint,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
    sessions_reject_endpoint,
    summary_endpoint,
)

# ---------------------------------------------------------------------------
# Request factories (inspired by test_shared.py)
# ---------------------------------------------------------------------------


def _make_json_request(body: object, *, path: str = "/") -> Request:
    """Build a minimal Starlette ``Request`` with a JSON body."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": path,
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    body_bytes = json.dumps(body).encode() if body is not None else b""

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    return Request(scope, receive)


def _make_query_request(query_string: str, *, path: str = "/") -> Request:
    """Build a minimal Starlette ``Request`` with the given query string."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": path,
        "query_string": query_string.encode(),
        "headers": [],
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    return Request(scope, receive)


def _make_request(
    *,
    method: str = "GET",
    path: str = "/",
    query_string: str = "",
    path_params: dict[str, str] | None = None,
    app_state: object | None = None,
) -> Request:
    """Build a minimal Starlette ``Request`` with full control over scope."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": path,
        "query_string": query_string.encode(),
        "headers": [],
        "path_params": path_params or {},
    }
    if app_state is not None:
        scope["app"] = type("FakeApp", (), {"state": app_state})()

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    return Request(scope, receive)


# ---------------------------------------------------------------------------
# _cleanup_session
# ---------------------------------------------------------------------------


def test_cleanup_session_none_registry() -> None:
    """Returns 0 when the subsession registry is None."""
    state = MagicMock(subsession_registry=None)
    request = _make_request(app_state=state)

    result = _cleanup_session("sess-1", request)
    assert result == 0


def test_cleanup_session_delegates_to_registry() -> None:
    """Calls ``close_all_for_owner`` and returns its int result."""
    mock_registry = MagicMock()
    mock_registry.close_all_for_owner.return_value = 3
    state = MagicMock(subsession_registry=mock_registry)
    request = _make_request(app_state=state)

    result = _cleanup_session("sess-1", request)
    assert result == 3
    mock_registry.close_all_for_owner.assert_called_once_with(
        "sess-1", reason="session closed"
    )


def test_cleanup_session_registry_returns_zero() -> None:
    """Returns the registry's return value even when it is 0."""
    mock_registry = MagicMock()
    mock_registry.close_all_for_owner.return_value = 0
    state = MagicMock(subsession_registry=mock_registry)
    request = _make_request(app_state=state)

    result = _cleanup_session("sess-x", request)
    assert result == 0


# ---------------------------------------------------------------------------
# _require_owner_id
# ---------------------------------------------------------------------------


def test_require_owner_id_present() -> None:
    """Returns the owner_id when present in query params."""
    request = _make_query_request("owner_id=alice")
    result = _require_owner_id(request)
    assert result == "alice"


def test_require_owner_id_missing() -> None:
    """Raises 400 when owner_id is absent from query params."""
    request = _make_query_request("")
    with pytest.raises(HTTPException) as exc_info:
        _require_owner_id(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "owner_id query parameter is required"


def test_require_owner_id_empty_string() -> None:
    """Raises 400 when owner_id is present but an empty string."""
    request = _make_query_request("owner_id=")
    with pytest.raises(HTTPException) as exc_info:
        _require_owner_id(request)
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# history_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_endpoint_returns_turns() -> None:
    """Returns conversation history from the store."""
    mock_store = MagicMock()
    mock_store.history.return_value = [("Q", "A"), ("Q2", "A2")]
    state = MagicMock(conversation_store=mock_store)
    request = _make_query_request("session_id=sess-1")
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    response = await history_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body == {"turns": [["Q", "A"], ["Q2", "A2"]]}
    mock_store.history.assert_called_once_with("sess-1")


@pytest.mark.asyncio
async def test_history_endpoint_client_id_fallback() -> None:
    """Tolerates client_id as a legacy fallback for session_id."""
    mock_store = MagicMock()
    mock_store.history.return_value = []
    state = MagicMock(conversation_store=mock_store)
    request = _make_query_request("client_id=legacy-1")
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    response = await history_endpoint(request)
    assert response.status_code == 200
    assert json.loads(response.body) == {"turns": []}  # type: ignore[arg-type]
    mock_store.history.assert_called_once_with("legacy-1")


@pytest.mark.asyncio
async def test_history_endpoint_missing_both_params() -> None:
    """Raises 400 when neither session_id nor client_id is provided."""
    request = _make_query_request("")
    with pytest.raises(HTTPException) as exc_info:
        await history_endpoint(request)
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# sessions_list_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_list_endpoint_basic() -> None:
    """Returns sessions list and active_session_id."""
    mock_store = MagicMock()
    mock_store.list_sessions.return_value = (
        [{"session_id": "s1", "title": "Chat 1"}],
        "s1",
    )
    state = MagicMock(conversation_store=mock_store, autonomous_runner=None)
    request = _make_query_request("owner_id=alice")
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    response = await sessions_list_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["sessions"] == [{"session_id": "s1", "title": "Chat 1"}]
    assert body["active_session_id"] == "s1"
    mock_store.list_sessions.assert_called_once_with("alice")


@pytest.mark.asyncio
async def test_sessions_list_endpoint_missing_owner_id() -> None:
    """Raises 400 when owner_id is missing."""
    request = _make_query_request("")
    with pytest.raises(HTTPException) as exc_info:
        await sessions_list_endpoint(request)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_sessions_list_endpoint_autonomous_annotations() -> None:
    """Annotates autonomous sessions with state, plan_text, and turn counts."""
    from robotsix_chat.autonomous.models import AutonomousState

    mock_store = MagicMock()
    mock_store.list_sessions.return_value = (
        [{"session_id": "auto-1"}, {"session_id": "manual-1"}],
        "auto-1",
    )

    mock_runner = MagicMock()
    mock_runner.is_autonomous.side_effect = lambda sid: sid == "auto-1"
    mock_runner.get_state.return_value = AutonomousState.executing
    mock_runner.max_auto_turns = 50
    mock_runner.session_color = "#ff0000"

    fake_session = MagicMock()
    fake_session.plan_text = "Do stuff"
    fake_session.auto_turn_count = 5
    mock_runner.get_session.return_value = fake_session

    state = MagicMock(conversation_store=mock_store, autonomous_runner=mock_runner)
    request = _make_query_request("owner_id=bob")
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    response = await sessions_list_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["sessions"][0]["autonomous"] is True
    assert body["sessions"][0][SSE_AUTONOMOUS_STATE_TYPE] == "executing"
    assert body["sessions"][0]["autonomous_plan_text"] == "Do stuff"
    assert body["sessions"][0]["autonomous_turn_count"] == 5
    assert body["sessions"][0]["autonomous_max_turns"] == 50
    assert body["sessions"][0]["autonomous_session_color"] == "#ff0000"
    # Manual session should remain unannotated.
    assert "autonomous" not in body["sessions"][1]


@pytest.mark.asyncio
async def test_sessions_list_endpoint_autonomous_none_state_and_session() -> None:
    """Gracefully handles get_state/get_session returning None."""
    mock_store = MagicMock()
    mock_store.list_sessions.return_value = (
        [{"session_id": "auto-1"}],
        "auto-1",
    )
    mock_runner = MagicMock()
    mock_runner.is_autonomous.return_value = True
    mock_runner.get_state.return_value = None
    mock_runner.get_session.return_value = None

    state = MagicMock(conversation_store=mock_store, autonomous_runner=mock_runner)
    request = _make_query_request("owner_id=bob")
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    response = await sessions_list_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    s = body["sessions"][0]
    assert s["autonomous"] is True
    assert SSE_AUTONOMOUS_STATE_TYPE not in s
    assert "autonomous_plan_text" not in s
    assert "autonomous_turn_count" not in s


# ---------------------------------------------------------------------------
# sessions_create_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_create_endpoint_normal() -> None:
    """Creates a regular session via the conversation store."""
    mock_store = MagicMock()
    mock_store.create_session.return_value = {
        "session_id": "new-sess",
        "title": "New chat",
        "last_active": 0.0,
        "turn_count": 0,
    }
    state = MagicMock(conversation_store=mock_store, autonomous_runner=None)
    request = _make_json_request({"owner_id": "alice"})
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    response = await sessions_create_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["session_id"] == "new-sess"
    mock_store.create_session.assert_called_once_with("alice")


@pytest.mark.asyncio
async def test_sessions_create_endpoint_missing_owner_id() -> None:
    """Raises 400 when owner_id is missing from the body."""
    request = _make_json_request({})
    with pytest.raises(HTTPException) as exc_info:
        await sessions_create_endpoint(request)
    assert exc_info.value.status_code == 400
    assert "owner_id" in exc_info.value.detail


@pytest.mark.asyncio
async def test_sessions_create_endpoint_owner_id_wrong_type() -> None:
    """Raises 400 when owner_id is not a string."""
    request = _make_json_request({"owner_id": 123})
    with pytest.raises(HTTPException) as exc_info:
        await sessions_create_endpoint(request)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_sessions_create_endpoint_autonomous() -> None:
    """Creates an autonomous session when autonomous=true."""
    from robotsix_chat.autonomous.models import AutonomousState

    mock_runner = MagicMock()
    fake_aq = MagicMock()
    fake_aq.session_id = "auto-sess"
    fake_aq.state = AutonomousState.selecting_subject
    mock_runner.create_session.return_value = fake_aq

    state = MagicMock(conversation_store=MagicMock(), autonomous_runner=mock_runner)
    request = _make_json_request({"owner_id": "bob", "autonomous": True})
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    response = await sessions_create_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["session_id"] == "auto-sess"
    assert body["autonomous"] is True
    mock_runner.create_session.assert_called_once_with("bob")


@pytest.mark.asyncio
async def test_sessions_create_endpoint_autonomous_disabled() -> None:
    """Raises 404 when autonomous is requested but runner is None."""
    state = MagicMock(conversation_store=MagicMock(), autonomous_runner=None)
    request = _make_json_request({"owner_id": "bob", "autonomous": True})
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    with pytest.raises(HTTPException) as exc_info:
        await sessions_create_endpoint(request)
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "autonomous sessions are not enabled"


# ---------------------------------------------------------------------------
# sessions_delete_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_delete_endpoint_success() -> None:
    """Deletes a session and returns the active_session_id."""
    mock_store = MagicMock()
    mock_store.history.return_value = [("Q", "A")]
    mock_store.delete_session.return_value = {
        "deleted": True,
        "active_session_id": "other-sess",
    }

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=None,
    )
    request = _make_request(
        method="DELETE",
        query_string="owner_id=alice",
        path_params={"session_id": "sess-1"},
        app_state=state,
    )

    response = await sessions_delete_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["deleted"] is True
    assert body["active_session_id"] == "other-sess"
    assert body["subsessions_closed"] == 0
    mock_store.delete_session.assert_called_once_with("alice", "sess-1")


@pytest.mark.asyncio
async def test_sessions_delete_endpoint_not_found() -> None:
    """Returns 404 when the session is not found."""
    mock_store = MagicMock()
    mock_store.history.return_value = []
    mock_store.delete_session.return_value = {"deleted": False}

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=None,
    )
    request = _make_request(
        method="DELETE",
        query_string="owner_id=alice",
        path_params={"session_id": "missing"},
        app_state=state,
    )

    response = await sessions_delete_endpoint(request)
    assert response.status_code == 404
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["error"] == "session not found"


@pytest.mark.asyncio
async def test_sessions_delete_endpoint_missing_owner_id() -> None:
    """Raises 400 when owner_id is missing."""
    request = _make_request(
        method="DELETE",
        query_string="",
        path_params={"session_id": "sess-1"},
    )
    with pytest.raises(HTTPException) as exc_info:
        await sessions_delete_endpoint(request)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_sessions_delete_endpoint_cleans_up_subsessions() -> None:
    """Calls _cleanup_session and reports subsessions_closed count."""
    mock_registry = MagicMock()
    mock_registry.close_all_for_owner.return_value = 2
    mock_store = MagicMock()
    mock_store.history.return_value = [("Q", "A")]
    mock_store.delete_session.return_value = {
        "deleted": True,
        "active_session_id": "other",
    }

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=mock_registry,
        feedback_runner=None,
    )
    request = _make_request(
        method="DELETE",
        query_string="owner_id=alice",
        path_params={"session_id": "sess-1"},
        app_state=state,
    )

    response = await sessions_delete_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["subsessions_closed"] == 2
    mock_registry.close_all_for_owner.assert_called_once_with(
        "sess-1", reason="session closed"
    )


@pytest.mark.asyncio
async def test_sessions_delete_endpoint_schedules_feedback() -> None:
    """Schedules feedback when feedback_runner is configured and history exists."""
    mock_store = MagicMock()
    mock_store.history.return_value = [("Q", "A")]
    mock_store.delete_session.return_value = {
        "deleted": True,
        "active_session_id": "other",
    }
    mock_feedback = MagicMock()

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=mock_feedback,
    )
    request = _make_request(
        method="DELETE",
        query_string="owner_id=alice",
        path_params={"session_id": "sess-1"},
        app_state=state,
    )

    response = await sessions_delete_endpoint(request)
    assert response.status_code == 200
    mock_feedback.schedule.assert_called_once_with(
        "session_end", "sess-1", [("Q", "A")]
    )


@pytest.mark.asyncio
async def test_sessions_delete_endpoint_no_feedback_on_empty_history() -> None:
    """Does not schedule feedback when history is empty."""
    mock_store = MagicMock()
    mock_store.history.return_value = []
    mock_store.delete_session.return_value = {
        "deleted": True,
        "active_session_id": "other",
    }
    mock_feedback = MagicMock()

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=mock_feedback,
    )
    request = _make_request(
        method="DELETE",
        query_string="owner_id=alice",
        path_params={"session_id": "sess-1"},
        app_state=state,
    )

    response = await sessions_delete_endpoint(request)
    assert response.status_code == 200
    mock_feedback.schedule.assert_not_called()


# ---------------------------------------------------------------------------
# sessions_close_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_close_endpoint_success() -> None:
    """Closes a session and returns success."""
    mock_store = MagicMock()
    mock_store.history.return_value = []
    mock_store.close_session.return_value = {"closed": True}

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=None,
    )
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "sess-1"},
        app_state=state,
    )

    response = await sessions_close_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["closed"] is True
    assert body["session_id"] == "sess-1"
    assert body["subsessions_closed"] == 0
    mock_store.close_session.assert_called_once_with("alice", "sess-1")


@pytest.mark.asyncio
async def test_sessions_close_endpoint_not_found() -> None:
    """Returns 404 when the session is not found."""
    mock_store = MagicMock()
    mock_store.close_session.return_value = {"closed": False}

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=None,
    )
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "missing"},
        app_state=state,
    )

    response = await sessions_close_endpoint(request)
    assert response.status_code == 404
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["error"] == "session not found"


@pytest.mark.asyncio
async def test_sessions_close_endpoint_missing_owner_id() -> None:
    """Raises 400 when owner_id is missing."""
    request = _make_request(
        method="POST",
        query_string="",
        path_params={"session_id": "sess-1"},
    )
    with pytest.raises(HTTPException) as exc_info:
        await sessions_close_endpoint(request)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_sessions_close_endpoint_cleans_up_subsessions() -> None:
    """Reports subsessions_closed count from the registry."""
    mock_registry = MagicMock()
    mock_registry.close_all_for_owner.return_value = 3
    mock_store = MagicMock()
    mock_store.history.return_value = []
    mock_store.close_session.return_value = {"closed": True}

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=mock_registry,
        feedback_runner=None,
    )
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "sess-1"},
        app_state=state,
    )

    response = await sessions_close_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["subsessions_closed"] == 3


@pytest.mark.asyncio
async def test_sessions_close_endpoint_schedules_feedback() -> None:
    """Schedules feedback on close when history exists."""
    mock_store = MagicMock()
    mock_store.history.return_value = [("Q", "A")]
    mock_store.close_session.return_value = {"closed": True}
    mock_feedback = MagicMock()

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=mock_feedback,
    )
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "sess-1"},
        app_state=state,
    )

    response = await sessions_close_endpoint(request)
    assert response.status_code == 200
    mock_feedback.schedule.assert_called_once_with(
        "session_end", "sess-1", [("Q", "A")]
    )


@pytest.mark.asyncio
async def test_sessions_close_endpoint_no_feedback_on_empty_history() -> None:
    """Does not schedule feedback when history is empty on close."""
    mock_store = MagicMock()
    mock_store.history.return_value = []
    mock_store.close_session.return_value = {"closed": True}
    mock_feedback = MagicMock()

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=mock_feedback,
    )
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "sess-1"},
        app_state=state,
    )

    response = await sessions_close_endpoint(request)
    assert response.status_code == 200
    mock_feedback.schedule.assert_not_called()


# ---------------------------------------------------------------------------
# sessions_approve_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_approve_endpoint_success() -> None:
    """Approves an autonomous session and returns success."""
    mock_runner = MagicMock()
    mock_runner.approve.return_value = (True, "")

    state = MagicMock(autonomous_runner=mock_runner)
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_approve_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["approved"] is True
    mock_runner.approve.assert_called_once_with("alice", "auto-1")


@pytest.mark.asyncio
async def test_sessions_approve_endpoint_owner_mismatch() -> None:
    """Returns 403 when owner_id does not match."""
    mock_runner = MagicMock()
    mock_runner.approve.return_value = (False, "owner_id mismatch")

    state = MagicMock(autonomous_runner=mock_runner)
    request = _make_request(
        method="POST",
        query_string="owner_id=eve",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_approve_endpoint(request)
    assert response.status_code == 403
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["error"] == "owner_id mismatch"


@pytest.mark.asyncio
async def test_sessions_approve_endpoint_wrong_state() -> None:
    """Returns 409 when session is not awaiting_approval."""
    mock_runner = MagicMock()
    mock_runner.approve.return_value = (
        False,
        "session is in state executing, not awaiting_approval",
    )

    state = MagicMock(autonomous_runner=mock_runner)
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_approve_endpoint(request)
    assert response.status_code == 409
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert "executing" in body["error"]


@pytest.mark.asyncio
async def test_sessions_approve_endpoint_not_found() -> None:
    """Returns 404 when the session is not found by the runner."""
    mock_runner = MagicMock()
    mock_runner.approve.return_value = (False, "session not found")

    state = MagicMock(autonomous_runner=mock_runner)
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "unknown"},
        app_state=state,
    )

    response = await sessions_approve_endpoint(request)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_sessions_approve_endpoint_runner_not_configured() -> None:
    """Returns 404 when the autonomous runner is not wired."""
    state = MagicMock(autonomous_runner=None)
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_approve_endpoint(request)
    assert response.status_code == 404
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["error"] == "autonomous sessions are not enabled"


@pytest.mark.asyncio
async def test_sessions_approve_endpoint_missing_owner_id() -> None:
    """Raises 400 when owner_id is missing."""
    request = _make_request(
        method="POST",
        query_string="",
        path_params={"session_id": "auto-1"},
    )
    with pytest.raises(HTTPException) as exc_info:
        await sessions_approve_endpoint(request)
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# sessions_reject_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_reject_endpoint_success() -> None:
    """Rejects an autonomous session and returns success."""
    mock_runner = MagicMock()
    mock_runner.reject.return_value = (True, "")

    state = MagicMock(autonomous_runner=mock_runner)
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_reject_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["rejected"] is True
    mock_runner.reject.assert_called_once_with("alice", "auto-1")


@pytest.mark.asyncio
async def test_sessions_reject_endpoint_owner_mismatch() -> None:
    """Returns 403 when owner_id does not match."""
    mock_runner = MagicMock()
    mock_runner.reject.return_value = (False, "owner_id mismatch")

    state = MagicMock(autonomous_runner=mock_runner)
    request = _make_request(
        method="POST",
        query_string="owner_id=eve",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_reject_endpoint(request)
    assert response.status_code == 403
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["error"] == "owner_id mismatch"


@pytest.mark.asyncio
async def test_sessions_reject_endpoint_wrong_state() -> None:
    """Returns 409 when session is not awaiting_approval."""
    mock_runner = MagicMock()
    mock_runner.reject.return_value = (
        False,
        "session is in state executing, not awaiting_approval",
    )

    state = MagicMock(autonomous_runner=mock_runner)
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_reject_endpoint(request)
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_sessions_reject_endpoint_not_found() -> None:
    """Returns 404 when the session is not found."""
    mock_runner = MagicMock()
    mock_runner.reject.return_value = (False, "session not found")

    state = MagicMock(autonomous_runner=mock_runner)
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "unknown"},
        app_state=state,
    )

    response = await sessions_reject_endpoint(request)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_sessions_reject_endpoint_runner_not_configured() -> None:
    """Returns 404 when the autonomous runner is not wired."""
    state = MagicMock(autonomous_runner=None)
    request = _make_request(
        method="POST",
        query_string="owner_id=alice",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_reject_endpoint(request)
    assert response.status_code == 404
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["error"] == "autonomous sessions are not enabled"


@pytest.mark.asyncio
async def test_sessions_reject_endpoint_missing_owner_id() -> None:
    """Raises 400 when owner_id is missing."""
    request = _make_request(
        method="POST",
        query_string="",
        path_params={"session_id": "auto-1"},
    )
    with pytest.raises(HTTPException) as exc_info:
        await sessions_reject_endpoint(request)
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# summary_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_endpoint_success() -> None:
    """Generates a summary by streaming from the summary agent."""
    mock_agent = MagicMock()
    mock_agent.stream = AsyncMock(return_value=AsyncMock())

    # Simulate streaming tokens.
    async def _fake_stream(*args, **kwargs):
        yield "Brief "
        yield "summary."

    mock_agent.stream = _fake_stream

    mock_store = MagicMock()
    mock_store.history.return_value = [("Hello", "Hi there")]

    state = MagicMock(summary_agent=mock_agent, conversation_store=mock_store)
    request = _make_json_request({"session_id": "sess-1"})
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    with patch(
        "robotsix_chat.chat.server.routes.sessions.build_transcript",
        return_value="User: Hello\nAssistant: Hi there",
    ):
        response = await summary_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["summary"] == "Brief summary."


@pytest.mark.asyncio
async def test_summary_endpoint_empty_history() -> None:
    """Returns an empty summary when the session has no turns."""
    mock_store = MagicMock()
    mock_store.history.return_value = []

    state = MagicMock(summary_agent=MagicMock(), conversation_store=mock_store)
    request = _make_json_request({"session_id": "sess-1"})
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    response = await summary_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["summary"] == ""


@pytest.mark.asyncio
async def test_summary_endpoint_missing_session_id() -> None:
    """Raises 400 when session_id is missing from the body."""
    state = MagicMock(summary_agent=MagicMock(), conversation_store=MagicMock())
    request = _make_json_request({})
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    with pytest.raises(HTTPException) as exc_info:
        await summary_endpoint(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "session_id is required"


@pytest.mark.asyncio
async def test_summary_endpoint_session_id_wrong_type() -> None:
    """Raises 400 when session_id is not a string."""
    state = MagicMock(summary_agent=MagicMock(), conversation_store=MagicMock())
    request = _make_json_request({"session_id": 42})
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    with pytest.raises(HTTPException) as exc_info:
        await summary_endpoint(request)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_summary_endpoint_agent_error() -> None:
    """Returns 500 when the summary agent raises an exception during streaming."""
    mock_store = MagicMock()
    mock_store.history.return_value = [("Q", "A")]

    mock_agent = MagicMock()

    async def _failing_stream(*args, **kwargs):
        yield "start"
        raise RuntimeError("LLM connection lost")

    mock_agent.stream = _failing_stream

    state = MagicMock(summary_agent=mock_agent, conversation_store=mock_store)
    request = _make_json_request({"session_id": "sess-1"})
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    with (
        patch(
            "robotsix_chat.chat.server.routes.sessions.build_transcript",
            return_value="User: Q\nAssistant: A",
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await summary_endpoint(request)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "summary generation failed"

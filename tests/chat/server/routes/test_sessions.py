"""Unit tests for the session lifecycle endpoint handlers in ``sessions.py``."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

from robotsix_chat.autonomous.models import AutonomousState
from robotsix_chat.chat.events import SSE_AUTONOMOUS_STATE_TYPE
from robotsix_chat.chat.server.routes.sessions import (
    _cleanup_session,
    _require_owner_id,
    autonomous_refinements_accept_endpoint,
    autonomous_refinements_list_endpoint,
    autonomous_refinements_reject_endpoint,
    autonomous_refinements_reset_endpoint,
    history_endpoint,
    models_list_endpoint,
    session_model_set_endpoint,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
    summary_endpoint,
)
from robotsix_chat.config.constants import (
    FRONTIER_MODEL_LEVEL,
    level_needs_api_key,
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
async def test_history_endpoint_exposes_compaction_summary() -> None:
    """A compacted session returns its summary and the covered-turn count."""
    from types import SimpleNamespace

    mock_store = MagicMock()
    mock_store.history.return_value = [("Q", "A"), ("Q2", "A2"), ("Q3", "A3")]
    mock_store.get_session.return_value = SimpleNamespace(
        compacted_summary="Earlier we agreed on X.", compacted_turn_index=2
    )
    state = MagicMock(conversation_store=mock_store)
    request = _make_query_request("session_id=sess-1")
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    response = await history_endpoint(request)
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["compacted_summary"] == "Earlier we agreed on X."
    assert body["compacted_turn_index"] == 2
    assert len(body["turns"]) == 3


@pytest.mark.asyncio
async def test_history_endpoint_omits_compaction_keys_when_not_compacted() -> None:
    """Never-compacted sessions keep the plain ``{"turns": ...}`` shape."""
    from types import SimpleNamespace

    mock_store = MagicMock()
    mock_store.history.return_value = [("Q", "A")]
    mock_store.get_session.return_value = SimpleNamespace(
        compacted_summary=None, compacted_turn_index=0
    )
    state = MagicMock(conversation_store=mock_store)
    request = _make_query_request("session_id=sess-1")
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    response = await history_endpoint(request)
    assert json.loads(response.body) == {"turns": [["Q", "A"]]}  # type: ignore[arg-type]


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
    mock_store.list_sessions.assert_called_once_with("alice", create_default=True)


@pytest.mark.asyncio
async def test_sessions_list_endpoint_missing_owner_id() -> None:
    """Raises 400 when owner_id is missing."""
    request = _make_query_request("")
    with pytest.raises(HTTPException) as exc_info:
        await sessions_list_endpoint(request)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_sessions_list_endpoint_autonomous_annotations() -> None:
    """Annotates autonomous sessions with state and turn counts."""
    from robotsix_chat.autonomous.models import AutonomousState

    mock_store = MagicMock()
    mock_store.list_sessions.return_value = (
        [{"session_id": "auto-1"}, {"session_id": "manual-1"}],
        "auto-1",
    )

    mock_runner = MagicMock()
    mock_runner.is_autonomous.side_effect = lambda sid: sid == "auto-1"
    mock_runner.get_state.return_value = AutonomousState.executing

    fake_session = MagicMock()
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
    assert body["sessions"][0]["autonomous_turn_count"] == 5
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
    fake_aq.state = AutonomousState.executing
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
        autonomous_runner=None,
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
    mock_store.delete_session.assert_called_once_with(
        "alice", "sess-1", create_replacement=True
    )


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


# ---------------------------------------------------------------------------
# Autonomous pseudo-owner handling (lifecycle + closability fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_endpoint_no_lazy_default_for_autonomous_owner() -> None:
    """The autonomous pseudo-owner never gets a lazily-created husk session."""
    mock_store = MagicMock()
    mock_store.list_sessions.return_value = ([], "")
    mock_runner = MagicMock()
    mock_runner.bootstrap_owner = "autonomous"
    mock_runner.is_autonomous.return_value = False
    mock_runner.is_autonomous_owner.return_value = True

    state = MagicMock(conversation_store=mock_store, autonomous_runner=mock_runner)
    request = _make_query_request("owner_id=autonomous")
    request.scope["app"] = type("FakeApp", (), {"state": state})()

    response = await sessions_list_endpoint(request)
    assert response.status_code == 200
    mock_store.list_sessions.assert_called_once_with("autonomous", create_default=False)


@pytest.mark.asyncio
async def test_delete_autonomous_session_forgets_and_restarts() -> None:
    """Deleting an autonomous session purges the runner and auto-restarts."""
    mock_store = MagicMock()
    mock_store.history.return_value = []
    mock_store.delete_session.return_value = {
        "deleted": True,
        "active_session_id": "",
    }
    mock_store.owner_for_session.return_value = "autonomous"
    mock_runner = MagicMock()
    mock_runner.bootstrap_owner = "autonomous"
    mock_runner.is_autonomous.return_value = True
    mock_runner.is_autonomous_owner.return_value = True
    mock_runner.get_state.return_value = AutonomousState.executing

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=None,
        autonomous_runner=mock_runner,
    )
    request = _make_request(
        method="DELETE",
        query_string="owner_id=autonomous",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_delete_endpoint(request)
    assert response.status_code == 200
    # No empty husk spawned for the pseudo-owner.
    mock_store.delete_session.assert_called_once_with(
        "autonomous", "auto-1", create_replacement=False
    )
    mock_runner.forget_session.assert_called_once_with("auto-1")
    mock_runner.schedule_restart.assert_called_once_with("autonomous")


@pytest.mark.asyncio
async def test_delete_autonomous_session_in_countdown_hides_without_restart() -> None:
    """Deleting a completed autonomous session hides it without restarting.

    The already-scheduled ``_auto_restart`` re-creates it at the next run.
    """
    mock_store = MagicMock()
    mock_store.history.return_value = []
    mock_store.delete_session.return_value = {
        "deleted": True,
        "active_session_id": "",
    }
    mock_store.owner_for_session.return_value = "autonomous"
    mock_runner = MagicMock()
    mock_runner.bootstrap_owner = "autonomous"
    mock_runner.is_autonomous.return_value = True
    mock_runner.is_autonomous_owner.return_value = True
    mock_runner.get_state.return_value = AutonomousState.completed

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=None,
        autonomous_runner=mock_runner,
    )
    request = _make_request(
        method="DELETE",
        query_string="owner_id=autonomous",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_delete_endpoint(request)
    assert response.status_code == 200
    mock_store.delete_session.assert_called_once_with(
        "autonomous", "auto-1", create_replacement=False
    )
    mock_runner.forget_session.assert_called_once_with("auto-1")
    mock_runner.ensure_active_session.assert_not_called()


@pytest.mark.asyncio
async def test_delete_autonomous_preset_session_retargets_to_subscope_owner() -> None:
    """Deleting a preset session from the merged autonomous list uses its real owner."""
    mock_store = MagicMock()
    mock_store.history.return_value = []
    mock_store.delete_session.return_value = {
        "deleted": True,
        "active_session_id": "",
    }
    mock_store.owner_for_session.return_value = "autonomous:nightly-audit"
    mock_runner = MagicMock()
    mock_runner.bootstrap_owner = "autonomous"
    mock_runner.is_autonomous.return_value = True
    mock_runner.is_autonomous_owner.side_effect = lambda oid: (
        oid
        in (
            "autonomous",
            "autonomous:nightly-audit",
        )
    )

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=None,
        autonomous_runner=mock_runner,
    )
    request = _make_request(
        method="DELETE",
        query_string="owner_id=autonomous",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_delete_endpoint(request)
    assert response.status_code == 200
    mock_store.delete_session.assert_called_once_with(
        "autonomous:nightly-audit", "auto-1", create_replacement=False
    )
    mock_runner.schedule_restart.assert_called_once_with("autonomous:nightly-audit")


@pytest.mark.asyncio
async def test_close_autonomous_session_forgets_and_restarts() -> None:
    """Close an executing autonomous session and schedule a restart.

    Closing while executing purges the runner and schedules a throttled
    restart.
    """
    mock_store = MagicMock()
    mock_store.history.return_value = []
    mock_store.close_session.return_value = {"closed": True}
    mock_runner = MagicMock()
    mock_runner.bootstrap_owner = "autonomous"
    mock_runner.is_autonomous.return_value = True
    mock_runner.get_state.return_value = AutonomousState.executing

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=None,
        autonomous_runner=mock_runner,
    )
    request = _make_request(
        method="POST",
        query_string="owner_id=autonomous",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_close_endpoint(request)
    assert response.status_code == 200
    mock_runner.forget_session.assert_called_once_with("auto-1")
    mock_runner.schedule_restart.assert_called_once_with("autonomous")


@pytest.mark.asyncio
async def test_close_autonomous_countdown_no_restart() -> None:
    """Hide a completed autonomous session without scheduling a restart.

    The already-scheduled ``_auto_restart`` task re-creates it at the next
    run.
    """
    mock_store = MagicMock()
    mock_store.history.return_value = []
    mock_store.close_session.return_value = {"closed": True}
    mock_runner = MagicMock()
    mock_runner.bootstrap_owner = "autonomous"
    mock_runner.is_autonomous.return_value = True
    mock_runner.get_state.return_value = AutonomousState.completed

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=None,
        autonomous_runner=mock_runner,
    )
    request = _make_request(
        method="POST",
        query_string="owner_id=autonomous",
        path_params={"session_id": "auto-1"},
        app_state=state,
    )

    response = await sessions_close_endpoint(request)
    assert response.status_code == 200
    mock_runner.forget_session.assert_called_once_with("auto-1")
    mock_runner.schedule_restart.assert_not_called()


@pytest.mark.asyncio
async def test_delete_non_autonomous_session_leaves_runner_untouched() -> None:
    """A plain browser session delete does not trigger autonomous cleanup."""
    mock_store = MagicMock()
    mock_store.history.return_value = []
    mock_store.delete_session.return_value = {
        "deleted": True,
        "active_session_id": "other",
    }
    mock_runner = MagicMock()
    mock_runner.bootstrap_owner = "autonomous"
    mock_runner.is_autonomous.return_value = False
    mock_runner.is_autonomous_owner.return_value = False

    state = MagicMock(
        conversation_store=mock_store,
        subsession_registry=None,
        feedback_runner=None,
        autonomous_runner=mock_runner,
    )
    request = _make_request(
        method="DELETE",
        query_string="owner_id=alice",
        path_params={"session_id": "sess-1"},
        app_state=state,
    )

    response = await sessions_delete_endpoint(request)
    assert response.status_code == 200
    mock_store.delete_session.assert_called_once_with(
        "alice", "sess-1", create_replacement=True
    )
    mock_runner.forget_session.assert_not_called()
    mock_runner.ensure_active_session.assert_not_called()


# ---------------------------------------------------------------------------
# Refinement endpoints
# ---------------------------------------------------------------------------


class TestAutonomousRefinementsListEndpoint:
    """Tests for ``GET /autonomous/definitions/{name}/refinements``."""

    def test_runner_none_returns_404(self) -> None:
        """404 when autonomous runner is not enabled."""
        state = MagicMock(autonomous_runner=None)
        request = _make_request(
            method="GET",
            path_params={"name": "def1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_list_endpoint(request))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "autonomous sessions are not enabled" in body["error"]

    def test_refinement_store_none_returns_404(self) -> None:
        """404 when refinement store is not available."""
        mock_runner = MagicMock()
        mock_runner.refinement_store = None
        state = MagicMock(autonomous_runner=mock_runner)
        request = _make_request(
            method="GET",
            path_params={"name": "def1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_list_endpoint(request))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "refinement store is not available" in body["error"]

    def test_unknown_definition_returns_404(self) -> None:
        """404 when the definition name is unknown."""
        mock_runner = MagicMock()
        mock_runner.refinement_store = MagicMock()
        mock_runner.get_definition.return_value = None
        state = MagicMock(autonomous_runner=mock_runner)
        request = _make_request(
            method="GET",
            path_params={"name": "unknown"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_list_endpoint(request))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "unknown definition" in body["error"]

    def test_returns_entries(self) -> None:
        """Successful response includes effective_prompt and entries."""
        from robotsix_chat.autonomous.refinement import (
            DefinitionRefinementState,
            RefinementEntry,
        )

        entry = RefinementEntry(
            id="e1",
            timestamp=1234567890.0,
            base_prompt="base",
            previous_addendum="",
            proposed_addendum="lesson",
            feedback_summary="summary",
            session_id="s1",
            status="pending",
        )
        state_obj = DefinitionRefinementState(
            definition_name="def1",
            base_prompt="base",
            accepted_addendum="",
            entries=[entry],
        )

        mock_store = MagicMock()
        mock_store.get_state.return_value = state_obj
        mock_store.effective_prompt.return_value = "base"

        mock_runner = MagicMock()
        mock_runner.refinement_store = mock_store
        mock_runner.get_definition.return_value = {"prompt": "base"}
        state = MagicMock(autonomous_runner=mock_runner)
        request = _make_request(
            method="GET",
            path_params={"name": "def1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_list_endpoint(request))
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["definition_name"] == "def1"
        assert body["effective_prompt"] == "base"
        assert len(body["entries"]) == 1
        assert body["entries"][0]["id"] == "e1"


class TestAutonomousRefinementsAcceptEndpoint:
    """Tests for ``POST .../refinements/{id}/accept``."""

    def test_runner_none_returns_404(self) -> None:
        """404 when autonomous runner is not enabled."""
        state = MagicMock(autonomous_runner=None)
        request = _make_request(
            method="POST",
            path_params={"name": "def1", "refinement_id": "r1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_accept_endpoint(request))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "autonomous sessions are not enabled" in body["error"]

    def test_refinement_store_none_returns_404(self) -> None:
        """404 when refinement store is not available."""
        mock_runner = MagicMock()
        mock_runner.refinement_store = None
        state = MagicMock(autonomous_runner=mock_runner)
        request = _make_request(
            method="POST",
            path_params={"name": "def1", "refinement_id": "r1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_accept_endpoint(request))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "refinement store is not available" in body["error"]

    def test_not_found_returns_404(self) -> None:
        """404 when refinement is not found or not pending."""
        mock_store = MagicMock()
        mock_store.accept_refinement.return_value = False
        mock_runner = MagicMock()
        mock_runner.refinement_store = mock_store
        state = MagicMock(autonomous_runner=mock_runner)
        request = _make_request(
            method="POST",
            path_params={"name": "def1", "refinement_id": "r1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_accept_endpoint(request))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "not found or not pending" in body["error"]

    def test_accept_success_returns_200(self) -> None:
        """200 with {"accepted": true} on success."""
        mock_store = MagicMock()
        mock_store.accept_refinement.return_value = True
        mock_runner = MagicMock()
        mock_runner.refinement_store = mock_store
        state = MagicMock(autonomous_runner=mock_runner)
        request = _make_request(
            method="POST",
            path_params={"name": "def1", "refinement_id": "r1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_accept_endpoint(request))
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["accepted"] is True


class TestAutonomousRefinementsRejectEndpoint:
    """Tests for ``POST .../refinements/{id}/reject``."""

    def test_runner_none_returns_404(self) -> None:
        """404 when autonomous runner is not enabled."""
        state = MagicMock(autonomous_runner=None)
        request = _make_request(
            method="POST",
            path_params={"name": "def1", "refinement_id": "r1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_reject_endpoint(request))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "autonomous sessions are not enabled" in body["error"]

    def test_refinement_store_none_returns_404(self) -> None:
        """404 when refinement store is not available."""
        mock_runner = MagicMock()
        mock_runner.refinement_store = None
        state = MagicMock(autonomous_runner=mock_runner)
        request = _make_request(
            method="POST",
            path_params={"name": "def1", "refinement_id": "r1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_reject_endpoint(request))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "refinement store is not available" in body["error"]

    def test_not_found_returns_404(self) -> None:
        """404 when refinement is not found or not pending."""
        mock_store = MagicMock()
        mock_store.reject_refinement.return_value = False
        mock_runner = MagicMock()
        mock_runner.refinement_store = mock_store
        state = MagicMock(autonomous_runner=mock_runner)
        request = _make_request(
            method="POST",
            path_params={"name": "def1", "refinement_id": "r1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_reject_endpoint(request))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "not found or not pending" in body["error"]

    def test_reject_success_returns_200(self) -> None:
        """200 with {"rejected": true} on success."""
        mock_store = MagicMock()
        mock_store.reject_refinement.return_value = True
        mock_runner = MagicMock()
        mock_runner.refinement_store = mock_store
        state = MagicMock(autonomous_runner=mock_runner)
        request = _make_request(
            method="POST",
            path_params={"name": "def1", "refinement_id": "r1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_reject_endpoint(request))
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["rejected"] is True


class TestAutonomousRefinementsResetEndpoint:
    """Tests for ``POST .../refinements/reset``."""

    def test_runner_none_returns_404(self) -> None:
        """404 when autonomous runner is not enabled."""
        state = MagicMock(autonomous_runner=None)
        request = _make_request(
            method="POST",
            path_params={"name": "def1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_reset_endpoint(request))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "autonomous sessions are not enabled" in body["error"]

    def test_refinement_store_none_returns_404(self) -> None:
        """404 when refinement store is not available."""
        mock_runner = MagicMock()
        mock_runner.refinement_store = None
        state = MagicMock(autonomous_runner=mock_runner)
        request = _make_request(
            method="POST",
            path_params={"name": "def1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_reset_endpoint(request))
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "refinement store is not available" in body["error"]

    def test_reset_success_returns_200(self) -> None:
        """200 with {"reset": true} on success."""
        mock_store = MagicMock()
        mock_store.reset_refinements.return_value = True
        mock_runner = MagicMock()
        mock_runner.refinement_store = mock_store
        state = MagicMock(autonomous_runner=mock_runner)
        request = _make_request(
            method="POST",
            path_params={"name": "def1"},
            app_state=state,
        )
        import asyncio

        response = asyncio.run(autonomous_refinements_reset_endpoint(request))
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["reset"] is True


# ---------------------------------------------------------------------------
# models_list_endpoint / session_model_set_endpoint
# ---------------------------------------------------------------------------


def _first_level_needing_key() -> int:
    """Return the lowest configured level whose provider needs an API key."""
    for level in range(1, FRONTIER_MODEL_LEVEL + 1):
        if level_needs_api_key(level):
            return level
    raise AssertionError("expected at least one keyed level in llmio config")


def _first_keyless_level() -> int:
    """Return the lowest configured level whose provider needs no API key."""
    for level in range(1, FRONTIER_MODEL_LEVEL + 1):
        if not level_needs_api_key(level):
            return level
    raise AssertionError("expected at least one keyless level in llmio config")


@pytest.mark.asyncio
async def test_models_list_endpoint_all_available_with_key() -> None:
    """Every configured level is available when an API key is present."""
    state = MagicMock(chat_api_key_available=True, chat_model_level=3)
    request = _make_request(app_state=state)

    response = await models_list_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["default_level"] == 3
    assert len(body["models"]) == FRONTIER_MODEL_LEVEL
    assert all(m["available"] for m in body["models"])
    assert [m["level"] for m in body["models"]] == list(
        range(1, FRONTIER_MODEL_LEVEL + 1)
    )


@pytest.mark.asyncio
async def test_models_list_endpoint_keyed_unavailable_without_key() -> None:
    """Keyed levels are marked unavailable when no API key is configured."""
    state = MagicMock(chat_api_key_available=False, chat_model_level=2)
    request = _make_request(app_state=state)

    response = await models_list_endpoint(request)
    body = json.loads(response.body)  # type: ignore[arg-type]
    for m in body["models"]:
        assert m["available"] == (not m["needs_api_key"])


@pytest.mark.asyncio
async def test_session_model_set_endpoint_success() -> None:
    """Pins the session, publishes a session_model frame, returns the model."""
    mock_store = MagicMock()
    mock_store.set_model_level.return_value = True
    mock_bus = MagicMock()
    level = _first_keyless_level()
    state = MagicMock(
        conversation_store=mock_store,
        event_bus=mock_bus,
        chat_api_key_available=False,
        chat_model_level=level,
    )
    request = _make_json_request({"level": level})
    request.scope["app"] = type("FakeApp", (), {"state": state})()
    request.scope["path_params"] = {"session_id": "sess-1"}

    response = await session_model_set_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["model_level"] == level
    assert body["session_id"] == "sess-1"
    mock_store.set_model_level.assert_called_once_with("sess-1", level)
    mock_bus.publish.assert_called_once()


@pytest.mark.asyncio
async def test_session_model_set_endpoint_rejects_out_of_range() -> None:
    """Levels outside 1..FRONTIER are rejected with 400."""
    state = MagicMock(chat_api_key_available=True, chat_model_level=3)
    request = _make_json_request({"level": FRONTIER_MODEL_LEVEL + 1})
    request.scope["app"] = type("FakeApp", (), {"state": state})()
    request.scope["path_params"] = {"session_id": "sess-1"}

    with pytest.raises(HTTPException) as exc_info:
        await session_model_set_endpoint(request)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_session_model_set_endpoint_rejects_non_integer() -> None:
    """A non-integer level is rejected with 400."""
    state = MagicMock(chat_api_key_available=True, chat_model_level=3)
    request = _make_json_request({"level": "3"})
    request.scope["app"] = type("FakeApp", (), {"state": state})()
    request.scope["path_params"] = {"session_id": "sess-1"}

    with pytest.raises(HTTPException) as exc_info:
        await session_model_set_endpoint(request)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_session_model_set_endpoint_requires_api_key() -> None:
    """A keyed level is rejected with 409 when no API key is configured."""
    level = _first_level_needing_key()
    state = MagicMock(chat_api_key_available=False, chat_model_level=2)
    request = _make_json_request({"level": level})
    request.scope["app"] = type("FakeApp", (), {"state": state})()
    request.scope["path_params"] = {"session_id": "sess-1"}

    with pytest.raises(HTTPException) as exc_info:
        await session_model_set_endpoint(request)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_session_model_set_endpoint_unknown_session() -> None:
    """Returns 404 when the session is not known to the store."""
    mock_store = MagicMock()
    mock_store.set_model_level.return_value = False
    level = _first_keyless_level()
    state = MagicMock(
        conversation_store=mock_store,
        event_bus=None,
        chat_api_key_available=False,
        chat_model_level=level,
    )
    request = _make_json_request({"level": level})
    request.scope["app"] = type("FakeApp", (), {"state": state})()
    request.scope["path_params"] = {"session_id": "ghost"}

    with pytest.raises(HTTPException) as exc_info:
        await session_model_set_endpoint(request)
    assert exc_info.value.status_code == 404

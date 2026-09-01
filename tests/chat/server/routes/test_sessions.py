"""Unit tests for the session lifecycle endpoint handlers in ``sessions.py``."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

from robotsix_chat.chat.server.routes.sessions import (
    _cleanup_session,
    _require_owner_id,
    history_endpoint,
    models_list_endpoint,
    session_model_set_endpoint,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
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
    state = MagicMock(conversation_store=mock_store, periodic_scheduler=None)
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
    state = MagicMock(conversation_store=mock_store, periodic_scheduler=None)
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
        periodic_scheduler=None,
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
async def test_models_list_endpoint_reports_failover_state() -> None:
    """``/models`` carries provider + llmio failover status for the UI badge."""
    from robotsix_llmio.core.failover import get_failover_tracker
    from robotsix_llmio.exceptions import ProviderExhaustedError

    state = MagicMock(chat_api_key_available=True, chat_model_level=2)
    request = _make_request(app_state=state)

    body = json.loads((await models_list_endpoint(request)).body)  # type: ignore[arg-type]
    assert body["failover"]["failover_active"] is False
    assert body["failover"]["active_slot"] == "default"
    assert all(m["provider"] == "claudeSDK" for m in body["models"])

    get_failover_tracker().record_failure(
        "default", ProviderExhaustedError("weekly cap")
    )
    body = json.loads((await models_list_endpoint(request)).body)  # type: ignore[arg-type]
    assert body["failover"]["failover_active"] is True
    assert body["failover"]["active_slot"] == "fallback"
    assert body["failover"]["failover_until"] is not None
    # The slot-resolved names now come from the OpenRouter fallback slot.
    assert all(m["provider"] == "openrouter" for m in body["models"])
    assert all(m["needs_api_key"] for m in body["models"])


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
    """While failover routes to the keyed slot, a keyless server rejects with 409."""
    from robotsix_llmio.core.failover import get_failover_tracker
    from robotsix_llmio.exceptions import ProviderExhaustedError

    get_failover_tracker().record_failure(
        "default", ProviderExhaustedError("weekly cap")
    )
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


@pytest.mark.asyncio
async def test_models_endpoint_reflects_chat_tier_overrides() -> None:
    """``/models`` shows chat's OWN tier config, not llmio's baked defaults.

    With the fallback-L2-pro override, the L2 row must display the pro
    snapshot during failover while L1 stays flash.
    """
    from robotsix_llmio.config import FALLBACK_LEVEL3
    from robotsix_llmio.core.failover import get_failover_tracker
    from robotsix_llmio.exceptions import ProviderExhaustedError

    state = MagicMock(
        chat_api_key_available=True,
        chat_model_level=2,
        llmio_tier_overrides={"fallback": {"level2": FALLBACK_LEVEL3.model_dump()}},
    )
    request = _make_request(app_state=state)

    get_failover_tracker().record_failure(
        "default", ProviderExhaustedError("weekly cap")
    )
    body = json.loads((await models_list_endpoint(request)).body)  # type: ignore[arg-type]
    rows = {m["level"]: m for m in body["models"]}
    assert rows[2]["name"] == "deepseek/deepseek-v4-pro-0813"
    assert rows[1]["name"] == "deepseek/deepseek-v4-flash-20260731"

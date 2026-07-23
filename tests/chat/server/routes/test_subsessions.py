"""Unit tests for the subsession route handlers in ``subsessions.py``."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.chat.server.routes.subsessions import (
    _get_subsession_registry,
    _resolve_subsession,
    subsessions_close_endpoint,
    subsessions_get_endpoint,
    subsessions_list_endpoint,
    subsessions_message_endpoint,
    subsessions_transcript_endpoint,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_subsession_info(
    *,
    sub_id: str = "sub-1",
    status_value: str = "running",
    summary: str | None = None,
    close_reason: str | None = None,
) -> Mock:
    """Build a mock ``SubsessionInfo`` with the given attributes."""
    info = Mock()
    info.id = sub_id
    info.status = Mock()
    info.status.value = status_value
    info.summary = summary
    info.close_reason = close_reason
    info.transcript = []
    info.snapshot = Mock(return_value={"id": sub_id, "status": status_value})
    return info


def _mock_registry(
    *,
    get_return: Mock | None = None,
    list_return: list[Mock] | None = None,
    enqueue_return: bool = True,
    cancel_and_close_return: Mock | None = None,
) -> Mock:
    """Build a mock ``SubsessionRegistry``."""
    registry = Mock()
    registry.get.return_value = get_return
    if list_return is not None:
        registry.list_for_owner.return_value = list_return
    registry.enqueue_message.return_value = enqueue_return
    registry.cancel_and_close.return_value = cancel_and_close_return
    return registry


def _make_get_request(
    *,
    path: str = "/subsessions",
    query_string: str = "session_id=s1",
    path_params: dict[str, str] | None = None,
    registry: Mock | None = None,
    delivery: Mock | None = None,
) -> Request:
    """Build a minimal Starlette ``Request`` for a GET with optional path params."""
    app = Mock()
    app.state.subsession_registry = registry
    app.state.subsession_delivery = delivery

    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": path,
        "path_params": path_params or {},
        "query_string": query_string.encode(),
        "headers": [],
        "app": app,
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    return Request(scope, receive)


def _make_post_request(
    *,
    path: str = "/subsessions/sub-1/message",
    path_params: dict[str, str] | None = None,
    body: object | None = None,
    registry: Mock | None = None,
    delivery: Mock | None = None,
) -> Request:
    """Build a minimal Starlette ``Request`` for a POST with a JSON body."""
    app = Mock()
    app.state.subsession_registry = registry
    app.state.subsession_delivery = delivery

    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": path,
        "path_params": path_params or {},
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "app": app,
    }

    body_bytes = json.dumps(body).encode() if body is not None else b"{}"

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    return Request(scope, receive)


# ---------------------------------------------------------------------------
# _get_subsession_registry
# ---------------------------------------------------------------------------


def test_get_subsession_registry_returns_registry() -> None:
    """Returns the registry when it is wired on app state."""
    registry = _mock_registry()
    request = _make_get_request(registry=registry)
    result = _get_subsession_registry(request)
    assert result is registry


def test_get_subsession_registry_none_raises_503() -> None:
    """Raises 503 when ``subsession_registry`` is None."""
    request = _make_get_request(registry=None)
    with pytest.raises(HTTPException) as exc_info:
        _get_subsession_registry(request)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "subsessions feature not enabled"


# ---------------------------------------------------------------------------
# _resolve_subsession
# ---------------------------------------------------------------------------


def test_resolve_subsession_success() -> None:
    """Returns (registry, info) when both lookups succeed."""
    info = _mock_subsession_info(sub_id="sub-1")
    registry = _mock_registry(get_return=info)
    request = _make_get_request(
        path="/subsessions/sub-1",
        path_params={"sub_id": "sub-1"},
        registry=registry,
    )
    result = _resolve_subsession(request)
    assert result == (registry, info)
    registry.get.assert_called_once_with("sub-1")


def test_resolve_subsession_no_registry_raises_503() -> None:
    """Raises 503 when the registry is None."""
    request = _make_get_request(
        path="/subsessions/sub-1",
        path_params={"sub_id": "sub-1"},
        registry=None,
    )
    with pytest.raises(HTTPException) as exc_info:
        _resolve_subsession(request)
    assert exc_info.value.status_code == 503


def test_resolve_subsession_unknown_id_raises_404() -> None:
    """Raises 404 when the subsession id is not found."""
    registry = _mock_registry(get_return=None)
    request = _make_get_request(
        path="/subsessions/unknown",
        path_params={"sub_id": "unknown"},
        registry=registry,
    )
    with pytest.raises(HTTPException) as exc_info:
        _resolve_subsession(request)
    assert exc_info.value.status_code == 404
    assert "unknown subsession 'unknown'" in exc_info.value.detail


# ---------------------------------------------------------------------------
# subsessions_list_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_endpoint_returns_snapshots() -> None:
    """GET /subsessions?session_id=... returns snapshot list."""
    info_a = _mock_subsession_info(sub_id="sub-a", status_value="running")
    info_b = _mock_subsession_info(sub_id="sub-b", status_value="closed")

    registry = _mock_registry(list_return=[info_a, info_b])
    request = _make_get_request(
        query_string="session_id=s1",
        registry=registry,
    )

    response = await subsessions_list_endpoint(request)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert "subsessions" in body
    assert len(body["subsessions"]) == 2
    registry.list_for_owner.assert_called_once_with("s1")


@pytest.mark.asyncio
async def test_list_endpoint_empty() -> None:
    """GET /subsessions returns an empty list when there are no subsessions."""
    registry = _mock_registry(list_return=[])
    request = _make_get_request(query_string="session_id=s1", registry=registry)

    response = await subsessions_list_endpoint(request)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body == {"subsessions": []}


@pytest.mark.asyncio
async def test_list_endpoint_no_registry_raises_503() -> None:
    """Raises 503 when the registry is not wired."""
    request = _make_get_request(query_string="session_id=s1", registry=None)
    with pytest.raises(HTTPException) as exc_info:
        await subsessions_list_endpoint(request)
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_list_endpoint_missing_session_id_raises_400() -> None:
    """Raises 400 when ``session_id`` query param is missing."""
    registry = _mock_registry()
    request = _make_get_request(query_string="", registry=registry)
    with pytest.raises(HTTPException) as exc_info:
        await subsessions_list_endpoint(request)
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# subsessions_get_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_endpoint_returns_snapshot_with_transcript() -> None:
    """GET /subsessions/{sub_id} returns snapshot including transcript."""
    info = _mock_subsession_info(sub_id="sub-1")
    registry = _mock_registry(get_return=info)
    request = _make_get_request(
        path="/subsessions/sub-1",
        path_params={"sub_id": "sub-1"},
        registry=registry,
    )

    response = await subsessions_get_endpoint(request)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    info.snapshot.assert_called_once_with(with_transcript=True)


@pytest.mark.asyncio
async def test_get_endpoint_unknown_id_raises_404() -> None:
    """Raises 404 when the subsession id is not found."""
    registry = _mock_registry(get_return=None)
    request = _make_get_request(
        path="/subsessions/unknown",
        path_params={"sub_id": "unknown"},
        registry=registry,
    )
    with pytest.raises(HTTPException) as exc_info:
        await subsessions_get_endpoint(request)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_endpoint_no_registry_raises_503() -> None:
    """Raises 503 when the registry is not wired."""
    request = _make_get_request(
        path="/subsessions/sub-1",
        path_params={"sub_id": "sub-1"},
        registry=None,
    )
    with pytest.raises(HTTPException) as exc_info:
        await subsessions_get_endpoint(request)
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# subsessions_transcript_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcript_endpoint_returns_transcript() -> None:
    """GET /subsessions/{sub_id}/transcript returns subsession_id + transcript."""
    entry = Mock()
    entry.as_dict.return_value = {"role": "user", "text": "hello", "timestamp": 1.0}

    info = _mock_subsession_info(sub_id="sub-1")
    info.transcript = [entry]
    registry = _mock_registry(get_return=info)
    request = _make_get_request(
        path="/subsessions/sub-1/transcript",
        path_params={"sub_id": "sub-1"},
        registry=registry,
    )

    response = await subsessions_transcript_endpoint(request)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body == {
        "subsession_id": "sub-1",
        "transcript": [{"role": "user", "text": "hello", "timestamp": 1.0}],
    }


@pytest.mark.asyncio
async def test_transcript_endpoint_unknown_id_raises_404() -> None:
    """Raises 404 when the subsession id is not found."""
    registry = _mock_registry(get_return=None)
    request = _make_get_request(
        path="/subsessions/unknown/transcript",
        path_params={"sub_id": "unknown"},
        registry=registry,
    )
    with pytest.raises(HTTPException) as exc_info:
        await subsessions_transcript_endpoint(request)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_transcript_endpoint_no_registry_raises_503() -> None:
    """Raises 503 when the registry is not wired."""
    request = _make_get_request(
        path="/subsessions/sub-1/transcript",
        path_params={"sub_id": "sub-1"},
        registry=None,
    )
    with pytest.raises(HTTPException) as exc_info:
        await subsessions_transcript_endpoint(request)
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# subsessions_message_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_endpoint_queues_and_returns_202() -> None:
    """POST /subsessions/{sub_id}/message with valid text returns 202."""
    info = _mock_subsession_info(sub_id="sub-1")
    registry = _mock_registry(get_return=info, enqueue_return=True)
    request = _make_post_request(
        path="/subsessions/sub-1/message",
        path_params={"sub_id": "sub-1"},
        body={"text": "hello"},
        registry=registry,
    )

    response = await subsessions_message_endpoint(request)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 202
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body == {"subsession_id": "sub-1", "status": "queued"}
    registry.enqueue_message.assert_called_once_with("sub-1", "user", "hello")


@pytest.mark.asyncio
async def test_message_endpoint_missing_text_raises_400() -> None:
    """Raises 400 when the ``text`` field is missing from the body."""
    info = _mock_subsession_info(sub_id="sub-1")
    registry = _mock_registry(get_return=info)
    request = _make_post_request(
        path="/subsessions/sub-1/message",
        path_params={"sub_id": "sub-1"},
        body={},
        registry=registry,
    )

    with pytest.raises(HTTPException) as exc_info:
        await subsessions_message_endpoint(request)
    assert exc_info.value.status_code == 400
    assert "'text' field is required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_message_endpoint_empty_text_raises_400() -> None:
    """Raises 400 when the ``text`` field is an empty string."""
    info = _mock_subsession_info(sub_id="sub-1")
    registry = _mock_registry(get_return=info)
    request = _make_post_request(
        path="/subsessions/sub-1/message",
        path_params={"sub_id": "sub-1"},
        body={"text": ""},
        registry=registry,
    )

    with pytest.raises(HTTPException) as exc_info:
        await subsessions_message_endpoint(request)
    assert exc_info.value.status_code == 400
    assert "'text' field is required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_message_endpoint_non_string_text_raises_400() -> None:
    """Raises 400 when the ``text`` field is not a string."""
    info = _mock_subsession_info(sub_id="sub-1")
    registry = _mock_registry(get_return=info)
    request = _make_post_request(
        path="/subsessions/sub-1/message",
        path_params={"sub_id": "sub-1"},
        body={"text": 42},
        registry=registry,
    )

    with pytest.raises(HTTPException) as exc_info:
        await subsessions_message_endpoint(request)
    assert exc_info.value.status_code == 400
    assert "'text' field is required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_message_endpoint_inactive_subsession_raises_409() -> None:
    """Raises 409 when ``enqueue_message`` returns False (subsession not active)."""
    info = _mock_subsession_info(sub_id="sub-1", status_value="closed")
    registry = _mock_registry(get_return=info, enqueue_return=False)
    request = _make_post_request(
        path="/subsessions/sub-1/message",
        path_params={"sub_id": "sub-1"},
        body={"text": "hello"},
        registry=registry,
    )

    with pytest.raises(HTTPException) as exc_info:
        await subsessions_message_endpoint(request)
    assert exc_info.value.status_code == 409
    assert "not active" in exc_info.value.detail


@pytest.mark.asyncio
async def test_message_endpoint_unknown_id_raises_404() -> None:
    """Raises 404 when the subsession id is unknown."""
    registry = _mock_registry(get_return=None)
    request = _make_post_request(
        path="/subsessions/unknown/message",
        path_params={"sub_id": "unknown"},
        body={"text": "hello"},
        registry=registry,
    )

    with pytest.raises(HTTPException) as exc_info:
        await subsessions_message_endpoint(request)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_message_endpoint_no_registry_raises_503() -> None:
    """Raises 503 when the registry is not wired."""
    request = _make_post_request(
        path="/subsessions/sub-1/message",
        path_params={"sub_id": "sub-1"},
        body={"text": "hello"},
        registry=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await subsessions_message_endpoint(request)
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# subsessions_close_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_endpoint_closes_and_delivers_summary() -> None:
    """POST /subsessions/{sub_id}/close closes and delivers summary."""
    closed_info = _mock_subsession_info(
        sub_id="sub-1",
        status_value="closed",
        summary="done",
        close_reason="closed by user",
    )
    info = _mock_subsession_info(sub_id="sub-1", status_value="running")
    registry = _mock_registry(get_return=info, cancel_and_close_return=closed_info)
    delivery = Mock()
    delivery.deliver_summary = AsyncMock()
    request = _make_post_request(
        path="/subsessions/sub-1/close",
        path_params={"sub_id": "sub-1"},
        body={},
        registry=registry,
        delivery=delivery,
    )

    response = await subsessions_close_endpoint(request)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body == {"subsession_id": "sub-1", "closed": True, "summary": "done"}
    registry.cancel_and_close.assert_called_once_with(
        "sub-1", reason="closed by user", closed_by="user"
    )
    delivery.deliver_summary.assert_awaited_once_with(
        closed_info, "done", "closed by user"
    )


@pytest.mark.asyncio
async def test_close_endpoint_already_terminal_returns_closed_false() -> None:
    """Returns ``closed: false`` when the subsession is already terminal."""
    info = _mock_subsession_info(sub_id="sub-1", status_value="closed")
    registry = _mock_registry(get_return=info, cancel_and_close_return=None)
    request = _make_post_request(
        path="/subsessions/sub-1/close",
        path_params={"sub_id": "sub-1"},
        body={},
        registry=registry,
    )

    response = await subsessions_close_endpoint(request)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body == {"subsession_id": "sub-1", "closed": False, "status": "closed"}


@pytest.mark.asyncio
async def test_close_endpoint_delivery_none_skips_summary() -> None:
    """Skips summary delivery when ``subsession_delivery`` is not wired."""
    closed_info = _mock_subsession_info(
        sub_id="sub-1",
        status_value="closed",
        summary="done",
        close_reason="closed by user",
    )
    info = _mock_subsession_info(sub_id="sub-1", status_value="running")
    registry = _mock_registry(get_return=info, cancel_and_close_return=closed_info)
    request = _make_post_request(
        path="/subsessions/sub-1/close",
        path_params={"sub_id": "sub-1"},
        body={},
        registry=registry,
        delivery=None,
    )

    response = await subsessions_close_endpoint(request)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body == {"subsession_id": "sub-1", "closed": True, "summary": "done"}


@pytest.mark.asyncio
async def test_close_endpoint_sets_default_summary_when_none() -> None:
    """Uses empty string for summary when ``closed.summary`` is None."""
    closed_info = _mock_subsession_info(
        sub_id="sub-1", status_value="closed", summary=None, close_reason=None
    )
    info = _mock_subsession_info(sub_id="sub-1", status_value="running")
    registry = _mock_registry(get_return=info, cancel_and_close_return=closed_info)
    delivery = Mock()
    delivery.deliver_summary = AsyncMock()
    request = _make_post_request(
        path="/subsessions/sub-1/close",
        path_params={"sub_id": "sub-1"},
        body={},
        registry=registry,
        delivery=delivery,
    )

    response = await subsessions_close_endpoint(request)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["summary"] is None
    # deliver_summary called with "" for summary and "closed" for reason
    delivery.deliver_summary.assert_awaited_once_with(closed_info, "", "closed")


@pytest.mark.asyncio
async def test_close_endpoint_unknown_id_raises_404() -> None:
    """Raises 404 when the subsession id is unknown."""
    registry = _mock_registry(get_return=None)
    request = _make_post_request(
        path="/subsessions/unknown/close",
        path_params={"sub_id": "unknown"},
        body={},
        registry=registry,
    )

    with pytest.raises(HTTPException) as exc_info:
        await subsessions_close_endpoint(request)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_close_endpoint_no_registry_raises_503() -> None:
    """Raises 503 when the registry is not wired."""
    request = _make_post_request(
        path="/subsessions/sub-1/close",
        path_params={"sub_id": "sub-1"},
        body={},
        registry=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await subsessions_close_endpoint(request)
    assert exc_info.value.status_code == 503

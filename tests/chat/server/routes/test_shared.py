"""Unit tests for the shared route helpers in ``_shared.py``."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from robotsix_chat.chat.server.routes._shared import (
    _get_session_id,
    _parse_json_body,
    _sse_frame,
    health_endpoint,
    ui_endpoint,
)

# ---------------------------------------------------------------------------
# _sse_frame
# ---------------------------------------------------------------------------


def test_sse_frame_dict() -> None:
    """A dict payload produces a correctly formatted SSE data frame."""
    frame = _sse_frame({"key": "value"})
    assert frame == b'data: {"key": "value"}\n\n'


def test_sse_frame_list() -> None:
    """A list payload produces a correctly formatted SSE data frame."""
    frame = _sse_frame([1, 2, 3])
    assert frame == b"data: [1, 2, 3]\n\n"


def test_sse_frame_string() -> None:
    """A string payload is JSON-serialised inside the SSE frame."""
    frame = _sse_frame("hello")
    assert frame == b'data: "hello"\n\n'


def test_sse_frame_number() -> None:
    """A numeric payload is JSON-serialised inside the SSE frame."""
    frame = _sse_frame(42)
    assert frame == b"data: 42\n\n"


def test_sse_frame_bool() -> None:
    """A boolean payload is JSON-serialised inside the SSE frame."""
    frame = _sse_frame(True)
    assert frame == b"data: true\n\n"


def test_sse_frame_none() -> None:
    """A ``None`` payload is JSON-serialised as ``null``."""
    frame = _sse_frame(None)
    assert frame == b"data: null\n\n"


def test_sse_frame_empty_dict() -> None:
    """An empty dict produces ``data: {}``."""
    frame = _sse_frame({})
    assert frame == b"data: {}\n\n"


# ---------------------------------------------------------------------------
# _parse_json_body
# ---------------------------------------------------------------------------


def _make_json_request(body: object) -> Request:
    """Build a minimal Starlette ``Request`` with a JSON body."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/test",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }

    body_bytes = json.dumps(body).encode() if body is not None else b""

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_parse_json_body_valid_dict() -> None:
    """A valid JSON object body returns the parsed dict."""
    request = _make_json_request({"key": "value"})
    result = await _parse_json_body(request)
    assert result == {"key": "value"}


@pytest.mark.asyncio
async def test_parse_json_body_empty_dict() -> None:
    """An empty JSON object ``{}`` returns an empty dict."""
    request = _make_json_request({})
    result = await _parse_json_body(request)
    assert result == {}


@pytest.mark.asyncio
async def test_parse_json_body_malformed() -> None:
    """Malformed JSON raises a 400 HTTPException."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/test",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }

    async def receive() -> dict[str, object]:
        return {
            "type": "http.request",
            "body": b"not valid json",
            "more_body": False,
        }

    request = Request(scope, receive)
    with pytest.raises(HTTPException) as exc_info:
        await _parse_json_body(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "invalid JSON body"


@pytest.mark.asyncio
async def test_parse_json_body_array() -> None:
    """A JSON array body raises a 400 HTTPException (expected object)."""
    request = _make_json_request([1, 2, 3])
    with pytest.raises(HTTPException) as exc_info:
        await _parse_json_body(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "expected a JSON object"


@pytest.mark.asyncio
async def test_parse_json_body_string() -> None:
    """A JSON string body raises a 400 HTTPException (expected object)."""
    request = _make_json_request("just a string")
    with pytest.raises(HTTPException) as exc_info:
        await _parse_json_body(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "expected a JSON object"


@pytest.mark.asyncio
async def test_parse_json_body_number() -> None:
    """A JSON number body raises a 400 HTTPException (expected object)."""
    request = _make_json_request(42)
    with pytest.raises(HTTPException) as exc_info:
        await _parse_json_body(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "expected a JSON object"


# ---------------------------------------------------------------------------
# _get_session_id
# ---------------------------------------------------------------------------


def _make_query_request(query_string: str) -> Request:
    """Build a minimal Starlette ``Request`` with the given query string."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/test",
        "query_string": query_string.encode(),
        "headers": [],
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    return Request(scope, receive)


def test_get_session_id_present() -> None:
    """Returns the ``session_id`` query param when present."""
    request = _make_query_request("session_id=abc123")
    result = _get_session_id(request)
    assert result == "abc123"


def test_get_session_id_client_id_fallback() -> None:
    """Falls back to ``client_id`` when ``session_id`` is absent."""
    request = _make_query_request("client_id=xyz789")
    result = _get_session_id(request)
    assert result == "xyz789"


def test_get_session_id_prefers_session_id() -> None:
    """When both are present, ``session_id`` takes priority."""
    request = _make_query_request("session_id=primary&client_id=fallback")
    result = _get_session_id(request)
    assert result == "primary"


def test_get_session_id_empty_session_id_falls_back() -> None:
    """An empty ``session_id`` is falsy, falling back to ``client_id``."""
    request = _make_query_request("session_id=&client_id=fallback")
    result = _get_session_id(request)
    assert result == "fallback"


def test_get_session_id_missing_both() -> None:
    """When both params are missing, raises a 400 HTTPException."""
    request = _make_query_request("")
    with pytest.raises(HTTPException) as exc_info:
        _get_session_id(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "session_id query parameter is required"


# ---------------------------------------------------------------------------
# health_endpoint
# ---------------------------------------------------------------------------


def _make_bare_request(app: object | None = None) -> Request:
    """Build a minimal Starlette ``Request`` with no query or body."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/",
        "query_string": b"",
        "headers": [],
    }
    if app is not None:
        scope["app"] = app

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    """The /health endpoint returns 200 ``{"status": "ok"}`` with no memory."""
    request = _make_bare_request()
    response = await health_endpoint(request)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ok"}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_health_endpoint_surfaces_degraded_memory() -> None:
    """When a memory backend is wired, /health embeds its status (degraded)."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    memory = Mock()
    memory.status.return_value = {
        "backend": "cognee",
        "degraded": True,
        "reason": "frozen",
    }
    app = SimpleNamespace(state=SimpleNamespace(memory=memory))
    request = _make_bare_request(app=app)

    response = await health_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["status"] == "ok"  # liveness must stay ok even when degraded
    assert body["memory"]["degraded"] is True


# ---------------------------------------------------------------------------
# ui_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_endpoint() -> None:
    """The / endpoint returns HTML from ``_load_ui_html`` with the timeout."""
    from unittest.mock import Mock

    mock_app = Mock()
    mock_app.state.idle_timeout_minutes = 5
    request = _make_bare_request(app=mock_app)

    with patch(
        "robotsix_chat.chat.server._load_ui_html",
        return_value="<html>ui</html>",
    ) as mock_load:
        response = await ui_endpoint(request)

    assert isinstance(response, HTMLResponse)
    assert response.status_code == 200
    assert response.body == b"<html>ui</html>"
    mock_load.assert_called_once_with(5)

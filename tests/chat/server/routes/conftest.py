"""Shared helpers and fixtures for routes tests.

Centralized Starlette ``Request`` builders live here so individual test
modules import them instead of redefining slightly-divergent copies. Each
builder is parameterized via optional keyword arguments to cover the
construction patterns used across the routes test suite.
"""

from __future__ import annotations

import json

from starlette.requests import Request


def _make_bare_request(
    app: object | None = None,
    *,
    method: str = "GET",
    path: str = "/",
    query_string: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    """Build a minimal Starlette ``Request`` with no body.

    All scope aspects are overridable via keyword arguments; the defaults
    reproduce the original ``GET /`` request with an empty query and no
    headers.
    """
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": path,
        "query_string": query_string,
        "headers": headers or [],
    }
    if app is not None:
        scope["app"] = app

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    return Request(scope, receive)


def _make_request(
    app: object | None = None,
    *,
    method: str = "GET",
    path: str = "/",
    query_string: str = "",
    path_params: dict[str, str] | None = None,
    session_id: str | None = None,
    client_id: str | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
    app_state: object | None = None,
) -> Request:
    """Build a minimal Starlette ``Request`` with flexible scope control.

    Pass ``app`` to attach a real application object directly, or
    ``app_state`` to wrap a state object in a lightweight fake app
    (``request.app.state``). ``session_id`` / ``client_id`` are appended to
    the query string as convenience shortcuts on top of any explicit
    ``query_string``.
    """
    params: list[str] = []
    if query_string:
        params.append(query_string)
    if session_id is not None:
        params.append(f"session_id={session_id}")
    if client_id is not None:
        params.append(f"client_id={client_id}")

    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": path,
        "query_string": "&".join(params).encode(),
        "headers": headers or [],
        "path_params": path_params or {},
    }
    if app is not None:
        scope["app"] = app
    elif app_state is not None:
        scope["app"] = type("FakeApp", (), {"state": app_state})()

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    return Request(scope, receive)


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

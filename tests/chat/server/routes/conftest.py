"""Shared helpers and fixtures for routes tests."""

from __future__ import annotations

from starlette.requests import Request


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

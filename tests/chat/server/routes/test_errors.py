"""Tests for the error handlers in ``errors.py``.

These handlers produce every API error response — if they regress, all
routes break silently.  Each test stands up a minimal Starlette app so
the handler registration contract is also exercised.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

from asgi_correlation_id import correlation_id as cid_ctx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from robotsix_chat.chat.server.routes.errors import (
    http_exception_handler,
    not_found_handler,
    server_error_handler,
    unhandled_exception_handler,
)


def _build_client(
    *,
    with_unhandled_route: bool = False,
    with_http_400_route: bool = False,
) -> TestClient:
    """Build a minimal Starlette app with error handlers and return a TestClient.

    ``server_error_handler`` cannot be exercised through the middleware
    stack (``ServerErrorMiddleware`` only uses the *last* of ``500``/
    ``Exception`` keys — ``Exception`` wins).  It is tested via direct
    async call instead.
    """
    routes: list[Route] = []
    if with_unhandled_route:

        async def _custom_raise(_request: Request) -> JSONResponse:
            raise ValueError("custom")

        routes.append(Route("/custom-raise", _custom_raise, methods=["GET"]))
    if with_http_400_route:

        async def _bad_request(_request: Request) -> JSONResponse:
            raise HTTPException(status_code=400, detail="bad input via route")

        routes.append(Route("/bad-request", _bad_request, methods=["GET"]))

    app = Starlette(
        routes=routes,
        exception_handlers={
            HTTPException: http_exception_handler,
            404: not_found_handler,
            500: server_error_handler,
            Exception: unhandled_exception_handler,
        },
    )
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# http_exception_handler
# ---------------------------------------------------------------------------


def test_http_exception_400() -> None:
    """``HTTPException(400)`` returns JSON error body with 400 status."""
    # We test the handler contract via direct call for the basic case
    # and also exercise the full Starlette stack via
    # test_http_exception_handler_via_route below.  The 404 contract is
    # exercised through the full app in test_not_found_unmatched_route.

    request = Mock(spec=Request)

    async def _call() -> JSONResponse:
        cid_ctx.set("test-cid-400")
        exc = HTTPException(status_code=400, detail="bad input")
        resp = await http_exception_handler(request, exc)
        cid_ctx.set("")
        return resp

    resp = asyncio.run(_call())
    assert resp.status_code == 400
    data = json.loads(resp.body)  # type: ignore[arg-type]
    assert data["error"] == "bad input"
    assert data["correlation_id"] == "test-cid-400"


def test_http_exception_handler_non_http_exception() -> None:
    """Non-``HTTPException`` passed to ``http_exception_handler`` returns 500."""
    request = Mock(spec=Request)

    async def _call() -> JSONResponse:
        cid_ctx.set("test-cid-500")
        exc = ValueError("non-http")
        resp = await http_exception_handler(request, exc)
        cid_ctx.set("")
        return resp

    resp = asyncio.run(_call())
    assert resp.status_code == 500
    data = json.loads(resp.body)  # type: ignore[arg-type]
    assert data["error"] == "non-http"
    assert data["correlation_id"] == "test-cid-500"


def test_http_exception_handler_via_route() -> None:
    """``HTTPException(400)`` raised inside a route hits ``http_exception_handler``."""
    client = _build_client(with_http_400_route=True)
    resp = client.get("/bad-request")
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "bad input via route"
    assert "correlation_id" in data


# ---------------------------------------------------------------------------
# not_found_handler
# ---------------------------------------------------------------------------


def test_not_found_unmatched_route() -> None:
    """Unmatched route returns generic ``{"error": "not found"}`` with 404."""
    client = _build_client()
    resp = client.get("/nonexistent")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "not found"
    assert "correlation_id" in data


def test_not_found_handler_custom_detail() -> None:
    """Explicit ``HTTPException(404, detail="custom")`` forwards the detail."""
    request = Mock(spec=Request)

    async def _call() -> JSONResponse:
        cid_ctx.set("test-cid-custom")
        exc = HTTPException(status_code=404, detail="unknown subsession 'xyz'")
        resp = await not_found_handler(request, exc)
        cid_ctx.set("")
        return resp

    resp = asyncio.run(_call())
    assert resp.status_code == 404
    data = json.loads(resp.body)  # type: ignore[arg-type]
    assert data["error"] == "unknown subsession 'xyz'"
    assert data["correlation_id"] == "test-cid-custom"


def test_not_found_handler_default_not_found_detail() -> None:
    """Default 'Not Found' detail returns generic 'not found' message."""
    request = Mock(spec=Request)

    async def _call() -> JSONResponse:
        cid_ctx.set("test-cid-default")
        exc = HTTPException(status_code=404, detail="Not Found")
        resp = await not_found_handler(request, exc)
        cid_ctx.set("")
        return resp

    resp = asyncio.run(_call())
    assert resp.status_code == 404
    data = json.loads(resp.body)  # type: ignore[arg-type]
    assert data["error"] == "not found"
    assert data["correlation_id"] == "test-cid-default"


# ---------------------------------------------------------------------------
# server_error_handler
# ---------------------------------------------------------------------------


def test_server_error_handler_500() -> None:
    """``server_error_handler`` returns ``{"error": "internal server error"}`` with 500.

    This handler cannot be tested through the middleware stack because
    ``Starlette.build_middleware_stack`` strips both ``500`` and
    ``Exception`` keys, passing only the *last* one to
    ``ServerErrorMiddleware`` — and ``Exception`` wins.  The handler
    is exercised via direct async call.
    """
    request = Mock(spec=Request)

    async def _call() -> JSONResponse:
        cid_ctx.set("test-cid-server-err")
        exc = Exception("something broke")
        resp = await server_error_handler(request, exc)
        cid_ctx.set("")
        return resp

    resp = asyncio.run(_call())
    assert resp.status_code == 500
    data = json.loads(resp.body)  # type: ignore[arg-type]
    assert data["error"] == "internal server error"
    assert data["correlation_id"] == "test-cid-server-err"


# ---------------------------------------------------------------------------
# _error_body empty-detail guard
# ---------------------------------------------------------------------------


def test_error_body_empty_detail() -> None:
    """``_error_body`` falls back to 'internal server error' on empty detail."""
    from robotsix_chat.chat.server.routes.errors import _error_body

    result = _error_body("")
    assert result["error"] == "internal server error"
    assert "correlation_id" in result


def test_error_body_non_empty_detail() -> None:
    """``_error_body`` passes through a non-empty detail unchanged."""
    from robotsix_chat.chat.server.routes.errors import _error_body

    result = _error_body("something specific")
    assert result["error"] == "something specific"


# ---------------------------------------------------------------------------
# unhandled_exception_handler
# ---------------------------------------------------------------------------


def test_unhandled_exception_generic_500() -> None:
    """Non-``HTTPException`` raises return generic 500 with correlation id."""
    client = _build_client(with_unhandled_route=True)
    resp = client.get("/custom-raise")
    assert resp.status_code == 500
    data = resp.json()
    assert data["error"] == "internal server error"
    assert "correlation_id" in data

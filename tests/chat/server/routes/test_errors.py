"""Tests for the error handlers in ``errors.py``.

These handlers produce every API error response — if they regress, all
routes break silently.  Each test stands up a minimal Starlette app so
the handler registration contract is also exercised.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
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


# ---------------------------------------------------------------------------
# Mid-stream SSE error curation
# ---------------------------------------------------------------------------


def test_stream_error_code_defaults_to_server_error() -> None:
    """An unrecognised exception maps to ``server_error``, not a guess."""
    from robotsix_chat.chat.server.routes.errors import (
        STREAM_ERROR_SERVER,
        stream_error_code,
    )

    assert stream_error_code(RuntimeError("anything")) == STREAM_ERROR_SERVER


def test_stream_error_code_maps_timeouts() -> None:
    """Both stdlib and httpx timeouts map to the ``timeout`` code."""
    import httpx

    from robotsix_chat.chat.server.routes.errors import (
        STREAM_ERROR_TIMEOUT,
        stream_error_code,
    )

    assert stream_error_code(TimeoutError()) == STREAM_ERROR_TIMEOUT
    assert stream_error_code(httpx.ReadTimeout("slow")) == STREAM_ERROR_TIMEOUT


def test_stream_error_code_maps_http_status() -> None:
    """HTTP status on an attached response selects the category."""
    from robotsix_chat.chat.server.routes.errors import (
        STREAM_ERROR_AUTH,
        STREAM_ERROR_INVALID_REQUEST,
        STREAM_ERROR_RATE_LIMIT,
        STREAM_ERROR_SERVER,
        stream_error_code,
    )

    def _exc(status: int) -> Exception:
        exc = Exception("upstream said no")
        exc.response = Mock(status_code=status)  # type: ignore[attr-defined]
        return exc

    assert stream_error_code(_exc(429)) == STREAM_ERROR_RATE_LIMIT
    assert stream_error_code(_exc(401)) == STREAM_ERROR_AUTH
    assert stream_error_code(_exc(403)) == STREAM_ERROR_AUTH
    assert stream_error_code(_exc(422)) == STREAM_ERROR_INVALID_REQUEST
    # 5xx is the upstream's problem, not a client-actionable category.
    assert stream_error_code(_exc(503)) == STREAM_ERROR_SERVER


def test_stream_error_code_maps_usage_exhausted_to_budget_exhausted() -> None:
    """Claude SDK usage exhaustion gets a distinct machine-readable code."""
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    from robotsix_chat.chat.server.routes.errors import (
        STREAM_ERROR_BUDGET_EXHAUSTED,
        stream_error_code,
    )

    assert (
        stream_error_code(ClaudeSDKUsageExhaustedError("out of usage credits"))
        == STREAM_ERROR_BUDGET_EXHAUSTED
    )


def test_curated_stream_error_budget_exhausted_names_reset_time() -> None:
    """Name the UTC reset time and wait, not the generic internal error.

    Also hints at paid fallback when it is disabled.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    from robotsix_chat.chat.server.routes.errors import (
        STREAM_ERROR_BUDGET_EXHAUSTED,
        curated_stream_error,
    )

    payload = curated_stream_error(
        ClaudeSDKUsageExhaustedError("You've hit your limit · resets 1am (UTC)"),
        fallback_id="turn-1",
    )
    assert payload["code"] == STREAM_ERROR_BUDGET_EXHAUSTED
    # Names the parsed reset clock time in UTC (deterministic regardless of now).
    assert "01:00 UTC" in payload["message"]
    assert "quota exhausted" in payload["message"].lower()
    # Paid fallback disabled by default → hint to enable it.
    assert "paid fallback" in payload["message"]
    # Never the generic internal-error surface for this class.
    assert "internal error" not in payload["message"].lower()
    # The curated message never echoes the raw upstream exception text.
    assert "hit your limit" not in payload["message"]


def test_curated_stream_error_budget_exhausted_paid_fallback_enabled() -> None:
    """With paid fallback enabled the message drops the enable-it hint."""
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    from robotsix_chat.chat.server.routes.errors import curated_stream_error

    payload = curated_stream_error(
        ClaudeSDKUsageExhaustedError("You've hit your limit · resets 1am (UTC)"),
        fallback_id="turn-1",
        paid_fallback_enabled=True,
    )
    assert "01:00 UTC" in payload["message"]
    assert "paid fallback" not in payload["message"]


def test_claude_usage_reset_at_parses_reset_hint() -> None:
    """A ``resets <time> (UTC)`` hint resolves to the next UTC occurrence."""
    from datetime import datetime

    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    from robotsix_chat.chat.server.routes.errors import claude_usage_reset_at

    now = datetime(2026, 8, 30, 0, 4, tzinfo=UTC)
    reset = claude_usage_reset_at(
        ClaudeSDKUsageExhaustedError("You've hit your limit · resets 1am (UTC)"),
        now=now,
    )
    assert reset == datetime(2026, 8, 30, 1, 0, tzinfo=UTC)


def test_claude_usage_reset_at_rolls_to_next_day_when_passed() -> None:
    """A reset clock time already behind ``now`` resolves to tomorrow."""
    from datetime import datetime

    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    from robotsix_chat.chat.server.routes.errors import claude_usage_reset_at

    now = datetime(2026, 8, 30, 2, 0, tzinfo=UTC)
    reset = claude_usage_reset_at(
        ClaudeSDKUsageExhaustedError("resets 11:10am (UTC)"),
        now=now,
    )
    assert reset == datetime(2026, 8, 30, 11, 10, tzinfo=UTC)

    now_late = datetime(2026, 8, 30, 23, 0, tzinfo=UTC)
    reset_late = claude_usage_reset_at(
        ClaudeSDKUsageExhaustedError("resets 8pm (UTC)"),
        now=now_late,
    )
    assert reset_late == datetime(2026, 8, 31, 20, 0, tzinfo=UTC)


def test_claude_usage_reset_at_returns_none_without_hint() -> None:
    """No parseable reset hint (bare 'out of usage credits') yields ``None``."""
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    from robotsix_chat.chat.server.routes.errors import claude_usage_reset_at

    assert (
        claude_usage_reset_at(
            ClaudeSDKUsageExhaustedError("You're out of usage credits")
        )
        is None
    )


def test_budget_exhausted_message_names_wait_duration() -> None:
    """The message states the approximate wait ('in 56 min') from the reset."""
    from datetime import datetime

    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    from robotsix_chat.chat.server.routes.errors import budget_exhausted_message

    now = datetime(2026, 8, 30, 0, 4, tzinfo=UTC)
    message = budget_exhausted_message(
        ClaudeSDKUsageExhaustedError("You've hit your limit · resets 1am (UTC)"),
        now=now,
    )
    assert "resets at 01:00 UTC" in message
    assert "in 56 min" in message
    assert "enable paid fallback" in message


def test_budget_exhausted_message_without_reset_hint() -> None:
    """With no reset hint the message degrades gracefully but stays actionable."""
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    from robotsix_chat.chat.server.routes.errors import budget_exhausted_message

    message = budget_exhausted_message(
        ClaudeSDKUsageExhaustedError("You're out of usage credits")
    )
    assert "Claude quota exhausted" in message
    assert "enable paid fallback" in message
    assert "UTC" not in message


def test_curated_stream_error_hides_exception_text() -> None:
    """The payload never echoes ``str(exc)`` or the exception class name."""
    from robotsix_chat.chat.server.routes.errors import curated_stream_error

    secret = "/srv/app/.env password=hunter2 https://upstream.internal/v1"
    payload = curated_stream_error(RuntimeError(secret), fallback_id="turn-9")

    assert secret not in payload["message"]
    assert "hunter2" not in payload["message"]
    assert "RuntimeError" not in payload["message"]
    assert payload["message"]


def test_curated_stream_error_uses_correlation_id_when_set() -> None:
    """The correlation id in context wins over the fallback."""
    from robotsix_chat.chat.server.routes.errors import curated_stream_error

    token = cid_ctx.set("cid-abc")
    try:
        payload = curated_stream_error(RuntimeError("x"), fallback_id="turn-9")
    finally:
        cid_ctx.reset(token)

    assert payload["correlation_id"] == "cid-abc"


def test_curated_stream_error_falls_back_to_turn_id() -> None:
    """With no correlation id in context the turn id is used instead."""
    from robotsix_chat.chat.server.routes.errors import curated_stream_error

    token = cid_ctx.set(None)
    try:
        payload = curated_stream_error(RuntimeError("x"), fallback_id="turn-9")
    finally:
        cid_ctx.reset(token)

    assert payload["correlation_id"] == "turn-9"


# ---------------------------------------------------------------------------
# Chained exhaustion — a failed fallback walk must still classify as
# budget_exhausted (regression: mimo 404 after Claude exhaustion surfaced as
# the generic "internal error").
# ---------------------------------------------------------------------------


def _chained(inner: BaseException, outer: BaseException) -> BaseException:
    try:
        raise inner
    except BaseException:
        try:
            raise outer
        except BaseException as caught:
            return caught


def test_stream_error_code_sees_exhaustion_through_context_chain() -> None:
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    from robotsix_chat.chat.server.routes.errors import (
        STREAM_ERROR_BUDGET_EXHAUSTED,
        stream_error_code,
    )

    root = ClaudeSDKUsageExhaustedError("You've hit your limit · resets 10pm (UTC)")
    exc = _chained(root, RuntimeError("fallback tier failed too"))
    assert stream_error_code(exc) == STREAM_ERROR_BUDGET_EXHAUSTED


def test_curated_stream_error_names_reset_time_through_chain() -> None:
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    from robotsix_chat.chat.server.routes.errors import curated_stream_error

    root = ClaudeSDKUsageExhaustedError("You've hit your limit · resets 10pm (UTC)")
    exc = _chained(root, RuntimeError("boom"))
    msg = curated_stream_error(exc)["message"]
    assert "22:00 UTC" in msg

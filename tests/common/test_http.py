"""Tests for the shared ``robotsix_chat.common.http`` helper.

:func:`safe_http_request` and :class:`HttpResult`, with ``respx`` mocked
so there are no real network calls.
"""

from __future__ import annotations

import json as _json

import httpx
import pytest
import respx

from robotsix_chat.common.http import HttpResult, safe_http_request

# ---------------------------------------------------------------------------
# HttpResult
# ---------------------------------------------------------------------------


def test_http_result_success() -> None:
    """HttpResult with text and no error is ok."""
    r = HttpResult(text="response body", status_code=200)
    assert r.ok is True
    assert r.text == "response body"
    assert r.error is None


def test_http_result_error() -> None:
    """HttpResult with error and no text is not ok."""
    r = HttpResult(error="something went wrong", status_code=500)
    assert r.ok is False
    assert r.text is None
    assert r.error == "something went wrong"


# ---------------------------------------------------------------------------
# safe_http_request — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_success(respx_mock: respx.MockRouter) -> None:
    """A successful GET returns the response text."""
    route = respx_mock.get("https://example.com/api").mock(
        return_value=httpx.Response(200, text="hello world")
    )

    result = await safe_http_request("GET", "https://example.com/api")

    assert result.ok is True
    assert result.text == "hello world"
    assert result.status_code == 200
    assert route.called


@pytest.mark.asyncio
async def test_post_success(respx_mock: respx.MockRouter) -> None:
    """A successful POST returns the response text."""
    route = respx_mock.post("https://example.com/api").mock(
        return_value=httpx.Response(201, text='{"id": 1}')
    )

    result = await safe_http_request(
        "POST",
        "https://example.com/api",
        json_body={"key": "value"},
    )

    assert result.ok is True
    assert result.text == '{"id": 1}'
    assert route.called
    assert _json.loads(route.calls.last.request.content) == {"key": "value"}


@pytest.mark.asyncio
async def test_forwards_headers(respx_mock: respx.MockRouter) -> None:
    """Passed headers are forwarded to the request."""
    route = respx_mock.get("https://example.com/api").mock(
        return_value=httpx.Response(200, text="ok")
    )

    await safe_http_request(
        "GET",
        "https://example.com/api",
        headers={"Authorization": "Bearer xyz"},
    )

    assert route.calls.last.request.headers["authorization"] == "Bearer xyz"


@pytest.mark.asyncio
async def test_forwards_params(respx_mock: respx.MockRouter) -> None:
    """Passed query params are forwarded to GET."""
    route = respx_mock.get("https://example.com/api").mock(
        return_value=httpx.Response(200, text="ok")
    )

    await safe_http_request(
        "GET",
        "https://example.com/api",
        params={"page": "1"},
    )

    assert dict(route.calls.last.request.url.params) == {"page": "1"}


@pytest.mark.asyncio
async def test_does_not_pass_params_when_none(
    respx_mock: respx.MockRouter,
) -> None:
    """When params is None, no query string is added."""
    route = respx_mock.get("https://example.com/api").mock(
        return_value=httpx.Response(200, text="ok")
    )

    await safe_http_request("GET", "https://example.com/api")

    assert route.called
    assert dict(route.calls.last.request.url.params) == {}


@pytest.mark.asyncio
async def test_passes_timeout_to_client(
    respx_mock: respx.MockRouter,
) -> None:
    """Request completes without error, confirming timeout is handled internally."""
    route = respx_mock.get("https://example.com/api").mock(
        return_value=httpx.Response(200, text="ok")
    )

    result = await safe_http_request("GET", "https://example.com/api", timeout=12.5)

    assert result.ok is True
    assert route.called


# ---------------------------------------------------------------------------
# safe_http_request — HTTPStatusError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_returns_diagnostic(
    respx_mock: respx.MockRouter,
) -> None:
    """An HTTP error (>=400) is returned as an error string, never raised."""
    respx_mock.get("https://example.com/api").mock(
        return_value=httpx.Response(404, text="not found")
    )

    result = await safe_http_request("GET", "https://example.com/api")

    assert result.ok is False
    assert result.error is not None
    assert "404" in result.error
    assert "not found" in result.error


@pytest.mark.asyncio
async def test_http_error_includes_label(
    respx_mock: respx.MockRouter,
) -> None:
    """The label parameter appears in the error message."""
    respx_mock.get("https://example.com/api").mock(
        return_value=httpx.Response(500, text="boom")
    )

    result = await safe_http_request("GET", "https://example.com/api", label="TestSvc")

    assert result.error is not None
    assert "TestSvc error 500" in result.error


@pytest.mark.asyncio
async def test_http_error_truncates_body(
    respx_mock: respx.MockRouter,
) -> None:
    """Response bodies longer than 500 chars are truncated in the error."""
    long_body = "x" * 1000
    respx_mock.get("https://example.com/api").mock(
        return_value=httpx.Response(500, text=long_body)
    )

    result = await safe_http_request("GET", "https://example.com/api")

    assert result.error is not None
    assert len(result.error) < len(long_body) + 200
    assert "x" * 500 in result.error
    assert "x" * 501 not in result.error


@pytest.mark.asyncio
async def test_http_error_empty_body(
    respx_mock: respx.MockRouter,
) -> None:
    """An empty response body yields '(empty body)' in the error."""
    respx_mock.get("https://example.com/api").mock(
        return_value=httpx.Response(500, text="")
    )

    result = await safe_http_request("GET", "https://example.com/api")

    assert result.error is not None
    assert "(empty body)" in result.error


# ---------------------------------------------------------------------------
# safe_http_request — TimeoutException
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_diagnostic(
    respx_mock: respx.MockRouter,
) -> None:
    """A timeout is returned as an error string, never raised."""
    respx_mock.get("https://example.com/api").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    result = await safe_http_request("GET", "https://example.com/api", timeout=5.0)

    assert result.ok is False
    assert result.error is not None
    assert "timed out" in result.error
    assert "5.0s" in result.error


# ---------------------------------------------------------------------------
# safe_http_request — unexpected Exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_error_returns_diagnostic(
    respx_mock: respx.MockRouter,
) -> None:
    """An unexpected exception is returned as an error string, never raised."""
    respx_mock.get("https://example.com/api").mock(
        side_effect=RuntimeError("something crashed")
    )

    result = await safe_http_request("GET", "https://example.com/api")

    assert result.ok is False
    assert result.error is not None
    assert "something crashed" in result.error

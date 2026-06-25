"""Tests for the shared ``robotsix_chat.common.http`` helper.

:func:`safe_http_request` and :class:`HttpResult`, with ``httpx`` mocked
so there are no real network calls.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from robotsix_chat.common.http import HttpResult, safe_http_request
from tests.common.mock_helpers import MockResponse as _MockResponse
from tests.common.mock_helpers import install_mock_client as _install_mock_client

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
async def test_get_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful GET returns the response text."""
    resp = _MockResponse(text="hello world", status_code=200)
    captured = _install_mock_client(monkeypatch, resp)

    result = await safe_http_request("GET", "https://example.com/api")

    assert result.ok is True
    assert result.text == "hello world"
    assert result.status_code == 200
    assert captured["method"] == "GET"
    assert captured["url"] == "https://example.com/api"


@pytest.mark.asyncio
async def test_post_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful POST returns the response text."""
    resp = _MockResponse(text='{"id": 1}', status_code=201)
    captured = _install_mock_client(monkeypatch, resp)

    result = await safe_http_request(
        "POST",
        "https://example.com/api",
        json_body={"key": "value"},
    )

    assert result.ok is True
    assert result.text == '{"id": 1}'
    assert captured["method"] == "POST"
    assert captured["json"] == {"key": "value"}


@pytest.mark.asyncio
async def test_forwards_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passed headers are forwarded to the request."""
    resp = _MockResponse(text="ok")
    captured = _install_mock_client(monkeypatch, resp)

    await safe_http_request(
        "GET",
        "https://example.com/api",
        headers={"Authorization": "Bearer xyz"},
    )

    assert captured["headers"] == {"Authorization": "Bearer xyz"}


@pytest.mark.asyncio
async def test_forwards_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passed query params are forwarded to GET."""
    resp = _MockResponse(text="ok")
    captured = _install_mock_client(monkeypatch, resp)

    await safe_http_request(
        "GET",
        "https://example.com/api",
        params={"page": "1"},
    )

    assert captured["params"] == {"page": "1"}


@pytest.mark.asyncio
async def test_does_not_pass_params_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When params is None, no params kwarg is sent (compat with simpler mocks)."""
    resp = _MockResponse(text="ok")
    captured = _install_mock_client(monkeypatch, resp)

    await safe_http_request("GET", "https://example.com/api")

    assert captured.get("params") is None


@pytest.mark.asyncio
async def test_passes_timeout_to_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timeout parameter is forwarded to httpx.AsyncClient."""
    resp = _MockResponse(text="ok")
    captured = _install_mock_client(monkeypatch, resp, capture_kwargs=True)

    await safe_http_request("GET", "https://example.com/api", timeout=12.5)

    assert captured["client_kwargs"]["timeout"] == 12.5


# ---------------------------------------------------------------------------
# safe_http_request — HTTPStatusError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_returns_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP error (>=400) is returned as an error string, never raised."""
    resp = _MockResponse(text="not found", status_code=404)
    _install_mock_client(monkeypatch, resp)

    result = await safe_http_request("GET", "https://example.com/api")

    assert result.ok is False
    assert result.error is not None
    assert "404" in result.error
    assert "not found" in result.error


@pytest.mark.asyncio
async def test_http_error_includes_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label parameter appears in the error message."""
    resp = _MockResponse(text="boom", status_code=500)
    _install_mock_client(monkeypatch, resp)

    result = await safe_http_request("GET", "https://example.com/api", label="TestSvc")

    assert "TestSvc error 500" in result.error


@pytest.mark.asyncio
async def test_http_error_truncates_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Response bodies longer than 500 chars are truncated in the error."""
    long_body = "x" * 1000
    resp = _MockResponse(text=long_body, status_code=500)
    _install_mock_client(monkeypatch, resp)

    result = await safe_http_request("GET", "https://example.com/api")

    assert len(result.error or "") < len(long_body) + 200
    assert "x" * 500 in result.error
    assert "x" * 501 not in result.error


@pytest.mark.asyncio
async def test_http_error_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty response body yields '(empty body)' in the error."""
    resp = _MockResponse(text="", status_code=500)
    _install_mock_client(monkeypatch, resp)

    result = await safe_http_request("GET", "https://example.com/api")

    assert "(empty body)" in result.error


# ---------------------------------------------------------------------------
# safe_http_request — TimeoutException
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout is returned as an error string, never raised."""

    class _TimeoutClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _TimeoutClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> None:
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)

    result = await safe_http_request("GET", "https://example.com/api", timeout=5.0)

    assert result.ok is False
    assert "timed out" in result.error
    assert "5.0s" in result.error


# ---------------------------------------------------------------------------
# safe_http_request — unexpected Exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_error_returns_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception is returned as an error string, never raised."""

    class _BrokenClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _BrokenClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> None:
            raise RuntimeError("something crashed")

    monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)

    result = await safe_http_request("GET", "https://example.com/api")

    assert result.ok is False
    assert "something crashed" in result.error

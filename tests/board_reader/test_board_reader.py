"""Tests for the board-reader integration.

:func:`build_board_reader_tools` and :class:`BoardReader`, with ``httpx``
mocked so there are no real network calls.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from robotsix_chat.board_reader import build_board_reader_tools
from robotsix_chat.board_reader.client import BoardReader
from robotsix_chat.config import BoardReaderSettings


def _settings(**kw: Any) -> BoardReaderSettings:
    base: dict[str, Any] = {"enabled": True}
    base.update(kw)
    return BoardReaderSettings(**base)


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------


class _MockResponse:
    """Minimal httpx.Response stand-in for testing."""

    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=object(),  # type: ignore[arg-type]
                response=self,  # type: ignore[arg-type]
            )


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    response: _MockResponse,
) -> dict[str, Any]:
    """Replace ``httpx.AsyncClient`` with a factory returning *response*.

    Returns a ``captured`` dict that receives ``url``, ``headers``, and
    ``params`` from each ``get`` call for later inspection.
    """
    captured: dict[str, Any] = {}

    class _BoundClient:
        def __init__(self, **kwargs: Any) -> None:
            self._resp = response

        async def __aenter__(self) -> _BoundClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str] | None = None,
        ) -> _MockResponse:
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return self._resp

    monkeypatch.setattr(httpx, "AsyncClient", _BoundClient)
    return captured


# ---------------------------------------------------------------------------
# build_board_reader_tools
# ---------------------------------------------------------------------------


def test_build_board_reader_tools_disabled() -> None:
    """Verify that disabled board reader returns no tools."""
    assert build_board_reader_tools(BoardReaderSettings(enabled=False)) == []


def test_build_board_reader_tools_returns_two_tools() -> None:
    """Verify that enabled board reader returns list and read tools."""
    tools = build_board_reader_tools(_settings())
    assert len(tools) == 2
    names = [t.__name__ for t in tools]
    assert "list_board_tickets" in names
    assert "read_board_ticket" in names


# ---------------------------------------------------------------------------
# BoardReader.list_tickets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tickets_calls_get_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that list_tickets calls GET /tickets with correct params."""
    resp = _MockResponse(text='[{"id": "abc", "title": "Fix bug"}]')
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings(api_base_url="http://127.0.0.1:8077"))
    out = await client.list_tickets(repo_id="robotsix-chat")

    assert out == resp.text
    assert captured["url"] == "http://127.0.0.1:8077/tickets"
    assert captured["params"] == {"repo_id": "robotsix-chat"}


@pytest.mark.asyncio
async def test_list_tickets_includes_closed_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that include_closed and state are forwarded as query params."""
    resp = _MockResponse(text="[]")
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings())
    await client.list_tickets(
        repo_id="robotsix-mill",
        include_closed=True,
        state="ready",
    )

    assert captured["params"] == {
        "repo_id": "robotsix-mill",
        "include_closed": "true",
        "state": "ready",
    }


# ---------------------------------------------------------------------------
# BoardReader.get_ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticket_calls_get_tickets_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that get_ticket calls GET /tickets/{id}."""
    resp = _MockResponse(text='{"id": "abc", "title": "Fix bug", "state": "ready"}')
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings(api_base_url="http://localhost:8077"))
    out = await client.get_ticket("abc")

    assert out == resp.text
    assert captured["url"] == "http://localhost:8077/tickets/abc"


# ---------------------------------------------------------------------------
# BoardReader auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_token_sent_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that the Authorization header is set when api_token is given."""
    resp = _MockResponse(text="[]")
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings(api_token="secret-token"))
    await client.list_tickets(repo_id="robotsix-chat")

    assert captured["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer secret-token",
    }


@pytest.mark.asyncio
async def test_no_auth_header_when_token_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that no Authorization header is sent when api_token is empty."""
    resp = _MockResponse(text="[]")
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings(api_token=""))
    await client.list_tickets(repo_id="robotsix-chat")

    assert "Authorization" not in captured["headers"]


# ---------------------------------------------------------------------------
# BoardReader error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_returns_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that HTTP errors become a text message, never raised."""
    resp = _MockResponse(text="not found", status_code=404)
    _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings())
    out = await client.get_ticket("nonexistent")

    assert "404" in out
    assert "not found" in out


@pytest.mark.asyncio
async def test_timeout_returns_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that timeouts become a text message, never raised."""

    class _TimeoutClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _TimeoutClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            params: dict[str, str] | None = None,
        ) -> None:
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)

    client = BoardReader(_settings(timeout=5.0))
    out = await client.list_tickets(repo_id="x")

    assert "timed out" in out
    assert "5.0s" in out


@pytest.mark.asyncio
async def test_unexpected_error_returns_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that unexpected errors become a text message, never raised."""

    class _BrokenClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _BrokenClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            params: dict[str, str] | None = None,
        ) -> None:
            raise RuntimeError("something crashed")

    monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)

    client = BoardReader(_settings())
    out = await client.get_ticket("abc")

    assert "failed" in out.lower()
    assert "something crashed" in out

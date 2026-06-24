"""Tests for the board-reader integration.

:func:`build_board_reader_tools` and :class:`BoardReader`, with ``httpx``
mocked so there are no real network calls.
"""

from __future__ import annotations

import time
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
    counter: list[int] | None = None,
) -> dict[str, Any]:
    """Replace ``httpx.AsyncClient`` with a factory returning *response*.

    Returns a ``captured`` dict that receives ``url``, ``headers``, and
    ``params`` from each ``get`` call for later inspection.

    If *counter* is a list, its first element is incremented on every
    ``get`` call.
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
            if counter is not None:
                counter[0] += 1
            return self._resp

    monkeypatch.setattr(httpx, "AsyncClient", _BoundClient)
    return captured


# ---------------------------------------------------------------------------
# build_board_reader_tools
# ---------------------------------------------------------------------------


def test_build_board_reader_tools_disabled() -> None:
    """Verify that disabled board reader returns no tools."""
    assert build_board_reader_tools(BoardReaderSettings(enabled=False)) == []


def test_build_board_reader_tools_returns_three_tools() -> None:
    """Verify that enabled board reader returns list, read, and create tools."""
    tools = build_board_reader_tools(_settings())
    assert len(tools) == 3
    names = [t.__name__ for t in tools]
    assert "list_board_tickets" in names
    assert "read_board_ticket" in names
    assert "create_board_ticket" in names


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


# ---------------------------------------------------------------------------
# BoardReader.create_ticket
# ---------------------------------------------------------------------------


def _install_mock_post_client(
    monkeypatch: pytest.MonkeyPatch,
    response: _MockResponse,
    counter: list[int] | None = None,
) -> dict[str, Any]:
    """Replace ``httpx.AsyncClient`` with a factory for POST requests.

    Returns a ``captured`` dict that receives ``url``, ``headers``, and
    ``json`` from each ``post`` call for later inspection.

    If *counter* is a list, its first element is incremented on every
    ``post`` call.
    """
    captured: dict[str, Any] = {}

    class _BoundPostClient:
        def __init__(self, **kwargs: Any) -> None:
            self._resp = response

        async def __aenter__(self) -> _BoundPostClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, str] | None = None,
        ) -> _MockResponse:
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            if counter is not None:
                counter[0] += 1
            return self._resp

    monkeypatch.setattr(httpx, "AsyncClient", _BoundPostClient)
    return captured


@pytest.mark.asyncio
async def test_create_ticket_calls_post_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that create_ticket calls POST /tickets with correct payload."""
    resp = _MockResponse(
        text='{"id": "abc", "title": "New ticket", "state": "draft"}',
        status_code=201,
    )
    captured = _install_mock_post_client(monkeypatch, resp)

    client = BoardReader(_settings(api_base_url="http://127.0.0.1:8077"))
    out = await client.create_ticket(
        title="New ticket",
        description="A test ticket",
        repo_id="robotsix-chat",
    )

    assert out == resp.text
    assert captured["url"] == "http://127.0.0.1:8077/tickets"
    assert captured["json"] == {
        "title": "New ticket",
        "description": "A test ticket",
        "repo_id": "robotsix-chat",
    }


@pytest.mark.asyncio
async def test_create_ticket_with_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that kind is included in payload when provided."""
    resp = _MockResponse(text='{"id": "xyz", "kind": "bug"}', status_code=201)
    captured = _install_mock_post_client(monkeypatch, resp)

    client = BoardReader(_settings())
    await client.create_ticket(
        title="Bug report",
        description="Something broke",
        repo_id="robotsix-mill",
        kind="bug",
    )

    assert captured["json"] == {
        "title": "Bug report",
        "description": "Something broke",
        "repo_id": "robotsix-mill",
        "kind": "bug",
    }


@pytest.mark.asyncio
async def test_create_ticket_omits_kind_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that kind is omitted from payload when empty string."""
    resp = _MockResponse(text='{"id": "abc"}', status_code=201)
    captured = _install_mock_post_client(monkeypatch, resp)

    client = BoardReader(_settings())
    await client.create_ticket(
        title="T",
        description="D",
        repo_id="r",
        kind="",
    )

    assert "kind" not in captured["json"]


@pytest.mark.asyncio
async def test_create_ticket_http_error_returns_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that create_ticket HTTP errors become text, never raised."""
    resp = _MockResponse(text="conflict", status_code=409)
    _install_mock_post_client(monkeypatch, resp)

    client = BoardReader(_settings())
    out = await client.create_ticket(
        title="Dup",
        description="Duplicate",
        repo_id="x",
    )

    assert "409" in out
    assert "conflict" in out


# ---------------------------------------------------------------------------
# BoardReader cache tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tickets_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second call to list_tickets with same params must hit cache (0 extra HTTP)."""
    get_counter: list[int] = [0]
    resp = _MockResponse(text='[{"id": "abc"}]')
    _install_mock_client(monkeypatch, resp, counter=get_counter)

    client = BoardReader(_settings(cache_ttl=60.0))
    out1 = await client.list_tickets(repo_id="robotsix-chat")
    out2 = await client.list_tickets(repo_id="robotsix-chat")

    assert out1 == out2 == '[{"id": "abc"}]'
    assert get_counter[0] == 1  # only the first call hit HTTP


@pytest.mark.asyncio
async def test_list_tickets_cache_miss_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After cache_ttl seconds, a fresh HTTP call must be made."""
    get_counter: list[int] = [0]
    resp = _MockResponse(text="[]")
    _install_mock_client(monkeypatch, resp, counter=get_counter)

    # Control monotonic time so we can advance past cache_ttl
    fake_time = [0.0]

    class _FakeMonotonic:
        def __call__(self) -> float:
            return fake_time[0]

    monkeypatch.setattr(time, "monotonic", _FakeMonotonic())

    client = BoardReader(_settings(cache_ttl=5.0))

    # First call fills cache at t=0
    await client.list_tickets(repo_id="robotsix-chat")
    assert get_counter[0] == 1

    # Advance past cache_ttl
    fake_time[0] = 10.0

    await client.list_tickets(repo_id="robotsix-chat")
    assert get_counter[0] == 2  # cache expired → second HTTP call


@pytest.mark.asyncio
async def test_list_tickets_errors_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error responses must not be stored; each call hits HTTP."""
    get_counter: list[int] = [0]
    # 500 error → response text starts with "Board API error"
    resp = _MockResponse(text="Board API error 500 for GET /tickets: boom", status_code=500)
    _install_mock_client(monkeypatch, resp, counter=get_counter)

    client = BoardReader(_settings(cache_ttl=60.0))
    out1 = await client.list_tickets(repo_id="x")
    out2 = await client.list_tickets(repo_id="x")

    assert "500" in out1
    assert "500" in out2
    assert get_counter[0] == 2  # errors not cached → both calls hit HTTP


@pytest.mark.asyncio
async def test_create_ticket_invalidates_list_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_ticket must clear _list_cache, forcing a list refetch."""
    get_counter: list[int] = [0]
    post_counter: list[int] = [0]

    list_resp = _MockResponse(text='[{"id": "abc"}]')
    create_resp = _MockResponse(text='{"id": "xyz"}', status_code=201)

    captured: dict[str, Any] = {}

    class _BothClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_BothClient":
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
            captured.setdefault("params_list", []).append(params)
            get_counter[0] += 1
            return list_resp

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, str] | None = None,
        ) -> _MockResponse:
            captured["post_url"] = url
            captured["post_headers"] = headers
            captured["post_json"] = json
            post_counter[0] += 1
            return create_resp

    monkeypatch.setattr(httpx, "AsyncClient", _BothClient)

    client = BoardReader(_settings(cache_ttl=60.0))

    # 1) Populate list cache
    await client.list_tickets(repo_id="robotsix-chat")
    assert get_counter[0] == 1

    # 2) Create a ticket → invalidates list cache
    await client.create_ticket(
        title="T", description="D", repo_id="robotsix-chat"
    )
    assert post_counter[0] == 1

    # 3) List again → must refetch (cache was cleared by create)
    await client.list_tickets(repo_id="robotsix-chat")
    assert get_counter[0] == 2  # second list HTTP call
    assert post_counter[0] == 1  # still exactly one POST


@pytest.mark.asyncio
async def test_get_ticket_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second get_ticket for the same id must hit cache (0 extra HTTP)."""
    get_counter: list[int] = [0]
    resp = _MockResponse(text='{"id": "abc", "title": "Fix bug"}')
    _install_mock_client(monkeypatch, resp, counter=get_counter)

    client = BoardReader(_settings(cache_ttl=60.0))
    out1 = await client.get_ticket("abc")
    out2 = await client.get_ticket("abc")

    assert out1 == out2 == '{"id": "abc", "title": "Fix bug"}'
    assert get_counter[0] == 1  # only the first call hit HTTP

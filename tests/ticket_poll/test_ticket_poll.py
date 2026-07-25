"""Tests for the ticket_poll tool.

Uses ``respx`` (httpx transport-layer mocking) so the tests
run without a real network and never touch the board API.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings, Settings
from robotsix_chat.ticket_poll import build_ticket_poll_tools, load_ticket_poll_skill


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "board_api_base_url": "http://board:8077",
        "board_api_token": "",
        "timeout": 10.0,
    }
    base.update(kw)
    return Settings(direct_repo=DirectRepoSettings(**base))


# ---------------------------------------------------------------------------
# load_ticket_poll_skill
# ---------------------------------------------------------------------------


def test_load_ticket_poll_skill_returns_content() -> None:
    """The shipped skill.md is loadable and contains expected markers."""
    skill = load_ticket_poll_skill()
    assert len(skill) > 50
    assert "ticket_poll" in skill
    assert "board" in skill.lower()


def test_load_ticket_poll_skill_missing_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When skill.md is missing, returns empty string without raising."""
    import robotsix_chat.ticket_poll as tp

    monkeypatch.setattr(tp, "__file__", "/nonexistent/__init__.py")
    assert load_ticket_poll_skill() == ""


# ---------------------------------------------------------------------------
# build_ticket_poll_tools — disabled / empty
# ---------------------------------------------------------------------------


def test_empty_board_url_returns_empty_list() -> None:
    """Empty board_api_base_url → no tools returned."""
    tools = build_ticket_poll_tools(
        Settings(direct_repo=DirectRepoSettings(board_api_base_url=""))
    )
    assert tools == []


def test_whitespace_only_board_url_returns_empty_list() -> None:
    """Whitespace-only board_api_base_url → no tools returned."""
    tools = build_ticket_poll_tools(
        Settings(direct_repo=DirectRepoSettings(board_api_base_url="   "))
    )
    assert tools == []


# ---------------------------------------------------------------------------
# build_ticket_poll_tools — enabled
# ---------------------------------------------------------------------------


def test_configured_board_url_returns_one_tool() -> None:
    """When board_api_base_url is set, returns exactly one callable."""
    tools = build_ticket_poll_tools(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "ticket_poll"


@pytest.mark.asyncio
async def test_ticket_poll_success(respx_mock: respx.MockRouter) -> None:
    """On success, returns JSON with ticket_id, state, and empty error."""
    route = respx_mock.get("http://board:8077/tickets/test-123").mock(
        return_value=httpx.Response(
            200,
            json={"state": "DONE", "title": "Fix bug"},
        )
    )

    tools = build_ticket_poll_tools(_settings())
    result = json.loads(await tools[0]("test-123"))

    assert route.called
    assert result["ticket_id"] == "test-123"
    assert result["state"] == "DONE"
    assert result["error"] == ""


@pytest.mark.asyncio
async def test_ticket_poll_state_null_when_absent(respx_mock: respx.MockRouter) -> None:
    """When response JSON has no 'state' key, state is null."""
    respx_mock.get("http://board:8077/tickets/test-456").mock(
        return_value=httpx.Response(200, json={"title": "No state field"})
    )

    tools = build_ticket_poll_tools(_settings())
    result = json.loads(await tools[0]("test-456"))

    assert result["ticket_id"] == "test-456"
    assert result["state"] is None
    assert result["error"] == ""


@pytest.mark.asyncio
async def test_ticket_poll_with_auth_token(respx_mock: respx.MockRouter) -> None:
    """When board_api_token is set, Authorization header is sent."""
    route = respx_mock.get("http://board:8077/tickets/test-auth").mock(
        return_value=httpx.Response(200, json={"state": "IN_PROGRESS"})
    )

    tools = build_ticket_poll_tools(_settings(board_api_token="secret-token"))
    result = json.loads(await tools[0]("test-auth"))

    assert route.called
    request_headers = route.calls.last.request.headers
    assert request_headers["Authorization"] == "Bearer secret-token"
    assert result["state"] == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_ticket_poll_strips_trailing_slash(respx_mock: respx.MockRouter) -> None:
    """Trailing slash on board_api_base_url is stripped."""
    route = respx_mock.get("http://board:8077/tickets/test-slash").mock(
        return_value=httpx.Response(200, json={"state": "OPEN"})
    )

    tools = build_ticket_poll_tools(_settings(board_api_base_url="http://board:8077/"))
    result = json.loads(await tools[0]("test-slash"))

    assert route.called
    assert result["state"] == "OPEN"


# ---------------------------------------------------------------------------
# HTTP error responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_poll_http_404(respx_mock: respx.MockRouter) -> None:
    """HTTP 404 → returns error JSON with state=null."""
    respx_mock.get("http://board:8077/tickets/not-found").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    tools = build_ticket_poll_tools(_settings())
    result = json.loads(await tools[0]("not-found"))

    assert result["ticket_id"] == "not-found"
    assert result["state"] is None
    assert "404" in result["error"]


@pytest.mark.asyncio
async def test_ticket_poll_http_500(respx_mock: respx.MockRouter) -> None:
    """HTTP 500 → returns error JSON with state=null."""
    respx_mock.get("http://board:8077/tickets/server-error").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    tools = build_ticket_poll_tools(_settings())
    result = json.loads(await tools[0]("server-error"))

    assert result["ticket_id"] == "server-error"
    assert result["state"] is None
    assert "500" in result["error"]


# ---------------------------------------------------------------------------
# Network / transport errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_poll_timeout(respx_mock: respx.MockRouter) -> None:
    """Timeout → returns error JSON with state=null."""
    respx_mock.get("http://board:8077/tickets/timeout-id").mock(
        side_effect=httpx.TimeoutException("timed out")
    )

    tools = build_ticket_poll_tools(_settings())
    result = json.loads(await tools[0]("timeout-id"))

    assert result["ticket_id"] == "timeout-id"
    assert result["state"] is None
    assert "timed out" in result["error"].lower()
    assert "10.0s" in result["error"]


@pytest.mark.asyncio
async def test_ticket_poll_connect_error(respx_mock: respx.MockRouter) -> None:
    """Connection error → returns error JSON with state=null."""
    respx_mock.get("http://board:8077/tickets/conn-fail").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    tools = build_ticket_poll_tools(_settings())
    result = json.loads(await tools[0]("conn-fail"))

    assert result["ticket_id"] == "conn-fail"
    assert result["state"] is None
    assert "connection refused" in result["error"]


@pytest.mark.asyncio
async def test_ticket_poll_json_decode_failure(respx_mock: respx.MockRouter) -> None:
    """Non-JSON response body → returns error JSON with state=null."""
    respx_mock.get("http://board:8077/tickets/bad-json").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><body>Not JSON</body></html>",
        )
    )

    tools = build_ticket_poll_tools(_settings())
    result = json.loads(await tools[0]("bad-json"))

    assert result["ticket_id"] == "bad-json"
    assert result["state"] is None
    assert "Non-JSON" in result["error"]


@pytest.mark.asyncio
async def test_ticket_poll_empty_body_json_decode_failure(
    respx_mock: respx.MockRouter,
) -> None:
    """Empty response body → returns error JSON with state=null."""
    respx_mock.get("http://board:8077/tickets/empty-body").mock(
        return_value=httpx.Response(200, text="")
    )

    tools = build_ticket_poll_tools(_settings())
    result = json.loads(await tools[0]("empty-body"))

    assert result["ticket_id"] == "empty-body"
    assert result["state"] is None
    assert "Non-JSON" in result["error"]

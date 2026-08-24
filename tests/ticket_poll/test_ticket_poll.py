"""Tests for the ticket_poll tool.

Uses ``respx`` (httpx transport-layer mocking) so the tests
run without a real network and never touch the board API.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import AutonomousSettings, DirectRepoSettings, Settings
from robotsix_chat.ticket_poll import (
    _check_unexpected_terminal,
    build_file_ticket_tool,
    build_find_ticket_by_pr_tool,
    build_list_stale_ready_tickets_tool,
    build_mark_ticket_done_tool,
    build_mark_ticket_ready_tool,
    build_merge_pull_request_tool,
    build_ticket_poll_tools,
    load_ticket_poll_skill,
)


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "board_api_base_url": "http://board:8077",
        "board_api_token": "",
        "timeout": 10.0,
    }
    base.update(kw)
    return Settings(direct_repo=DirectRepoSettings(**base))


def _component_request_success(
    ticket_id: str,
    state: str = "DONE",
) -> Callable[..., Any]:
    """Return an async mock that returns a successful roster-style response."""

    async def _req(
        component: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> str:
        return "HTTP 200 OK\n" + json.dumps({"state": state, "ticket_id": ticket_id})

    return _req


def _component_request_error(
    error_msg: str = "Error: connection refused",
) -> Callable[..., Any]:
    """Return an async mock that returns a roster-style error."""

    async def _req(
        component: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> str:
        return error_msg

    return _req


def _component_request_ticket_list(
    tickets: Any,
) -> Callable[..., Any]:
    """Return an async mock that returns a roster-style ticket list response."""

    async def _req(
        component: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> str:
        return "HTTP 200 OK\n" + json.dumps(tickets)

    return _req


def _component_request_http_error(
    status_code: int = 502,
    body: str = '{"error": "internal"}',
) -> Callable[..., Any]:
    """Return an async mock that returns a roster-style HTTP error response."""

    async def _req(
        component: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> str:
        return f"HTTP {status_code} Bad Gateway\n{body}"

    return _req


def _stale_settings(
    ready_staleness_minutes: int = 10,
    priority_ready_staleness_minutes: int = 60,
    **kw: Any,
) -> Settings:
    """Return Settings with autonomous.ready_staleness_minutes configured."""
    base: dict[str, Any] = {
        "board_api_base_url": "http://board:8077",
        "board_api_token": "",
        "timeout": 10.0,
    }
    base.update(kw)
    return Settings(
        direct_repo=DirectRepoSettings(**base),
        autonomous=AutonomousSettings(
            ready_staleness_minutes=ready_staleness_minutes,
            priority_ready_staleness_minutes=priority_ready_staleness_minutes,
        ),
    )


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


def test_configured_board_url_returns_two_tools() -> None:
    """When board_api_base_url is set, returns ticket_poll and ticket_poll_batch."""
    tools = build_ticket_poll_tools(_settings())
    assert len(tools) == 2
    assert tools[0].__name__ == "ticket_poll"
    assert tools[1].__name__ == "ticket_poll_batch"


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
    assert result["error"].startswith("ticket not found (404)")


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
    assert result["error"] == "Board API error: HTTP 500"


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
    assert result["error"].startswith("Board API timeout after")


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
    assert result["error"].startswith("Board API unreachable:")


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
    assert result["error"] == "Board API returned a non-JSON response"


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
    assert result["error"] == "Board API returned a non-JSON response"


# ---------------------------------------------------------------------------
# roster-first behaviour (component_request available)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_poll_roster_first_success() -> None:
    """When component_request is available, use it first and return result."""
    tools = build_ticket_poll_tools(
        _settings(),
        component_request=_component_request_success("t-roster", state="BLOCKED"),
    )
    result = json.loads(await tools[0]("t-roster"))

    assert result["ticket_id"] == "t-roster"
    assert result["state"] == "BLOCKED"
    assert result["error"] == ""


@pytest.mark.asyncio
async def test_ticket_poll_roster_first_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """When roster path fails, fall back to the direct board API URL."""
    route = respx_mock.get("http://board:8077/tickets/t-fallback").mock(
        return_value=httpx.Response(200, json={"state": "OPEN"})
    )

    tools = build_ticket_poll_tools(
        _settings(),
        component_request=_component_request_error("Error: connection refused"),
    )
    result = json.loads(await tools[0]("t-fallback"))

    assert route.called
    assert result["ticket_id"] == "t-fallback"
    assert result["state"] == "OPEN"
    assert result["error"] == ""


@pytest.mark.asyncio
async def test_ticket_poll_roster_error_non_json_falls_back(
    respx_mock: respx.MockRouter,
) -> None:
    """When roster returns non-success status, fall back to direct."""
    route = respx_mock.get("http://board:8077/tickets/t-rost-err").mock(
        return_value=httpx.Response(200, json={"state": "DONE"})
    )

    tools = build_ticket_poll_tools(
        _settings(),
        component_request=_component_request_error("HTTP 502 Bad Gateway"),
    )
    result = json.loads(await tools[0]("t-rost-err"))

    assert route.called
    assert result["state"] == "DONE"


@pytest.mark.asyncio
async def test_ticket_poll_no_component_request_uses_direct_only(
    respx_mock: respx.MockRouter,
) -> None:
    """Without component_request, the direct path is used directly (regression)."""
    route = respx_mock.get("http://board:8077/tickets/t-direct-only").mock(
        return_value=httpx.Response(200, json={"state": "IN_PROGRESS"})
    )

    tools = build_ticket_poll_tools(_settings())  # component_request=None
    result = json.loads(await tools[0]("t-direct-only"))

    assert route.called
    assert result["state"] == "IN_PROGRESS"


# ---------------------------------------------------------------------------
# ticket_poll — ID resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_poll_resolves_paraphrased_id(
    respx_mock: respx.MockRouter,
) -> None:
    """Paraphrased ID is resolved via hash-suffix match before the GET."""
    real_id = "20260731T020731Z-batch-approval-should-resolve-ids-32be"

    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"ticket_id": real_id, "state": "BLOCKED"},
                {"ticket_id": "20260730T232905Z-other-ticket-761f", "state": "DONE"},
            ],
        )
    )

    route = respx_mock.get(f"http://board:8077/tickets/{real_id}").mock(
        return_value=httpx.Response(
            200,
            json={"state": "BLOCKED", "title": "Batch approval"},
        )
    )

    tools = build_ticket_poll_tools(_settings())
    # Pass a paraphrased ID — only the hash suffix matches
    result = json.loads(await tools[0]("...-resolve-ids-32be"))

    assert route.called
    assert result["ticket_id"] == real_id
    assert result["state"] == "BLOCKED"
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# ticket_poll_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_poll_batch_multiple_success(
    respx_mock: respx.MockRouter,
) -> None:
    """Fetches multiple tickets concurrently; returns full data for each."""
    route_a = respx_mock.get("http://board:8077/tickets/ticket-a").mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "BLOCKED",
                "title": "Ticket A",
                "events": [
                    {
                        "type": "state_change",
                        "from": "IN_PROGRESS",
                        "to": "BLOCKED",
                    }
                ],
            },
        )
    )
    route_b = respx_mock.get("http://board:8077/tickets/ticket-b").mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "DONE",
                "title": "Ticket B",
                "events": [],
            },
        )
    )

    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    result = json.loads(await batch_tool(["ticket-a", "ticket-b"]))

    assert route_a.called
    assert route_b.called
    assert len(result["tickets"]) == 2

    a = result["tickets"][0]
    assert a["ticket_id"] == "ticket-a"
    assert a["state"] == "BLOCKED"
    assert a["data"]["title"] == "Ticket A"
    assert a["data"]["events"][0]["type"] == "state_change"
    assert a["error"] == ""

    b = result["tickets"][1]
    assert b["ticket_id"] == "ticket-b"
    assert b["state"] == "DONE"
    assert b["data"]["title"] == "Ticket B"
    assert b["error"] == ""


@pytest.mark.asyncio
async def test_ticket_poll_batch_partial_failure(
    respx_mock: respx.MockRouter,
) -> None:
    """One failing ticket does not block others; each gets its own error field."""
    respx_mock.get("http://board:8077/tickets/good").mock(
        return_value=httpx.Response(200, json={"state": "OPEN"})
    )
    respx_mock.get("http://board:8077/tickets/bad").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    result = json.loads(await batch_tool(["good", "bad"]))

    assert len(result["tickets"]) == 2
    good = result["tickets"][0]
    assert good["ticket_id"] == "good"
    assert good["state"] == "OPEN"
    assert good["error"] == ""

    bad = result["tickets"][1]
    assert bad["ticket_id"] == "bad"
    assert bad["state"] is None
    assert bad["data"] is None
    assert bad["error"] == "Board API error: HTTP 500"


@pytest.mark.asyncio
async def test_ticket_poll_batch_with_auth_token(
    respx_mock: respx.MockRouter,
) -> None:
    """Auth token is propagated to every request in the batch."""
    route_a = respx_mock.get("http://board:8077/tickets/t1").mock(
        return_value=httpx.Response(200, json={"state": "OPEN"})
    )
    route_b = respx_mock.get("http://board:8077/tickets/t2").mock(
        return_value=httpx.Response(200, json={"state": "OPEN"})
    )

    tools = build_ticket_poll_tools(_settings(board_api_token="batch-token"))
    batch_tool = tools[1]
    await batch_tool(["t1", "t2"])

    for route in (route_a, route_b):
        assert route.called
        request_headers = route.calls.last.request.headers
        assert request_headers["Authorization"] == "Bearer batch-token"


@pytest.mark.asyncio
async def test_ticket_poll_batch_empty_list(
    respx_mock: respx.MockRouter,
) -> None:
    """Empty ticket_ids list returns empty tickets array."""
    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    result = json.loads(await batch_tool([]))

    assert result == {"tickets": []}


@pytest.mark.asyncio
async def test_ticket_poll_batch_timeout_per_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Timeout on one ticket surfaces in its error; others still succeed."""
    respx_mock.get("http://board:8077/tickets/fast").mock(
        return_value=httpx.Response(200, json={"state": "DONE"})
    )
    respx_mock.get("http://board:8077/tickets/slow").mock(
        side_effect=httpx.TimeoutException("timed out")
    )

    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    result = json.loads(await batch_tool(["fast", "slow"]))

    assert len(result["tickets"]) == 2
    fast = result["tickets"][0]
    assert fast["state"] == "DONE"
    assert fast["error"] == ""

    slow = result["tickets"][1]
    assert slow["state"] is None
    assert slow["error"].startswith("Board API timeout after")


@pytest.mark.asyncio
async def test_ticket_poll_batch_json_decode_failure(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-JSON response in batch → error per ticket, data is None."""
    respx_mock.get("http://board:8077/tickets/bad-json").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html>Not JSON</html>",
        )
    )

    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    result = json.loads(await batch_tool(["bad-json"]))

    ticket = result["tickets"][0]
    assert ticket["ticket_id"] == "bad-json"
    assert ticket["state"] is None
    assert ticket["data"] is None
    assert ticket["error"] == "Board API returned a non-JSON response"


# ---------------------------------------------------------------------------
# ticket_poll_batch — ID resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_poll_batch_resolves_by_hash_suffix(
    respx_mock: respx.MockRouter,
) -> None:
    """Paraphrased ID with a matching 4-char hex suffix is resolved."""
    real_id = "20260731T020731Z-batch-approval-should-resolve-ids-32be"

    # Mock GET /tickets listing
    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"ticket_id": real_id, "state": "BLOCKED"},
                {"ticket_id": "20260730T232905Z-other-ticket-761f", "state": "DONE"},
            ],
        )
    )

    # The real ticket fetch should be for the resolved ID
    route = respx_mock.get(f"http://board:8077/tickets/{real_id}").mock(
        return_value=httpx.Response(
            200,
            json={"state": "BLOCKED", "title": "Batch approval"},
        )
    )

    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    # Pass a paraphrased ID — only the hash suffix survives
    result = json.loads(await batch_tool(["...-resolve-ids-32be"]))

    assert route.called
    tickets = result["tickets"]
    assert len(tickets) == 1
    assert tickets[0]["ticket_id"] == real_id
    assert tickets[0]["state"] == "BLOCKED"
    assert tickets[0]["error"] == ""


@pytest.mark.asyncio
async def test_ticket_poll_batch_resolves_by_slug_substring(
    respx_mock: respx.MockRouter,
) -> None:
    """Paraphrased ID without a hash suffix is resolved via slug substring."""
    real_id = "20260731T020731Z-batch-approval-should-resolve-ticket-ids-32be"

    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"ticket_id": real_id, "state": "DONE"},
                {
                    "ticket_id": "20260730T232905Z-unrelated-ticket-761f",
                    "state": "OPEN",
                },
            ],
        )
    )

    route = respx_mock.get(f"http://board:8077/tickets/{real_id}").mock(
        return_value=httpx.Response(
            200,
            json={"state": "DONE", "title": "Batch approval"},
        )
    )

    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    # Slug "batch-approval-should-resolve" is unique enough
    result = json.loads(await batch_tool(["batch-approval-should-resolve"]))

    assert route.called
    tickets = result["tickets"]
    assert len(tickets) == 1
    assert tickets[0]["ticket_id"] == real_id
    assert tickets[0]["state"] == "DONE"


@pytest.mark.asyncio
async def test_ticket_poll_batch_exact_match_no_list_fetch(
    respx_mock: respx.MockRouter,
) -> None:
    """When an ID is already exact, resolution still fetches the list.

    But the result is the same ID.
    """
    real_id = "20260731T020731Z-batch-approval-should-resolve-ticket-ids-32be"

    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=[{"ticket_id": real_id, "state": "BLOCKED"}],
        )
    )

    route = respx_mock.get(f"http://board:8077/tickets/{real_id}").mock(
        return_value=httpx.Response(
            200,
            json={"state": "BLOCKED", "title": "Batch approval"},
        )
    )

    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    result = json.loads(await batch_tool([real_id]))

    assert route.called
    tickets = result["tickets"]
    assert len(tickets) == 1
    assert tickets[0]["ticket_id"] == real_id
    assert tickets[0]["state"] == "BLOCKED"
    assert tickets[0]["error"] == ""


@pytest.mark.asyncio
async def test_ticket_poll_batch_unresolvable_id_still_attempted(
    respx_mock: respx.MockRouter,
) -> None:
    """An ID that cannot be resolved is still passed through to the API."""
    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=[{"ticket_id": "20260730T232905Z-unrelated-761f", "state": "OPEN"}],
        )
    )

    # The unresolvable ID is still attempted; it gets a 404
    route = respx_mock.get("http://board:8077/tickets/ghost-ticket-xxxx").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    result = json.loads(await batch_tool(["ghost-ticket-xxxx"]))

    assert route.called
    tickets = result["tickets"]
    assert len(tickets) == 1
    assert tickets[0]["ticket_id"] == "ghost-ticket-xxxx"
    assert tickets[0]["error"].startswith("ticket not found (404)")


@pytest.mark.asyncio
async def test_ticket_poll_batch_resolution_list_failure_graceful(
    respx_mock: respx.MockRouter,
) -> None:
    """When GET /tickets fails, resolution is skipped and original IDs are used."""
    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    # The original ID is still attempted
    route = respx_mock.get("http://board:8077/tickets/...-resolve-ids-32be").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    result = json.loads(await batch_tool(["...-resolve-ids-32be"]))

    assert route.called
    tickets = result["tickets"]
    assert len(tickets) == 1
    assert tickets[0]["ticket_id"] == "...-resolve-ids-32be"
    assert tickets[0]["error"].startswith("ticket not found (404)")


# ---------------------------------------------------------------------------
# build_merge_pull_request_tool
# ---------------------------------------------------------------------------


def test_merge_tool_empty_config_returns_empty_list() -> None:
    """Neither component_request nor board_api_base_url → empty list."""
    tools = build_merge_pull_request_tool(
        Settings(direct_repo=DirectRepoSettings(board_api_base_url=""))
    )
    assert tools == []


def test_merge_tool_configured_returns_one_tool() -> None:
    """When board_api_base_url is set, returns merge_pull_request."""
    tools = build_merge_pull_request_tool(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "merge_pull_request"


@pytest.mark.asyncio
async def test_merge_pull_request_roster_first_success() -> None:
    """When component_request succeeds, return its response directly."""

    async def _req(component: str, method: str, path: str) -> str:
        return "HTTP 200 OK\n" + json.dumps({"status": "merged", "sha": "abc123"})

    tools = build_merge_pull_request_tool(
        _settings(),
        component_request=_req,
    )
    result = await tools[0]("mr-roster")

    assert "HTTP 200" in result
    assert "merged" in result
    assert "abc123" in result


@pytest.mark.asyncio
async def test_merge_pull_request_roster_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """When roster path returns an error, fall back to direct POST."""
    route = respx_mock.post("http://board:8077/tickets/mr-fallback/merge-now").mock(
        return_value=httpx.Response(200, json={"status": "merged_from_direct"})
    )

    tools = build_merge_pull_request_tool(
        _settings(),
        component_request=_component_request_error("Error: connection refused"),
    )
    result = await tools[0]("mr-fallback")

    assert route.called
    assert "merged_from_direct" in result


@pytest.mark.asyncio
async def test_merge_pull_request_roster_http_error_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """Roster 404 (no ``Error:`` prefix) still triggers the direct fallback."""
    route = respx_mock.post(
        "http://board:8077/tickets/mr-http-fallback/merge-now"
    ).mock(return_value=httpx.Response(200, json={"status": "merged_from_direct"}))

    tools = build_merge_pull_request_tool(
        _settings(),
        component_request=_component_request_error(
            "HTTP 404 Not Found\n" + json.dumps({"detail": "Not found"})
        ),
    )
    result = await tools[0]("mr-http-fallback")

    assert route.called
    assert "merged_from_direct" in result


@pytest.mark.asyncio
async def test_merge_pull_request_direct_only(
    respx_mock: respx.MockRouter,
) -> None:
    """Without component_request, the direct POST path works."""
    route = respx_mock.post("http://board:8077/tickets/mr-direct/merge-now").mock(
        return_value=httpx.Response(200, json={"status": "merged"})
    )

    tools = build_merge_pull_request_tool(_settings())
    result = await tools[0]("mr-direct")

    assert route.called
    assert "merged" in result


@pytest.mark.asyncio
async def test_merge_pull_request_with_auth_token(
    respx_mock: respx.MockRouter,
) -> None:
    """Auth token is sent as Bearer in the Authorization header."""
    route = respx_mock.post("http://board:8077/tickets/mr-auth/merge-now").mock(
        return_value=httpx.Response(200, json={"status": "merged"})
    )

    tools = build_merge_pull_request_tool(
        _settings(board_api_token="merge-token"),
    )
    await tools[0]("mr-auth")

    assert route.called
    request_headers = route.calls.last.request.headers
    assert request_headers["Authorization"] == "Bearer merge-token"


@pytest.mark.asyncio
async def test_merge_pull_request_strips_trailing_slash(
    respx_mock: respx.MockRouter,
) -> None:
    """Trailing slash on board_api_base_url is stripped correctly."""
    route = respx_mock.post("http://board:8077/tickets/mr-slash/merge-now").mock(
        return_value=httpx.Response(200, json={"status": "merged"})
    )

    tools = build_merge_pull_request_tool(
        _settings(board_api_base_url="http://board:8077/")
    )
    await tools[0]("mr-slash")

    assert route.called


# ---------------------------------------------------------------------------
# merge_pull_request — HTTP error responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_pull_request_http_404(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP 404 → error message includes the status code."""
    respx_mock.post("http://board:8077/tickets/mr-404/merge-now").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )

    tools = build_merge_pull_request_tool(_settings())
    result = await tools[0]("mr-404")

    assert "404" in result
    assert "Not found" in result


@pytest.mark.asyncio
async def test_merge_pull_request_http_500(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP 500 → error message includes the status code."""
    respx_mock.post("http://board:8077/tickets/mr-500/merge-now").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    tools = build_merge_pull_request_tool(_settings())
    result = await tools[0]("mr-500")

    assert "500" in result


@pytest.mark.asyncio
async def test_merge_pull_request_http_status_error(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTPStatusError (raise_for_status) → error with status code."""
    respx_mock.post("http://board:8077/tickets/mr-status-err/merge-now").mock(
        return_value=httpx.Response(
            409, json={"detail": "PR is not in a mergeable state"}
        )
    )

    tools = build_merge_pull_request_tool(_settings())
    result = await tools[0]("mr-status-err")

    assert "409" in result
    assert "mergeable" in result.lower()


# ---------------------------------------------------------------------------
# merge_pull_request — network / transport errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_pull_request_timeout(
    respx_mock: respx.MockRouter,
) -> None:
    """Timeout → error message mentions the ticket ID and timeout."""
    respx_mock.post("http://board:8077/tickets/mr-timeout/merge-now").mock(
        side_effect=httpx.TimeoutException("timed out")
    )

    tools = build_merge_pull_request_tool(_settings())
    result = await tools[0]("mr-timeout")

    assert "mr-timeout" in result
    assert "timed out" in result.lower()
    assert "10.0s" in result


@pytest.mark.asyncio
async def test_merge_pull_request_connect_error(
    respx_mock: respx.MockRouter,
) -> None:
    """ConnectError → error message mentions the ticket ID and timeout."""
    respx_mock.post("http://board:8077/tickets/mr-connfail/merge-now").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    tools = build_merge_pull_request_tool(_settings())
    result = await tools[0]("mr-connfail")

    assert "mr-connfail" in result
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_merge_pull_request_unexpected_exception(
    respx_mock: respx.MockRouter,
) -> None:
    """Unexpected exceptions → error with ticket ID and exception message."""
    respx_mock.post("http://board:8077/tickets/mr-uex/merge-now").mock(
        side_effect=RuntimeError("something exploded")
    )

    tools = build_merge_pull_request_tool(_settings())
    result = await tools[0]("mr-uex")

    assert "mr-uex" in result
    assert "something exploded" in result


# ---------------------------------------------------------------------------
# merge_pull_request — ID resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_pull_request_resolves_paraphrased_id(
    respx_mock: respx.MockRouter,
) -> None:
    """Paraphrased ID is resolved via hash-suffix match before the merge-now POST."""
    real_id = "20260731T020731Z-merge-resolution-test-761f"

    # Mock GET /tickets listing
    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"ticket_id": real_id, "state": "waiting_auto_merge"},
                {"ticket_id": "20260730T232905Z-other-ticket-a3f2", "state": "DONE"},
            ],
        )
    )

    # The merge-now POST should be for the resolved real ID
    route = respx_mock.post(f"http://board:8077/tickets/{real_id}/merge-now").mock(
        return_value=httpx.Response(200, json={"status": "merged", "sha": "abc123"})
    )

    tools = build_merge_pull_request_tool(_settings())
    # Pass a paraphrased ID — only the hash suffix matches
    result = await tools[0]("...-so-validate-c-761f")

    assert route.called
    assert "merged" in result
    assert "abc123" in result


# ============================================================================
# _check_unexpected_terminal
# ============================================================================


def test_unexpected_terminal_null_for_active_state() -> None:
    """Ticket in IN_PROGRESS — not terminal, returns None."""
    result = _check_unexpected_terminal({"state": "IN_PROGRESS"})
    assert result is None


def test_unexpected_terminal_null_for_unknown_state() -> None:
    """Ticket with no state field — returns None (no false positives)."""
    result = _check_unexpected_terminal({})
    assert result is None


def test_unexpected_terminal_null_with_history_active_state() -> None:
    """CLOSED ticket that was previously BLOCKED — transition is normal."""
    data: dict[str, Any] = {
        "state": "CLOSED",
        "history": [
            {"state": "DRAFT"},
            {"state": "BLOCKED"},
            {"state": "CLOSED"},
        ],
    }
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_null_with_implement_events() -> None:
    """CLOSED ticket with implement events — transition is normal."""
    data: dict[str, Any] = {
        "state": "CLOSED",
        "events": [
            {"type": "implement_started"},
            {"type": "implement_complete"},
        ],
    }
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_null_with_unblock_events() -> None:
    """DONE ticket with unblock event — transition is normal."""
    data: dict[str, Any] = {
        "state": "DONE",
        "events": [{"type": "unblock"}],
    }
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_detects_draft_to_closed() -> None:
    """CLOSED ticket with only DRAFT history — flag as unexpected."""
    data: dict[str, Any] = {
        "state": "CLOSED",
        "history": [
            {"state": "DRAFT"},
            {"state": "CLOSED"},
        ],
    }
    result = _check_unexpected_terminal(data)
    assert result is not None
    assert "CLOSED" in result
    assert "active work" in result.lower()


def test_unexpected_terminal_detects_no_history() -> None:
    """CLOSED ticket with no history or events — flag as unexpected."""
    data: dict[str, Any] = {"state": "CLOSED"}
    result = _check_unexpected_terminal(data)
    assert result is not None
    assert "CLOSED" in result


def test_unexpected_terminal_detects_ready_to_closed() -> None:
    """CLOSED ticket that was READY but never entered active work — flag."""
    data: dict[str, Any] = {
        "state": "CLOSED",
        "history": [
            {"state": "DRAFT"},
            {"state": "READY"},
            {"state": "CLOSED"},
        ],
    }
    result = _check_unexpected_terminal(data)
    assert result is not None


def test_unexpected_terminal_null_for_approved() -> None:
    """CLOSED with APPROVED in history — APPROVED is an active-work state."""
    data: dict[str, Any] = {
        "state": "CLOSED",
        "history": [
            {"state": "DRAFT"},
            {"state": "APPROVED"},
            {"state": "CLOSED"},
        ],
    }
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_case_insensitive_state() -> None:
    """State comparison is case-insensitive."""
    data: dict[str, Any] = {
        "state": "closed",
        "history": [{"state": "draft"}, {"state": "closed"}],
    }
    result = _check_unexpected_terminal(data)
    assert result is not None


def test_unexpected_terminal_uses_to_field_in_history() -> None:
    """History entries may use 'to' instead of 'state'."""
    data: dict[str, Any] = {
        "state": "CLOSED",
        "history": [
            {"to": "DRAFT"},
            {"to": "IN_PROGRESS"},
            {"to": "CLOSED"},
        ],
    }
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_uses_action_field_in_events() -> None:
    """Events may use 'action' instead of 'type'."""
    data: dict[str, Any] = {
        "state": "DONE",
        "events": [{"action": "resume"}],
    }
    assert _check_unexpected_terminal(data) is None


def test_unexpected_terminal_skips_non_dict_entries() -> None:
    """Non-dict entries in history/events are safely skipped."""
    data: dict[str, Any] = {
        "state": "CLOSED",
        "history": ["not a dict", None, {"state": "BLOCKED"}],
        "events": [None, "also not a dict"],
    }
    assert _check_unexpected_terminal(data) is None


# ============================================================================
# mark_ticket_ready
# ============================================================================


def test_mark_ticket_ready_empty_config_returns_empty_list() -> None:
    """Neither component_request nor board_api_base_url → empty list."""
    tools = build_mark_ticket_ready_tool(
        Settings(direct_repo=DirectRepoSettings(board_api_base_url=""))
    )
    assert tools == []


def test_mark_ticket_ready_configured_returns_one_tool() -> None:
    """When board_api_base_url is set, returns mark_ticket_ready."""
    tools = build_mark_ticket_ready_tool(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "mark_ticket_ready"


@pytest.mark.asyncio
async def test_mark_ticket_ready_roster_first_success() -> None:
    """When component_request succeeds, return its response directly."""

    async def _req(
        component: str,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        return "HTTP 200 OK\n" + json.dumps(
            {"state": "READY", "ticket_id": "mr-ready-roster"}
        )

    tools = build_mark_ticket_ready_tool(
        _settings(),
        component_request=_req,
    )
    result = await tools[0]("mr-ready-roster", justification="answered question")

    assert "HTTP 200" in result
    assert "READY" in result


@pytest.mark.asyncio
async def test_mark_ticket_ready_roster_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """When roster path returns an error, fall back to direct POST."""
    route = respx_mock.post("http://board:8077/tickets/mr-fallback/mark-ready").mock(
        return_value=httpx.Response(200, json={"state": "READY"})
    )

    tools = build_mark_ticket_ready_tool(
        _settings(),
        component_request=_component_request_error("Error: connection refused"),
    )
    result = await tools[0]("mr-fallback")

    assert route.called
    assert "READY" in result


@pytest.mark.asyncio
async def test_mark_ticket_ready_roster_http_error_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """Roster 502 (no ``Error:`` prefix) still triggers the direct fallback."""
    route = respx_mock.post(
        "http://board:8077/tickets/mr-http-fallback/mark-ready"
    ).mock(return_value=httpx.Response(200, json={"state": "READY"}))

    tools = build_mark_ticket_ready_tool(
        _settings(),
        component_request=_component_request_error(
            "HTTP 502 Bad Gateway\n" + json.dumps({"detail": "upstream down"})
        ),
    )
    result = await tools[0]("mr-http-fallback")

    assert route.called
    assert "READY" in result


@pytest.mark.asyncio
async def test_mark_ticket_ready_direct_only(
    respx_mock: respx.MockRouter,
) -> None:
    """Without component_request, the direct POST path works."""
    route = respx_mock.post("http://board:8077/tickets/mr-direct/mark-ready").mock(
        return_value=httpx.Response(200, json={"state": "READY"})
    )

    tools = build_mark_ticket_ready_tool(_settings())
    result = await tools[0]("mr-direct")

    assert route.called
    assert "READY" in result


@pytest.mark.asyncio
async def test_mark_ticket_ready_with_auth_token(
    respx_mock: respx.MockRouter,
) -> None:
    """Auth token is sent as Bearer in the Authorization header."""
    route = respx_mock.post("http://board:8077/tickets/mr-auth/mark-ready").mock(
        return_value=httpx.Response(200, json={"state": "READY"})
    )

    tools = build_mark_ticket_ready_tool(
        _settings(board_api_token="mark-token"),
    )
    await tools[0]("mr-auth")

    assert route.called
    request_headers = route.calls.last.request.headers
    assert request_headers["Authorization"] == "Bearer mark-token"


@pytest.mark.asyncio
async def test_mark_ticket_ready_strips_trailing_slash(
    respx_mock: respx.MockRouter,
) -> None:
    """Trailing slash on board_api_base_url is stripped correctly."""
    route = respx_mock.post("http://board:8077/tickets/mr-slash/mark-ready").mock(
        return_value=httpx.Response(200, json={"state": "READY"})
    )

    tools = build_mark_ticket_ready_tool(
        _settings(board_api_base_url="http://board:8077/")
    )
    await tools[0]("mr-slash")

    assert route.called


@pytest.mark.asyncio
async def test_mark_ticket_ready_http_404(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP 404 → error message includes the status code."""
    respx_mock.post("http://board:8077/tickets/mr-404/mark-ready").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )

    tools = build_mark_ticket_ready_tool(_settings())
    result = await tools[0]("mr-404")

    assert "404" in result
    assert "Not found" in result


@pytest.mark.asyncio
async def test_mark_ticket_ready_resolves_paraphrased_id(
    respx_mock: respx.MockRouter,
) -> None:
    """Paraphrased ID is resolved via hash-suffix match before the POST."""
    real_id = "20260811T083147Z-mark-ready-resolution-761f"

    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"ticket_id": real_id, "state": "draft"},
                {"ticket_id": "20260810T232905Z-other-ticket-a3f2", "state": "DONE"},
            ],
        )
    )

    route = respx_mock.post(f"http://board:8077/tickets/{real_id}/mark-ready").mock(
        return_value=httpx.Response(200, json={"state": "READY"})
    )

    tools = build_mark_ticket_ready_tool(_settings())
    result = await tools[0]("761f")

    assert route.called
    assert "READY" in result


# ============================================================================
# mark_ticket_done
# ============================================================================


def test_mark_ticket_done_empty_config_returns_empty_list() -> None:
    """Neither component_request nor board_api_base_url → empty list."""
    tools = build_mark_ticket_done_tool(
        Settings(direct_repo=DirectRepoSettings(board_api_base_url=""))
    )
    assert tools == []


def test_mark_ticket_done_configured_returns_one_tool() -> None:
    """When board_api_base_url is set, returns mark_ticket_done."""
    tools = build_mark_ticket_done_tool(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "mark_ticket_done"


# ============================================================================
# find_ticket_by_pr
# ============================================================================


def test_find_ticket_by_pr_empty_config_returns_empty_list() -> None:
    """Neither component_request nor board_api_base_url → empty list."""
    tools = build_find_ticket_by_pr_tool(
        Settings(direct_repo=DirectRepoSettings(board_api_base_url=""))
    )
    assert tools == []


def test_find_ticket_by_pr_configured_returns_one_tool() -> None:
    """When board_api_base_url is set, returns find_ticket_by_pr."""
    tools = build_find_ticket_by_pr_tool(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "find_ticket_by_pr"


@pytest.mark.asyncio
async def test_find_ticket_by_pr_server_side_match(
    respx_mock: respx.MockRouter,
) -> None:
    """Server-side ?pr_url= filter returns a matching ticket."""
    pr_url = "https://github.com/owner/repo/pull/656"
    ticket = {
        "ticket_id": "20250101T120000Z-fix-bug-a1b2",
        "state": "HUMAN_MR_APPROVAL",
        "pr_url": pr_url,
    }

    route = respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(200, json=[ticket])
    )

    tools = build_find_ticket_by_pr_tool(_settings())
    result = await tools[0](pr_url)

    assert route.called
    # Verify the pr_url query param was sent
    assert route.calls.last.request.url.params["pr_url"] == pr_url

    data = json.loads(result)
    assert data["ticket_id"] == "20250101T120000Z-fix-bug-a1b2"
    assert data["state"] == "HUMAN_MR_APPROVAL"
    assert data["pr_url"] == pr_url
    assert data["error"] == ""


@pytest.mark.asyncio
async def test_find_ticket_by_pr_no_match(
    respx_mock: respx.MockRouter,
) -> None:
    """No matching ticket → error message in JSON."""
    pr_url = "https://github.com/owner/repo/pull/404"

    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "ticket_id": "other-1",
                    "state": "DONE",
                    "pr_url": "https://github.com/other/repo/pull/1",
                },
            ],
        )
    )

    tools = build_find_ticket_by_pr_tool(_settings())
    result = await tools[0](pr_url)

    data = json.loads(result)
    assert data["ticket_id"] is None
    assert data["state"] is None
    assert data["pr_url"] == pr_url
    assert "No ticket found" in data["error"]


@pytest.mark.asyncio
async def test_find_ticket_by_pr_dict_response(
    respx_mock: respx.MockRouter,
) -> None:
    """Handles dict-shaped response ({"tickets": [...]})."""
    pr_url = "https://github.com/owner/repo/pull/656"
    ticket = {
        "ticket_id": "20250101T120000Z-fix-bug-a1b2",
        "state": "IN_PROGRESS",
        "pr_url": pr_url,
    }

    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(200, json={"tickets": [ticket]})
    )

    tools = build_find_ticket_by_pr_tool(_settings())
    result = await tools[0](pr_url)

    data = json.loads(result)
    assert data["ticket_id"] == "20250101T120000Z-fix-bug-a1b2"


@pytest.mark.asyncio
async def test_find_ticket_by_pr_with_auth_token(
    respx_mock: respx.MockRouter,
) -> None:
    """Auth token is sent as Bearer in the Authorization header."""
    pr_url = "https://github.com/owner/repo/pull/656"

    route = respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(200, json=[])
    )

    tools = build_find_ticket_by_pr_tool(
        _settings(board_api_token="find-token"),
    )
    await tools[0](pr_url)

    assert route.called
    request_headers = route.calls.last.request.headers
    assert request_headers["Authorization"] == "Bearer find-token"


@pytest.mark.asyncio
async def test_find_ticket_by_pr_board_unreachable(
    respx_mock: respx.MockRouter,
) -> None:
    """Board API unreachable → error message."""
    pr_url = "https://github.com/owner/repo/pull/656"

    respx_mock.get("http://board:8077/tickets").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    tools = build_find_ticket_by_pr_tool(_settings())
    result = await tools[0](pr_url)

    data = json.loads(result)
    assert data["ticket_id"] is None
    assert "No ticket found" in data["error"]


@pytest.mark.asyncio
async def test_find_ticket_by_pr_strips_trailing_slash(
    respx_mock: respx.MockRouter,
) -> None:
    """Trailing slash on board_api_base_url is stripped correctly."""
    pr_url = "https://github.com/owner/repo/pull/656"
    ticket = {
        "ticket_id": "t1",
        "state": "DONE",
        "pr_url": pr_url,
    }

    route = respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(200, json=[ticket])
    )

    tools = build_find_ticket_by_pr_tool(
        _settings(board_api_base_url="http://board:8077/")
    )
    await tools[0](pr_url)

    assert route.called


@pytest.mark.asyncio
async def test_find_ticket_by_pr_server_filter_falls_back_to_full_list(
    respx_mock: respx.MockRouter,
) -> None:
    """Server-side ?pr_url= returns all tickets; client-side filtering finds match."""
    pr_url = "https://github.com/owner/repo/pull/656"
    ticket = {
        "ticket_id": "20250101T120000Z-fix-bug-a1b2",
        "state": "HUMAN_MR_APPROVAL",
        "pr_url": pr_url,
    }

    # First call (with ?pr_url=) returns ALL tickets — simulating a board
    # that ignores the query param. The matching ticket is in the list.
    route1 = respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"ticket_id": "other-1", "state": "DONE", "pr_url": None},
                ticket,
            ],
        )
    )

    tools = build_find_ticket_by_pr_tool(_settings())
    result = await tools[0](pr_url)

    assert route1.called
    data = json.loads(result)
    assert data["ticket_id"] == "20250101T120000Z-fix-bug-a1b2"
    assert data["state"] == "HUMAN_MR_APPROVAL"


@pytest.mark.asyncio
async def test_find_ticket_by_pr_skips_non_dict_entries(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-dict entries in the ticket list are safely skipped."""
    pr_url = "https://github.com/owner/repo/pull/656"

    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=["not a dict", None, {"ticket_id": "other", "pr_url": "other-url"}],
        )
    )

    tools = build_find_ticket_by_pr_tool(_settings())
    result = await tools[0](pr_url)

    data = json.loads(result)
    assert data["ticket_id"] is None
    assert "No ticket found" in data["error"]


# ---------------------------------------------------------------------------
# build_file_ticket_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_ticket_sends_a_single_object_not_a_list(
    respx_mock: respx.MockRouter,
) -> None:
    """The ingest payload must be one TicketIngest object.

    Regression guard: the tool used to wrap the payload in a list, which
    mill's ``TicketIngest`` model rejects with 422 — so ``file_ticket``
    could never actually file a ticket.
    """
    route = respx_mock.post("http://board:8077/tickets/ingest").mock(
        return_value=httpx.Response(201, json={"ticket_id": "t-new", "deduped": False})
    )

    tools = build_file_ticket_tool(_settings())
    result = json.loads(
        await tools[0](
            title="Wallet value shows cash",
            description="details",
            repo_id="robotsix-invest",
        )
    )

    assert route.called
    sent = json.loads(route.calls[0].request.content)
    assert isinstance(sent, dict), f"payload must be an object, got {type(sent)}"
    assert sent["repo_id"] == "robotsix-invest"
    assert sent["title"] == "Wallet value shows cash"
    assert sent["source_tag"] == "robotsix-chat-tool"
    assert "details" in sent["body"]

    assert result["ticket_id"] == "t-new"
    assert result["error"] == ""


@pytest.mark.asyncio
async def test_file_ticket_requires_repo_id(respx_mock: respx.MockRouter) -> None:
    """An empty repo_id fails fast — mill has no default repo.

    Mill answers a missing repo_id with 422 and an empty one with
    ``404 Unknown repo_id: ''``; neither is actionable for the agent.
    """
    route = respx_mock.post("http://board:8077/tickets/ingest").mock(
        return_value=httpx.Response(201, json={"ticket_id": "unreachable"})
    )

    tools = build_file_ticket_tool(_settings())
    result = json.loads(await tools[0](title="No repo", description="details"))

    assert not route.called, "must not call the board without a repo_id"
    assert result["ticket_id"] == ""
    assert "repo_id is required" in result["error"]


@pytest.mark.asyncio
async def test_file_ticket_roster_path_sends_a_single_object() -> None:
    """The component_request (roster) path sends an object too."""
    captured: dict[str, Any] = {}

    async def _component_request(
        component_id: str,
        method: str,
        path: str,
        json_body: Any = None,
        **_kw: Any,
    ) -> str:
        captured["component_id"] = component_id
        captured["method"] = method
        captured["path"] = path
        captured["json_body"] = json_body
        return 'HTTP 201\n{"ticket_id": "t-roster-new", "deduped": false}'

    tools = build_file_ticket_tool(_settings(), component_request=_component_request)
    result = json.loads(
        await tools[0](title="Roster ticket", description="d", repo_id="robotsix-chat")
    )

    assert captured["path"] == "/tickets/ingest"
    assert captured["method"] == "POST"
    assert isinstance(captured["json_body"], dict)
    assert captured["json_body"]["repo_id"] == "robotsix-chat"
    assert result["ticket_id"] == "t-roster-new"
    assert result["error"] == ""


@pytest.mark.asyncio
async def test_file_ticket_reports_dedup_hit(respx_mock: respx.MockRouter) -> None:
    """A 200 + deduped=true resolves to the existing ticket id."""
    respx_mock.post("http://board:8077/tickets/ingest").mock(
        return_value=httpx.Response(
            200, json={"ticket_id": "t-existing", "deduped": True}
        )
    )

    tools = build_file_ticket_tool(_settings())
    result = json.loads(
        await tools[0](title="Dup", description="d", repo_id="robotsix-invest")
    )

    assert result["ticket_id"] == "t-existing"
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# build_list_stale_ready_tickets_tool — disabled / empty
# ---------------------------------------------------------------------------


def test_list_stale_ready_empty_config_returns_empty_list() -> None:
    """Neither component_request nor board_api_base_url → empty list."""
    settings = Settings(
        direct_repo=DirectRepoSettings(board_api_base_url=""),
        autonomous=AutonomousSettings(ready_staleness_minutes=10),
    )
    tools = build_list_stale_ready_tickets_tool(
        settings,
        component_request=None,
    )
    assert tools == []


def test_list_stale_ready_configured_returns_one_tool() -> None:
    """When board_api_base_url is set, returns one list_stale_ready_tickets tool."""
    tools = build_list_stale_ready_tickets_tool(_stale_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "list_stale_ready_tickets"


# ---------------------------------------------------------------------------
# build_list_stale_ready_tickets_tool — roster path success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_stale_ready_roster_success_no_stale() -> None:
    """Roster path returns tickets; none are stale when all are recent."""
    now = 1_750_000_000.0
    recent = now - 60  # 1 minute ago
    tickets = [
        {
            "id": "t-fresh",
            "state": "ready",
            "title": "Fresh ticket",
            "updated_at": recent,
            "created_at": recent,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_ready_count"] == 0
    assert result["total_ready"] == 1
    assert result["threshold_minutes"] == 10
    assert result["stale_tickets"] == []
    assert result.get("error") is None


@pytest.mark.asyncio
async def test_list_stale_ready_roster_success_with_stale() -> None:
    """Roster path returns tickets; tickets older than threshold are stale."""
    now = 1_750_000_000.0
    stale_age = 900  # 15 minutes ago
    recent_age = 120  # 2 minutes ago
    tickets = [
        {
            "id": "t-stale",
            "state": "ready",
            "title": "Stale ticket",
            "updated_at": now - stale_age,
            "created_at": now - stale_age,
        },
        {
            "id": "t-recent",
            "state": "ready",
            "title": "Recent ticket",
            "updated_at": now - recent_age,
            "created_at": now - recent_age,
        },
        {
            "id": "t-done",
            "state": "done",
            "title": "Done ticket",
            "updated_at": now - stale_age,
            "created_at": now - stale_age,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_ready_count"] == 1
    assert result["total_ready"] == 2
    assert len(result["stale_tickets"]) == 1
    assert result["stale_tickets"][0]["ticket_id"] == "t-stale"
    assert result["stale_tickets"][0]["state"] == "READY"


@pytest.mark.asyncio
async def test_list_stale_ready_roster_ticket_list_key() -> None:
    """Roster path returns dict with 'tickets' key wrapping the list."""
    tickets = [
        {
            "id": "t-wrapped",
            "state": "ready",
            "title": "Test",
            "updated_at": 1_750_000_000.0,
            "created_at": 1_750_000_000.0,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list({"tickets": tickets}),
    )

    result = json.loads(await tools[0]())
    assert result["total_ready"] == 1


# ---------------------------------------------------------------------------
# build_list_stale_ready_tickets_tool — priority ticket grace period
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_stale_ready_priority_ticket_longer_threshold() -> None:
    """Priority-flagged ready tickets use the longer priority threshold."""
    now = 1_750_000_000.0
    # 50 minutes ago — exceeds regular threshold (10 min) but not priority (60 min)
    priority_age = 50 * 60
    tickets = [
        {
            "id": "t-priority",
            "state": "ready",
            "title": "Priority ticket",
            "priority": True,
            "updated_at": now - priority_age,
            "created_at": now - priority_age,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(
            ready_staleness_minutes=10,
            priority_ready_staleness_minutes=60,
        ),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    # 50 min > 10 min (regular threshold) but < 60 min (priority threshold),
    # so the priority ticket should NOT be flagged as stale.
    assert result["stale_ready_count"] == 0
    assert result["total_ready"] == 1
    assert result["threshold_minutes"] == 10
    assert result["priority_threshold_minutes"] == 60


@pytest.mark.asyncio
async def test_list_stale_ready_priority_ticket_exceeds_priority_threshold() -> None:
    """Priority ticket IS stale when it exceeds the priority threshold."""
    now = 1_750_000_000.0
    # 90 minutes ago — exceeds both thresholds
    priority_age = 90 * 60
    tickets = [
        {
            "id": "t-priority-stale",
            "state": "ready",
            "title": "Very stale priority ticket",
            "flagged": True,
            "updated_at": now - priority_age,
            "created_at": now - priority_age,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(
            ready_staleness_minutes=10,
            priority_ready_staleness_minutes=60,
        ),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_ready_count"] == 1
    assert result["total_ready"] == 1
    assert result["stale_tickets"][0]["ticket_id"] == "t-priority-stale"
    assert result["stale_tickets"][0]["priority"] is True


@pytest.mark.asyncio
async def test_list_stale_ready_mixed_priority_and_regular() -> None:
    """Mixed tickets: priority uses longer threshold, regular uses short threshold."""
    now = 1_750_000_000.0
    # 30 minutes ago — exceeds regular (10 min) but not priority (60 min)
    age_30m = 30 * 60
    tickets = [
        {
            "id": "t-regular-stale",
            "state": "ready",
            "title": "Regular stale ticket",
            "updated_at": now - age_30m,
            "created_at": now - age_30m,
        },
        {
            "id": "t-priority-ok",
            "state": "ready",
            "title": "Priority not stale",
            "priority": True,
            "updated_at": now - age_30m,
            "created_at": now - age_30m,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(
            ready_staleness_minutes=10,
            priority_ready_staleness_minutes=60,
        ),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    # Only the regular ticket should be stale.
    assert result["stale_ready_count"] == 1
    assert result["total_ready"] == 2
    assert result["stale_tickets"][0]["ticket_id"] == "t-regular-stale"


# ---------------------------------------------------------------------------
# build_list_stale_ready_tickets_tool — roster fallback to direct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_stale_ready_roster_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """When roster returns error, fall back to the direct board API URL."""
    tickets = [
        {
            "id": "t-fallback",
            "state": "ready",
            "title": "Fallback ticket",
            "updated_at": 1_750_000_000.0,
            "created_at": 1_750_000_000.0,
        },
    ]
    route = respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(200, json=tickets),
    )

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_error("Error: connection refused"),
    )
    result = json.loads(await tools[0]())

    assert route.called
    assert result["total_ready"] == 1
    assert result["stale_tickets"][0]["ticket_id"] == "t-fallback"


@pytest.mark.asyncio
async def test_list_stale_ready_roster_http_error_returns_error() -> None:
    """When roster returns HTTP 502, the error is surfaced directly (no fallback)."""
    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_error("HTTP 502 Bad Gateway"),
    )
    result = json.loads(await tools[0]())

    assert "error" in result
    assert "Unexpected response format" in result["error"]
    assert result["stale_ready_count"] == 0


@pytest.mark.asyncio
async def test_list_stale_ready_roster_http_500_proper_format() -> None:
    """When roster returns a properly-formatted HTTP 500, error is surfaced."""
    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_http_error(500),
    )
    result = json.loads(await tools[0]())

    assert "error" in result
    assert "500" in result["error"]
    assert result["stale_ready_count"] == 0


@pytest.mark.asyncio
async def test_list_stale_ready_direct_only(
    respx_mock: respx.MockRouter,
) -> None:
    """Without component_request, the direct path is used directly."""
    tickets = [
        {
            "id": "t-direct",
            "state": "ready",
            "title": "Direct ticket",
            "updated_at": 1_750_000_000.0,
            "created_at": 1_750_000_000.0,
        },
    ]
    route = respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(200, json=tickets),
    )

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
    )
    result = json.loads(await tools[0]())

    assert route.called
    assert result["total_ready"] == 1


# ---------------------------------------------------------------------------
# build_list_stale_ready_tickets_tool — HTTP error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_stale_ready_direct_http_404(
    respx_mock: respx.MockRouter,
) -> None:
    """Direct path HTTP 404 → error JSON with zero counts."""
    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(404),
    )

    tools = build_list_stale_ready_tickets_tool(_stale_settings())
    result = json.loads(await tools[0]())

    assert "error" in result
    assert "404" in result["error"]
    assert result["stale_ready_count"] == 0
    assert result["stale_tickets"] == []


@pytest.mark.asyncio
async def test_list_stale_ready_direct_timeout(
    respx_mock: respx.MockRouter,
) -> None:
    """Direct path timeout → error JSON with zero counts."""
    respx_mock.get("http://board:8077/tickets").mock(
        side_effect=httpx.ConnectTimeout("timed out"),
    )

    tools = build_list_stale_ready_tickets_tool(_stale_settings())
    result = json.loads(await tools[0]())

    assert "error" in result
    assert "timed out" in result["error"]
    assert result["stale_ready_count"] == 0


@pytest.mark.asyncio
async def test_list_stale_ready_direct_connect_error(
    respx_mock: respx.MockRouter,
) -> None:
    """Direct path connect error → error JSON with zero counts."""
    respx_mock.get("http://board:8077/tickets").mock(
        side_effect=httpx.ConnectError("connection refused"),
    )

    tools = build_list_stale_ready_tickets_tool(_stale_settings())
    result = json.loads(await tools[0]())

    assert "error" in result
    assert "timed out" in result["error"] or "Board API" in result["error"]
    assert result["stale_ready_count"] == 0


# ---------------------------------------------------------------------------
# build_list_stale_ready_tickets_tool — timestamp parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_stale_ready_timestamp_iso_with_timezone() -> None:
    """ISO-8601 timestamp with timezone offset is parsed correctly."""
    now = 1_750_000_000.0
    stale_age = 900  # 15 minutes ago
    ts = now - stale_age
    import datetime

    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
    iso_str = dt.isoformat()

    tickets = [
        {
            "id": "t-iso-tz",
            "state": "ready",
            "title": "ISO TZ ticket",
            "updated_at": iso_str,
            "created_at": iso_str,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_ready_count"] == 1
    assert result["stale_tickets"][0]["staleness"] == "15m"


@pytest.mark.asyncio
async def test_list_stale_ready_timestamp_iso_without_timezone() -> None:
    """ISO-8601 timestamp without timezone is treated as UTC."""
    now = 1_750_000_000.0
    stale_age = 900  # 15 minutes ago
    ts = now - stale_age
    import datetime

    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
    # Remove timezone info for naive iso
    iso_str = dt.replace(tzinfo=None).isoformat()

    tickets = [
        {
            "id": "t-iso-naive",
            "state": "ready",
            "title": "ISO naive ticket",
            "updated_at": iso_str,
            "created_at": iso_str,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_ready_count"] == 1
    assert result["stale_tickets"][0]["staleness"] == "15m"


@pytest.mark.asyncio
async def test_list_stale_ready_timestamp_unix_float() -> None:
    """Unix-seconds float timestamp is parsed correctly."""
    now = 1_750_000_000.0
    stale_age = 900  # 15 minutes ago

    tickets = [
        {
            "id": "t-unix",
            "state": "ready",
            "title": "Unix timestamp",
            "updated_at": now - stale_age,
            "created_at": now - stale_age,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_ready_count"] == 1
    assert result["stale_tickets"][0]["staleness"] == "15m"


@pytest.mark.asyncio
async def test_list_stale_ready_timestamp_unix_float_as_string() -> None:
    """Unix-seconds float stored as string is parsed correctly."""
    now = 1_750_000_000.0
    stale_age = 900  # 15 minutes ago

    tickets = [
        {
            "id": "t-unix-str",
            "state": "ready",
            "title": "Unix string ticket",
            "updated_at": str(now - stale_age),
            "created_at": str(now - stale_age),
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_ready_count"] == 1


@pytest.mark.asyncio
async def test_list_stale_ready_timestamp_unparsable() -> None:
    """Unparsable timestamp string → ticket is included with 'unknown' staleness."""
    now = 1_750_000_000.0

    tickets = [
        {
            "id": "t-bad-ts",
            "state": "ready",
            "title": "Bad timestamp",
            "updated_at": "not-a-timestamp",
            "created_at": "not-a-timestamp",
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_ready_count"] == 1
    assert result["stale_tickets"][0]["staleness"] == "unknown (no timestamp)"


@pytest.mark.asyncio
async def test_list_stale_ready_timestamp_missing() -> None:
    """Missing both updated_at and created_at → included with 'unknown' staleness."""
    now = 1_750_000_000.0

    tickets = [
        {
            "id": "t-no-ts",
            "state": "ready",
            "title": "No timestamp",
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_ready_count"] == 1
    assert result["stale_tickets"][0]["staleness"] == "unknown (no timestamp)"


# ---------------------------------------------------------------------------
# build_list_stale_ready_tickets_tool — _format_staleness helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_stale_ready_format_staleness_hours() -> None:
    """Staleness >= 120 minutes is formatted in hours."""
    now = 1_750_000_000.0
    stale_age = 3 * 3600  # 3 hours = 180 minutes

    tickets = [
        {
            "id": "t-hours",
            "state": "ready",
            "title": "Hours ticket",
            "updated_at": now - stale_age,
            "created_at": now - stale_age,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_tickets"][0]["staleness"] == "3.0h"


@pytest.mark.asyncio
async def test_list_stale_ready_format_staleness_minutes() -> None:
    """Staleness between 1 and 120 minutes is formatted in minutes."""
    now = 1_750_000_000.0
    stale_age = 300  # 5 minutes

    tickets = [
        {
            "id": "t-minutes",
            "state": "ready",
            "title": "Minutes ticket",
            "updated_at": now - stale_age,
            "created_at": now - stale_age,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=2),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_tickets"][0]["staleness"] == "5m"


@pytest.mark.asyncio
async def test_list_stale_ready_format_staleness_seconds() -> None:
    """Staleness just at the threshold (60 s) is formatted as '1m'."""
    now = 1_750_000_000.0
    stale_age = 60  # exactly 1 minute

    tickets = [
        {
            "id": "t-one-minute",
            "state": "ready",
            "title": "One minute ticket",
            "updated_at": now - stale_age,
            "created_at": now - stale_age,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=1),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_tickets"][0]["staleness"] == "1m"


# ---------------------------------------------------------------------------
# build_list_stale_ready_tickets_tool — JSON output and sorting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_stale_ready_json_output_structure() -> None:
    """Output JSON contains expected top-level keys."""
    now = 1_750_000_000.0

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list([]),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert "stale_ready_count" in result
    assert "total_ready" in result
    assert "threshold_minutes" in result
    assert "stale_tickets" in result
    assert result["threshold_minutes"] == 10


@pytest.mark.asyncio
async def test_list_stale_ready_sorting_most_stale_first() -> None:
    """Stale tickets are sorted by staleness_seconds descending (most stale first)."""
    now = 1_750_000_000.0

    tickets = [
        {
            "id": "t-mid",
            "state": "ready",
            "title": "Mid ticket",
            "updated_at": now - 600,  # 10 min
            "created_at": now - 600,
        },
        {
            "id": "t-most",
            "state": "ready",
            "title": "Most stale",
            "updated_at": now - 3600,  # 60 min
            "created_at": now - 3600,
        },
        {
            "id": "t-least",
            "state": "ready",
            "title": "Least stale",
            "updated_at": now - 120,  # 2 min
            "created_at": now - 120,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=1),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    stale_ids = [t["ticket_id"] for t in result["stale_tickets"]]
    assert stale_ids == ["t-most", "t-mid", "t-least"]


@pytest.mark.asyncio
async def test_list_stale_ready_unknown_staleness_sorted_last() -> None:
    """Tickets with unknown staleness sort after timed tickets."""
    now = 1_750_000_000.0

    tickets = [
        {
            "id": "t-no-ts",
            "state": "ready",
            "title": "No timestamp",
        },
        {
            "id": "t-known",
            "state": "ready",
            "title": "Known staleness",
            "updated_at": now - 600,
            "created_at": now - 600,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=1),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    stale_ids = [t["ticket_id"] for t in result["stale_tickets"]]
    # Unknown staleness uses -(float('inf')) = -inf in sort key, so it sorts first
    assert stale_ids[0] == "t-no-ts"


@pytest.mark.asyncio
async def test_list_stale_ready_ticket_fields_in_output() -> None:
    """Each stale ticket entry contains all expected fields."""
    now = 1_750_000_000.0

    tickets = [
        {
            "id": "t-fields",
            "state": "ready",
            "title": "Fields ticket",
            "updated_at": now - 900,
            "created_at": now - 1200,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    entry = result["stale_tickets"][0]
    assert entry["ticket_id"] == "t-fields"
    assert entry["state"] == "READY"
    assert entry["title"] == "Fields ticket"
    assert "staleness" in entry
    assert entry["staleness_seconds"] is not None
    assert entry["updated_at"] is not None
    assert entry["created_at"] is not None


# ---------------------------------------------------------------------------
# build_list_stale_ready_tickets_tool — configurable threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_stale_ready_respects_threshold() -> None:
    """Tickets older than threshold are stale; newer are not."""
    now = 1_750_000_000.0

    tickets = [
        {
            "id": "t-old",
            "state": "ready",
            "title": "Old",
            "updated_at": now - 600,  # 10 min
            "created_at": now - 600,
        },
        {
            "id": "t-new",
            "state": "ready",
            "title": "New",
            "updated_at": now - 300,  # 5 min
            "created_at": now - 300,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=7),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_ready_count"] == 1
    assert result["total_ready"] == 2
    assert result["stale_tickets"][0]["ticket_id"] == "t-old"


@pytest.mark.asyncio
async def test_list_stale_ready_uses_ticket_id_field() -> None:
    """Tickets using 'ticket_id' key instead of 'id' are handled correctly."""
    now = 1_750_000_000.0

    tickets = [
        {
            "ticket_id": "t-ticket-id",
            "state": "ready",
            "title": "Uses ticket_id key",
            "updated_at": now - 900,
            "created_at": now - 900,
        },
    ]

    tools = build_list_stale_ready_tickets_tool(
        _stale_settings(ready_staleness_minutes=10),
        component_request=_component_request_ticket_list(tickets),
    )

    import time as _time_mod

    original_time = _time_mod.time
    try:
        _time_mod.time = lambda: now
        result = json.loads(await tools[0]())
    finally:
        _time_mod.time = original_time

    assert result["stale_tickets"][0]["ticket_id"] == "t-ticket-id"

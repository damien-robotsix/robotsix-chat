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

from robotsix_chat.config import DirectRepoSettings, Settings
from robotsix_chat.ticket_poll import (
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

    async def _req(component: str, method: str, path: str) -> str:
        return "HTTP 200 OK\n" + json.dumps({"state": state, "ticket_id": ticket_id})

    return _req


def _component_request_error(
    error_msg: str = "Error: connection refused",
) -> Callable[..., Any]:
    """Return an async mock that returns a roster-style error."""

    async def _req(component: str, method: str, path: str) -> str:
        return error_msg

    return _req


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
    assert result["error"] == "Board API request failed"


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
    assert result["error"] == "Board API request failed"


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
    assert result["error"] == "Board API request failed"


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
    assert result["error"] == "Board API request failed"


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
    assert result["error"] == "Board API request failed"


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
    assert result["error"] == "Board API request failed"


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
    assert bad["error"] == "Board API request failed"


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
    assert slow["error"] == "Board API request failed"


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
    assert ticket["error"] == "Board API request failed"


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
    assert tickets[0]["error"] == "Board API request failed"


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
    assert tickets[0]["error"] == "Board API request failed"


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

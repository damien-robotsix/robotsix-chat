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
from tests.ticket_poll._ticket_poll_helpers import (
    _69BE_HISTORY,
    _69BE_ID,
    _69BE_PR,
    _component_request_error,
    _component_request_success,
    _settings,
)


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


def test_configured_board_url_returns_two_tools() -> None:
    """When board_api_base_url is set, returns ticket_poll and ticket_poll_batch."""
    tools = build_ticket_poll_tools(_settings())
    assert len(tools) == 2
    assert tools[0].__name__ == "ticket_poll"
    assert tools[1].__name__ == "ticket_poll_batch"


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


async def test_ticket_poll_strips_trailing_slash(respx_mock: respx.MockRouter) -> None:
    """Trailing slash on board_api_base_url is stripped."""
    route = respx_mock.get("http://board:8077/tickets/test-slash").mock(
        return_value=httpx.Response(200, json={"state": "OPEN"})
    )

    tools = build_ticket_poll_tools(_settings(board_api_base_url="http://board:8077/"))
    result = json.loads(await tools[0]("test-slash"))

    assert route.called
    assert result["state"] == "OPEN"


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


async def test_ticket_poll_resolves_against_mill_id_field(
    respx_mock: respx.MockRouter,
) -> None:
    """Mill's GET /tickets keys tickets ``id``, not ``ticket_id``.

    Regression: extracting only ``ticket_id`` made every resolution fail
    with "(0 tickets listed)" against the live mill board (2026-09-01).
    """
    real_id = "20260731T020731Z-batch-approval-should-resolve-ids-32be"

    respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": real_id, "status": "fixing_ci"},
                {"id": "20260730T232905Z-other-ticket-761f", "status": "done"},
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
    result = json.loads(await tools[0]("...-resolve-ids-32be"))

    assert route.called
    assert result
    assert result["ticket_id"] == real_id
    assert result["state"] == "BLOCKED"
    assert result["error"] == ""


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


async def test_ticket_poll_batch_empty_list(
    respx_mock: respx.MockRouter,
) -> None:
    """Empty ticket_ids list returns empty tickets array."""
    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    result = json.loads(await batch_tool([]))

    assert result == {"tickets": []}


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


async def test_ticket_poll_direct_closed_with_pr_reports_delivered(
    respx_mock: respx.MockRouter,
) -> None:
    """Direct path: closed + pr_url + real history → delivered, no flag."""
    respx_mock.get(f"http://board:8077/tickets/{_69BE_ID}").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"id": _69BE_ID, "state": "closed", "pr_url": _69BE_PR}),
        )
    )
    history_route = respx_mock.get(
        f"http://board:8077/tickets/{_69BE_ID}/history"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                [{"state": row["state"], "note": None} for row in _69BE_HISTORY]
            ),
        )
    )
    tools = build_ticket_poll_tools(_settings())
    ticket_poll = tools[0]
    result = json.loads(await ticket_poll(_69BE_ID))
    assert history_route.called
    assert result["state"] == "closed"
    assert result["pr_url"] == _69BE_PR
    assert result["delivered"] is True
    assert result["delivery_note"] == (
        f"closed after delivery (retrospect): PR {_69BE_PR}"
    )
    assert result["unexpected_terminal"] is None
    assert result["error"] == ""


async def test_ticket_poll_direct_draft_to_closed_flags_dropped(
    respx_mock: respx.MockRouter,
) -> None:
    """Direct path: closed with no PR and a draft-only history → flagged."""
    respx_mock.get("http://board:8077/tickets/t-dropped").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-dropped", "state": "closed", "pr_url": None})
        )
    )
    respx_mock.get("http://board:8077/tickets/t-dropped/history").mock(
        return_value=httpx.Response(
            200, text=json.dumps([{"state": "draft"}, {"state": "closed"}])
        )
    )
    tools = build_ticket_poll_tools(_settings())
    result = json.loads(await tools[0]("t-dropped"))
    assert result["delivered"] is False
    assert result["pr_url"] is None
    assert result["unexpected_terminal"] is not None
    assert "draft → closed" in result["unexpected_terminal"]


async def test_ticket_poll_direct_active_state_skips_history_fetch(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-terminal tickets do not pay the history round-trip."""
    respx_mock.get("http://board:8077/tickets/t-active").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-active", "state": "code_review"})
        )
    )
    history_route = respx_mock.get("http://board:8077/tickets/t-active/history").mock(
        return_value=httpx.Response(200, text="[]")
    )
    tools = build_ticket_poll_tools(_settings())
    result = json.loads(await tools[0]("t-active"))
    assert not history_route.called
    assert result["state"] == "code_review"
    assert result["delivered"] is False
    assert result["unexpected_terminal"] is None


async def test_ticket_poll_component_path_fetches_history_via_roster() -> None:
    """Roster path: history comes from component_request GET /tickets/{id}/history."""
    calls: list[str] = []

    async def _req(component: str, method: str, path: str, **kwargs: Any) -> str:
        calls.append(path)
        if path.endswith("/history"):
            return "HTTP 200 OK\n" + json.dumps(
                [{"state": row["state"]} for row in _69BE_HISTORY]
            )
        if path == "/tickets":
            return "HTTP 200 OK\n" + json.dumps([{"ticket_id": _69BE_ID}])
        return "HTTP 200 OK\n" + json.dumps(
            {"ticket_id": _69BE_ID, "state": "closed", "pr_url": _69BE_PR}
        )

    tools = build_ticket_poll_tools(_settings(), component_request=_req)
    result = json.loads(await tools[0](_69BE_ID))
    assert f"/tickets/{_69BE_ID}/history" in calls
    assert result["delivered"] is True
    assert result["pr_url"] == _69BE_PR
    assert result["unexpected_terminal"] is None


async def test_ticket_poll_batch_resolves_closed_ticket_via_fallback(
    respx_mock: respx.MockRouter,
) -> None:
    """An abbreviated id of a CLOSED ticket resolves via the closed fallback.

    Mill's default ``GET /tickets`` hides closed tickets, so a shipped
    ticket's hash suffix never matched and chat re-filed shipped work
    (2026-09-07, a9bc dup of c64a).  The resolver must retry once against
    ``?include_closed=true&updated_after=…`` and report the closed state.
    """
    closed_id = "20260907T064354Z-bump-llmio-pin-a9bc"
    open_id = "20260907T072712Z-worker-stop-duplicate-stage-runs-0629"

    def _list(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("include_closed") == "true":
            assert request.url.params.get("updated_after", "").endswith("Z")
            return httpx.Response(
                200,
                json=[
                    {"id": open_id, "state": "implement"},
                    {"id": closed_id, "state": "closed"},
                ],
            )
        return httpx.Response(200, json=[{"id": open_id, "state": "implement"}])

    list_route = respx_mock.get("http://board:8077/tickets").mock(side_effect=_list)
    detail = respx_mock.get(f"http://board:8077/tickets/{closed_id}").mock(
        return_value=httpx.Response(200, json={"state": "closed"})
    )

    tools = build_ticket_poll_tools(_settings())
    batch_tool = tools[1]
    result = json.loads(await batch_tool(["a9bc"]))

    assert list_route.call_count == 2
    assert detail.called
    assert result["tickets"][0]["ticket_id"] == closed_id
    assert result["tickets"][0]["state"] == "closed"


async def test_ticket_poll_batch_no_closed_fallback_when_all_resolve(
    respx_mock: respx.MockRouter,
) -> None:
    """The (slow) closed listing is only fetched when something is unresolved."""
    open_id = "20260907T072712Z-worker-stop-duplicate-stage-runs-0629"
    list_route = respx_mock.get("http://board:8077/tickets").mock(
        return_value=httpx.Response(200, json=[{"id": open_id, "state": "ready"}])
    )
    respx_mock.get(f"http://board:8077/tickets/{open_id}").mock(
        return_value=httpx.Response(200, json={"state": "ready"})
    )

    tools = build_ticket_poll_tools(_settings())
    await tools[1](["0629"])

    assert list_route.call_count == 1

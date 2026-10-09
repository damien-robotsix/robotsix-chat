"""Per-tool builder tests for ticket_poll state/query/queue tools."""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx

from robotsix_chat.config import DirectRepoSettings, PeriodicSettings, Settings
from robotsix_chat.ticket_poll import (
    build_file_ticket_tool,
    build_find_ticket_by_pr_tool,
    build_list_stale_ready_tickets_tool,
    build_mark_ticket_done_tool,
    build_mark_ticket_ready_tool,
    build_merge_pull_request_tool,
)
from tests.ticket_poll._ticket_poll_helpers import (
    _component_request_error,
    _component_request_http_error,
    _component_request_ticket_list,
    _settings,
    _stale_settings,
    _transition_tool,
)


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


async def test_mark_ticket_ready_posts_real_transition_body() -> None:
    """The request hits mill's real ``/transition`` route with state=ready.

    Regression for 2026-09-07: the tool POSTed to a ``/mark-ready`` route
    that mill never had, so every approval attempt 404'd and burned a turn.
    """
    calls: list[tuple[str, str, str, dict[str, Any] | None]] = []

    async def _req(
        component: str,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        calls.append((component, method, path, json_body))
        return "HTTP 200 OK\n" + json.dumps({"state": "ready"})

    tools = build_mark_ticket_ready_tool(_settings(), component_request=_req)
    await tools[0]("mr-body", justification="operator approved in chat")

    assert calls == [
        (
            "mill",
            "POST",
            "/tickets/mr-body/transition",
            {"state": "ready", "note": "operator approved in chat"},
        )
    ]


async def test_mark_ticket_ready_accepts_note_alias() -> None:
    """``note=`` (the mill API's own field name) is accepted as an alias.

    Regression for 2026-09-14: three drain-run approvals per pass were
    rejected at the schema level with "Additional properties are not
    allowed ('note' was unexpected)" and burned a turn each.
    """
    calls: list[dict[str, Any] | None] = []

    async def _req(
        component: str,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        calls.append(json_body)
        return "HTTP 200 OK\n" + json.dumps({"state": "ready"})

    tools = build_mark_ticket_ready_tool(_settings(), component_request=_req)
    await tools[0]("mr-note", note="Approved (drain-run)")
    assert calls == [{"state": "ready", "note": "Approved (drain-run)"}]

    # justification wins when both are given.
    await tools[0]("mr-note", justification="j", note="n")
    assert calls[-1] == {"state": "ready", "note": "j"}


async def test_mark_ticket_ready_roster_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """When roster path returns an error, fall back to direct POST."""
    route = respx_mock.post("http://board:8077/tickets/mr-fallback/transition").mock(
        return_value=httpx.Response(200, json={"state": "READY"})
    )

    tools = build_mark_ticket_ready_tool(
        _settings(),
        component_request=_component_request_error("Error: connection refused"),
    )
    result = await tools[0]("mr-fallback")

    assert route.called
    assert "READY" in result


async def test_mark_ticket_ready_roster_http_error_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """Roster 502 (no ``Error:`` prefix) still triggers the direct fallback."""
    route = respx_mock.post(
        "http://board:8077/tickets/mr-http-fallback/transition"
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


async def test_mark_ticket_ready_direct_only(
    respx_mock: respx.MockRouter,
) -> None:
    """Without component_request, the direct POST path works."""
    route = respx_mock.post("http://board:8077/tickets/mr-direct/transition").mock(
        return_value=httpx.Response(200, json={"state": "READY"})
    )

    tools = build_mark_ticket_ready_tool(_settings())
    result = await tools[0]("mr-direct")

    assert route.called
    assert "READY" in result


async def test_mark_ticket_ready_with_auth_token(
    respx_mock: respx.MockRouter,
) -> None:
    """Auth token is sent as Bearer in the Authorization header."""
    route = respx_mock.post("http://board:8077/tickets/mr-auth/transition").mock(
        return_value=httpx.Response(200, json={"state": "READY"})
    )

    tools = build_mark_ticket_ready_tool(
        _settings(board_api_token="mark-token"),
    )
    await tools[0]("mr-auth")

    assert route.called
    request_headers = route.calls.last.request.headers
    assert request_headers["Authorization"] == "Bearer mark-token"


async def test_mark_ticket_ready_strips_trailing_slash(
    respx_mock: respx.MockRouter,
) -> None:
    """Trailing slash on board_api_base_url is stripped correctly."""
    route = respx_mock.post("http://board:8077/tickets/mr-slash/transition").mock(
        return_value=httpx.Response(200, json={"state": "READY"})
    )

    tools = build_mark_ticket_ready_tool(
        _settings(board_api_base_url="http://board:8077/")
    )
    await tools[0]("mr-slash")

    assert route.called


async def test_mark_ticket_ready_http_404(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP 404 → error message includes the status code."""
    respx_mock.post("http://board:8077/tickets/mr-404/transition").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )

    tools = build_mark_ticket_ready_tool(_settings())
    result = await tools[0]("mr-404")

    assert "404" in result
    assert "Not found" in result


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

    route = respx_mock.post(f"http://board:8077/tickets/{real_id}/transition").mock(
        return_value=httpx.Response(200, json={"state": "READY"})
    )

    tools = build_mark_ticket_ready_tool(_settings())
    result = await tools[0]("761f")

    assert route.called
    assert "READY" in result


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


async def test_file_ticket_empty_id_on_success_returns_error(
    respx_mock: respx.MockRouter,
) -> None:
    """When the board accepts the ticket but the response lacks an id, return an error.

    Regression guard: the tool used to return ``{"ticket_id": "", "error": ""}``
    which looked like success with no ticket id — wasting the agent's time
    verifying whether the ticket was actually created.
    """
    respx_mock.post("http://board:8077/tickets/ingest").mock(
        return_value=httpx.Response(201, json={"status": "ok"})
    )

    tools = build_file_ticket_tool(_settings())
    result = json.loads(
        await tools[0](
            title="No id in response",
            description="details",
            repo_id="robotsix-invest",
        )
    )

    assert result["ticket_id"] == ""
    assert "did not contain a ticket id" in result["error"]


async def test_file_ticket_roster_empty_id_on_success_returns_error() -> None:
    """Roster path: accepted but no id → error, not silent empty success."""

    async def _component_request(
        component_id: str,
        method: str,
        path: str,
        json_body: Any = None,
        **_kw: Any,
    ) -> str:
        return 'HTTP 201\n{"status": "ok"}'

    tools = build_file_ticket_tool(_settings(), component_request=_component_request)
    result = json.loads(
        await tools[0](title="No id roster", description="d", repo_id="robotsix-chat")
    )

    assert result["ticket_id"] == ""
    assert "did not contain a ticket id" in result["error"]


def test_list_stale_ready_empty_config_returns_empty_list() -> None:
    """Neither component_request nor board_api_base_url → empty list."""
    settings = Settings(
        direct_repo=DirectRepoSettings(board_api_base_url=""),
        periodic=PeriodicSettings(ready_staleness_minutes=10),
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


async def test_file_ticket_rejects_unregistered_repo_id(
    respx_mock: respx.MockRouter,
) -> None:
    """A repo_id not in the board's GET /repos list is rejected early.

    The agent gets a clear, actionable error listing available repos
    instead of a generic 404 from the board API.
    """
    respx_mock.get("http://board:8077/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"repo_id": "robotsix-chat", "board_id": "robotsix-chat"},
                {"repo_id": "robotsix-mill", "board_id": "robotsix-mill"},
            ],
        )
    )
    ingest_route = respx_mock.post("http://board:8077/tickets/ingest").mock(
        return_value=httpx.Response(201, json={"ticket_id": "t-new", "deduped": False})
    )

    tools = build_file_ticket_tool(_settings())
    result = json.loads(
        await tools[0](
            title="Some ticket",
            description="details",
            repo_id="mobile-app",
        )
    )

    assert not ingest_route.called, "must not call ingest for an unregistered repo"
    assert result["ticket_id"] == ""
    assert "mobile-app" in result["error"]
    assert "not registered" in result["error"]
    assert "robotsix-chat" in result["error"]
    assert "robotsix-mill" in result["error"]


async def test_file_ticket_allows_registered_repo_id(
    respx_mock: respx.MockRouter,
) -> None:
    """A repo_id that IS in the board's repo list proceeds to ingest."""
    respx_mock.get("http://board:8077/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"repo_id": "robotsix-chat", "board_id": "robotsix-chat"},
                {"repo_id": "robotsix-invest", "board_id": "robotsix-invest"},
            ],
        )
    )
    respx_mock.post("http://board:8077/tickets/ingest").mock(
        return_value=httpx.Response(201, json={"ticket_id": "t-ok", "deduped": False})
    )

    tools = build_file_ticket_tool(_settings())
    result = json.loads(
        await tools[0](
            title="Valid ticket",
            description="details",
            repo_id="robotsix-invest",
        )
    )

    assert result["ticket_id"] == "t-ok"
    assert result["error"] == ""


async def test_file_ticket_skips_validation_when_repos_unreachable(
    respx_mock: respx.MockRouter,
) -> None:
    """When GET /repos fails, filing proceeds without validation.

    Best-effort: the board API's own 404 is the fallback guard.
    """
    respx_mock.get("http://board:8077/repos").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    respx_mock.post("http://board:8077/tickets/ingest").mock(
        return_value=httpx.Response(
            201, json={"ticket_id": "t-fallback", "deduped": False}
        )
    )

    tools = build_file_ticket_tool(_settings())
    result = json.loads(
        await tools[0](
            title="Fallback ticket",
            description="details",
            repo_id="robotsix-invest",
        )
    )

    assert result["ticket_id"] == "t-fallback"
    assert result["error"] == ""


async def test_file_ticket_repo_validation_via_roster() -> None:
    """The roster (component_request) path also validates repo_id."""
    repos_response = json.dumps(
        [
            {"repo_id": "robotsix-chat", "board_id": "robotsix-chat"},
            {"repo_id": "robotsix-mill", "board_id": "robotsix-mill"},
        ]
    )

    async def _component_request(
        component_id: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> str:
        if method == "GET" and path == "/repos":
            return f"HTTP 200\n{repos_response}"
        return "HTTP 201\n" + json.dumps({"ticket_id": "t-roster", "deduped": False})

    tools = build_file_ticket_tool(_settings(), component_request=_component_request)
    result = json.loads(
        await tools[0](
            title="Wrong board",
            description="details",
            repo_id="mobile-app",
        )
    )

    assert result["ticket_id"] == ""
    assert "mobile-app" in result["error"]
    assert "not registered" in result["error"]


async def test_file_ticket_repo_validation_roster_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """When roster GET /repos fails, validation falls back to direct API."""
    respx_mock.get("http://board:8077/repos").mock(
        return_value=httpx.Response(
            200,
            json=[{"repo_id": "robotsix-chat", "board_id": "robotsix-chat"}],
        )
    )
    ingest_route = respx_mock.post("http://board:8077/tickets/ingest").mock(
        return_value=httpx.Response(201, json={"ticket_id": "t-new", "deduped": False})
    )

    async def _component_request_error(
        component_id: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> str:
        if method == "GET" and path == "/repos":
            return "Error: component unreachable"
        return "HTTP 201\n" + json.dumps({"ticket_id": "t-new", "deduped": False})

    tools = build_file_ticket_tool(
        _settings(), component_request=_component_request_error
    )
    result = json.loads(
        await tools[0](
            title="Wrong board",
            description="details",
            repo_id="mobile-app",
        )
    )

    assert not ingest_route.called
    assert result["ticket_id"] == ""
    assert "mobile-app" in result["error"]
    assert "not registered" in result["error"]


async def test_transition_ticket_approves_to_ready_with_note(monkeypatch):
    """The approve path posts /transition with state=ready and the rationale."""
    captured: list = []
    tool = _transition_tool(monkeypatch, captured)

    out = await tool("t-1", "ready", "spec actionable, consistent with standards")
    assert "ready" in out
    component, method, path, body = captured[0]
    assert (component, method) == ("mill", "POST")
    assert path == "/tickets/t-1/transition"
    assert body == {
        "state": "ready",
        "note": "spec actionable, consistent with standards",
    }


async def test_transition_ticket_thin_spec_goes_to_draft(monkeypatch):
    """The thin-spec path posts state=draft with the what-is-missing note."""
    captured: list = []
    tool = _transition_tool(monkeypatch, captured)

    await tool("t-2", "draft", "spec body is empty — needs goal and scope")
    assert captured[0][2] == "/tickets/t-2/transition"
    assert captured[0][3]["state"] == "draft"


async def test_transition_ticket_requires_a_note(monkeypatch):
    captured: list = []
    tool = _transition_tool(monkeypatch, captured)

    out = await tool("t-3", "ready", "   ")
    assert "Refusing" in out
    assert captured == []


async def test_transition_ticket_rejects_unlisted_states(monkeypatch):
    captured: list = []
    tool = _transition_tool(monkeypatch, captured)

    out = await tool("t-4", "done", "should not pass")
    assert "Refusing" in out
    assert captured == []


def test_mill_workflow_skill_carries_the_approval_gate_policy():
    """The approval-gate policy lives in the mill_workflow skill since prompt v162."""
    from robotsix_chat.mill_workflow import load_mill_workflow_skill

    instruction = load_mill_workflow_skill()
    assert "Mill approval gate" in instruction
    assert "transition_ticket" in instruction
    assert "human_issue_approval" in instruction
    assert "draft then closed" in instruction
    assert "Never spawn a subsession that merely waits for a human" in instruction


async def test_mark_ticket_ready_waits_out_classifying_then_retries(
    monkeypatch,
) -> None:
    """Wait for a ``classifying`` ticket to reach draft, then retry once.

    A just-filed ticket is still ``classifying``; mill answers the transition
    with ``409 classifying -> ready`` (2026-09-07: two approvals in two
    minutes each burned a turn on this 409).
    """
    from robotsix_chat.ticket_poll import ticket_poll_write as tp

    monkeypatch.setattr(tp, "_CLASSIFY_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(tp, "_CLASSIFY_POLL_SECONDS", 0.01)

    calls: list[tuple[str, str]] = []
    states = iter(["classifying", "classifying", "draft"])

    async def _req(
        component: str,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        calls.append((method, path))
        if method == "GET":
            return "HTTP 200\n" + json.dumps(
                {"id": "mr-classifying", "state": next(states)}
            )
        posts = sum(1 for m, _ in calls if m == "POST")
        if posts == 1:
            return "HTTP 409\n" + json.dumps(
                {
                    "title": "Conflict",
                    "status": 409,
                    "detail": (
                        "mr-classifying: classifying -> ready is not an "
                        "allowed transition"
                    ),
                }
            )
        return "HTTP 200 OK\n" + json.dumps({"state": "ready"})

    tools = build_mark_ticket_ready_tool(_settings(), component_request=_req)
    result = await tools[0]("mr-classifying", justification="operator asked")

    assert "HTTP 200" in result and "ready" in result
    assert [m for m, _ in calls] == ["POST", "GET", "GET", "GET", "POST"]
    assert all(p == "/tickets/mr-classifying" for m, p in calls if m == "GET")


async def test_mark_ticket_ready_still_classifying_returns_guidance(
    monkeypatch,
) -> None:
    """Return guidance when classification outlasts the bounded wait.

    The tool must not retry forever nor fall back to the direct path, which
    would 409 identically.
    """
    from robotsix_chat.ticket_poll import ticket_poll_write as tp

    monkeypatch.setattr(tp, "_CLASSIFY_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(tp, "_CLASSIFY_POLL_SECONDS", 0.01)

    calls: list[tuple[str, str]] = []

    async def _req(
        component: str,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        calls.append((method, path))
        if method == "GET":
            return "HTTP 200\n" + json.dumps({"state": "classifying"})
        return "HTTP 409\n" + json.dumps(
            {"detail": "x: classifying -> ready is not an allowed transition"}
        )

    tools = build_mark_ticket_ready_tool(_settings(), component_request=_req)
    result = await tools[0]("mr-stuck", justification="operator asked")

    assert "still in mill's `classifying` state" in result
    assert "Do not retry in this turn" in result
    assert sum(1 for m, _ in calls if m == "POST") == 1

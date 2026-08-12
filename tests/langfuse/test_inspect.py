"""Tests for the Langfuse trace-inspection tool.

:func:`build_langfuse_inspect_tools` with ``respx`` mocked so there are
no real network calls.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import LangfuseInspectSettings, LangfuseSettings
from robotsix_chat.langfuse import (
    build_langfuse_inspect_tools,
    load_langfuse_inspect_skill,
)


def _inspect_settings(**kw: Any) -> LangfuseInspectSettings:
    base: dict[str, Any] = {"enabled": True, "max_traces": 5}
    base.update(kw)
    return LangfuseInspectSettings(**base)


def _langfuse_settings(**kw: Any) -> LangfuseSettings:
    """Canonical block carrying this component's MAIN project credentials."""
    from robotsix_chat.config import PROJECT_MAIN

    base: dict[str, Any] = {
        "host": "https://cloud.langfuse.com",
        "projects": {
            PROJECT_MAIN: {
                "public_key": "pk-test",
                "secret_key": "sk-test",  # pragma: allowlist secret
            }
        },
    }
    base.update(kw)
    return LangfuseSettings(**base)


# ---------------------------------------------------------------------------
# build_langfuse_inspect_tools
# ---------------------------------------------------------------------------


def test_build_langfuse_inspect_tools_disabled() -> None:
    """Disabled langfuse_inspect returns no tools."""
    assert (
        build_langfuse_inspect_tools(
            LangfuseInspectSettings(enabled=False), _langfuse_settings()
        )
        == []
    )


def test_build_langfuse_inspect_tools_returns_one_tool() -> None:
    """Enabled langfuse_inspect returns exactly one tool."""
    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "inspect_langfuse_trace"


# ---------------------------------------------------------------------------
# load_langfuse_inspect_skill
# ---------------------------------------------------------------------------


def test_load_langfuse_inspect_skill_returns_non_empty_markdown() -> None:
    """The shipped skill.md is loadable and describes the tool."""
    skill = load_langfuse_inspect_skill()
    assert len(skill) > 100
    assert "inspect_langfuse_trace" in skill
    assert "read-only" in skill.lower()


# ---------------------------------------------------------------------------
# inspect_langfuse_trace — error paths (no network needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_both_ids_provided() -> None:
    """Providing both trace_id and ticket_id returns an error."""
    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0](trace_id="abc", ticket_id="xyz"))
    assert result["error"]
    assert "not both" in result["error"].lower()
    assert result["traces"] == []


@pytest.mark.asyncio
async def test_inspect_neither_id_provided() -> None:
    """Providing neither trace_id nor ticket_id returns an error."""
    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0]())
    assert result["error"]
    assert "either" in result["error"].lower()
    assert result["traces"] == []


@pytest.mark.asyncio
async def test_inspect_no_credentials() -> None:
    """When public/secret key are empty, tool returns a config error."""
    tools = build_langfuse_inspect_tools(
        _inspect_settings(),
        _langfuse_settings(projects={}),
    )
    result = json.loads(await tools[0](trace_id="abc"))
    assert result["error"]
    assert "credentials" in result["error"].lower()
    assert result["traces"] == []


# ---------------------------------------------------------------------------
# inspect_langfuse_trace — trace_id success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_by_trace_id_success(respx_mock: respx.MockRouter) -> None:
    """Fetching a single trace by id returns a summary."""
    trace_id = "01JM3XABCDEFGHIJKLMN"
    respx_mock.get(f"https://cloud.langfuse.com/api/public/traces/{trace_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": trace_id,
                "name": "implement",
                "timestamp": "2026-07-27T00:15:00.000Z",
                "userId": "agent-1",
                "latency": 12.5,
                "totalCost": 0.042,
                "metrics": {
                    "usage": {
                        "promptTokens": 1500,
                        "completionTokens": 800,
                        "totalTokens": 2300,
                    },
                },
                "observations": [
                    {"id": "obs-1", "type": "GENERATION"},
                    {"id": "obs-2", "type": "SPAN"},
                ],
                "scores": [
                    {"name": "success", "value": 1.0},
                ],
            },
        )
    )

    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0](trace_id=trace_id))

    assert len(result["traces"]) == 1
    t = result["traces"][0]
    assert t["id"] == trace_id
    assert t["name"] == "implement"
    assert t["userId"] == "agent-1"
    assert t["latency"] == 12.5
    assert t["totalCost"] == 0.042
    assert t["usage"]["promptTokens"] == 1500
    assert t["usage"]["completionTokens"] == 800
    assert t["usage"]["totalTokens"] == 2300
    assert t["observations"] == 2
    assert t["scores"] == [{"name": "success", "value": 1.0}]


# ---------------------------------------------------------------------------
# inspect_langfuse_trace — ticket_id search success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_by_ticket_id_success(respx_mock: respx.MockRouter) -> None:
    """Searching by ticket_id returns a list of summaries."""
    ticket_id = "20260727T001240Z-test-5bd6"

    respx_mock.get("https://cloud.langfuse.com/api/public/traces").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "trace-1",
                        "name": "implement",
                        "timestamp": "2026-07-27T00:16:00.000Z",
                        "userId": "agent-1",
                        "latency": 10.0,
                        "totalCost": 0.03,
                        "metrics": {
                            "usage": {
                                "promptTokens": 1000,
                                "completionTokens": 500,
                                "totalTokens": 1500,
                            }
                        },
                        "observations": [{"id": "o1"}],
                        "scores": [],
                    },
                    {
                        "id": "trace-2",
                        "name": "refine",
                        "timestamp": "2026-07-27T00:14:00.000Z",
                        "userId": "agent-1",
                        "latency": 8.0,
                        "totalCost": 0.02,
                        "metrics": {
                            "usage": {
                                "promptTokens": 800,
                                "completionTokens": 400,
                                "totalTokens": 1200,
                            }
                        },
                        "observations": [],
                        "scores": [{"name": "quality", "value": 0.9}],
                    },
                ],
                "meta": {"page": 1, "limit": 5, "totalItems": 2},
            },
        )
    )

    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0](ticket_id=ticket_id, limit=5))

    assert result["ticket_id"] == ticket_id
    assert result["limit"] == 5
    assert len(result["traces"]) == 2
    assert result["traces"][0]["id"] == "trace-1"
    assert result["traces"][0]["name"] == "implement"
    assert result["traces"][1]["id"] == "trace-2"
    assert result["traces"][1]["name"] == "refine"

    # Verify the tag filter was sent (query param order may vary).
    req = respx_mock.calls.last.request
    qs = req.url.query.decode()
    assert "limit=5" in qs
    assert "orderBy=timestamp.desc" in qs
    assert "tags=ticket_id%3A20260727T001240Z-test-5bd6" in qs


# ---------------------------------------------------------------------------
# inspect_langfuse_trace — limit clamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_limit_clamped_to_max(respx_mock: respx.MockRouter) -> None:
    """A limit higher than max_traces is clamped down."""
    respx_mock.get("https://cloud.langfuse.com/api/public/traces").mock(
        return_value=httpx.Response(200, json={"data": [], "meta": {}})
    )

    tools = build_langfuse_inspect_tools(
        _inspect_settings(max_traces=3), _langfuse_settings()
    )
    await tools[0](ticket_id="test", limit=20)

    req = respx_mock.calls.last.request
    assert b"limit=3" in req.url.query


@pytest.mark.asyncio
async def test_inspect_limit_minimum_one(respx_mock: respx.MockRouter) -> None:
    """A limit of zero or negative is clamped to 1."""
    respx_mock.get("https://cloud.langfuse.com/api/public/traces").mock(
        return_value=httpx.Response(200, json={"data": [], "meta": {}})
    )

    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    await tools[0](ticket_id="test", limit=0)

    req = respx_mock.calls.last.request
    assert b"limit=1" in req.url.query


# ---------------------------------------------------------------------------
# inspect_langfuse_trace — HTTP error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_trace_id_http_401(respx_mock: respx.MockRouter) -> None:
    """A 401 from Langfuse returns an error in the result."""
    trace_id = "01JM3XNOAUTH"
    respx_mock.get(f"https://cloud.langfuse.com/api/public/traces/{trace_id}").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0](trace_id=trace_id))

    assert result["error"]
    assert "401" in result["error"]
    assert result["traces"] == []


@pytest.mark.asyncio
async def test_inspect_trace_id_timeout(respx_mock: respx.MockRouter) -> None:
    """A timeout returns an error in the result."""
    trace_id = "01JM3XTIMEOUT"
    respx_mock.get(f"https://cloud.langfuse.com/api/public/traces/{trace_id}").mock(
        side_effect=httpx.TimeoutException("timed out")
    )

    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0](trace_id=trace_id))

    assert result["error"]
    assert "timed out" in result["error"].lower()
    assert result["traces"] == []


@pytest.mark.asyncio
async def test_inspect_ticket_id_http_500(respx_mock: respx.MockRouter) -> None:
    """A 500 from Langfuse returns an error in the result."""
    respx_mock.get("https://cloud.langfuse.com/api/public/traces").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0](ticket_id="test"))

    assert result["error"]
    assert "500" in result["error"]
    assert result["traces"] == []


# ---------------------------------------------------------------------------
# inspect_langfuse_trace — retry behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_timeout_recovers(respx_mock: respx.MockRouter) -> None:
    """A transient timeout is retried; second attempt succeeds."""
    trace_id = "01JM3XRETRY1"
    route = respx_mock.get(
        f"https://cloud.langfuse.com/api/public/traces/{trace_id}"
    ).mock(
        side_effect=[
            httpx.TimeoutException("timed out"),
            httpx.Response(
                200,
                json={
                    "id": trace_id,
                    "name": "implement",
                    "timestamp": "2026-07-27T00:15:00.000Z",
                    "userId": "agent-1",
                    "latency": 12.5,
                    "totalCost": 0.042,
                    "metrics": {
                        "usage": {
                            "promptTokens": 0,
                            "completionTokens": 0,
                            "totalTokens": 0,
                        }
                    },
                    "observations": [],
                    "scores": [],
                },
            ),
        ]
    )

    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0](trace_id=trace_id))

    assert len(result["traces"]) == 1
    assert result["traces"][0]["id"] == trace_id
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_retry_500_recovers(respx_mock: respx.MockRouter) -> None:
    """A 500 error is retried; second attempt succeeds."""
    trace_id = "01JM3XRETRY2"
    route = respx_mock.get(
        f"https://cloud.langfuse.com/api/public/traces/{trace_id}"
    ).mock(
        side_effect=[
            httpx.Response(502, text="Bad Gateway"),
            httpx.Response(
                200,
                json={
                    "id": trace_id,
                    "name": "implement",
                    "timestamp": "2026-07-27T00:15:00.000Z",
                    "userId": "agent-1",
                    "latency": 12.5,
                    "totalCost": 0.042,
                    "metrics": {
                        "usage": {
                            "promptTokens": 0,
                            "completionTokens": 0,
                            "totalTokens": 0,
                        }
                    },
                    "observations": [],
                    "scores": [],
                },
            ),
        ]
    )

    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0](trace_id=trace_id))

    assert len(result["traces"]) == 1
    assert result["traces"][0]["id"] == trace_id
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_no_retry_on_401(respx_mock: respx.MockRouter) -> None:
    """A 401 (auth error) is NOT retried — it's not transient."""
    trace_id = "01JM3XNORETRY"
    route = respx_mock.get(
        f"https://cloud.langfuse.com/api/public/traces/{trace_id}"
    ).mock(return_value=httpx.Response(401, text="Unauthorized"))

    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0](trace_id=trace_id))

    assert result["error"]
    assert "401" in result["error"]
    assert route.call_count == 1  # no retry — returned immediately


@pytest.mark.asyncio
async def test_retry_exhausted_returns_error(respx_mock: respx.MockRouter) -> None:
    """When all retries are exhausted, the last error is returned."""
    trace_id = "01JM3XEXHAUST"
    route = respx_mock.get(
        f"https://cloud.langfuse.com/api/public/traces/{trace_id}"
    ).mock(
        side_effect=[
            httpx.TimeoutException("timed out"),
            httpx.TimeoutException("timed out again"),
            httpx.TimeoutException("timed out a third time"),
        ]
    )

    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0](trace_id=trace_id))

    assert result["error"]
    assert "timed out" in result["error"].lower()
    assert result["traces"] == []
    # 3 total attempts: initial + 2 retries (_MAX_RETRIES = 2)
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_retry_ticket_id_search_timeout_recovers(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket-id search: timeout on first attempt, recovers on second."""
    route = respx_mock.get("https://cloud.langfuse.com/api/public/traces").mock(
        side_effect=[
            httpx.TimeoutException("timed out"),
            httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "trace-1",
                            "name": "implement",
                            "timestamp": "2026-07-27T00:16:00.000Z",
                            "userId": "agent-1",
                            "latency": 10.0,
                            "totalCost": 0.03,
                            "metrics": {
                                "usage": {
                                    "promptTokens": 1000,
                                    "completionTokens": 500,
                                    "totalTokens": 1500,
                                }
                            },
                            "observations": [],
                            "scores": [],
                        }
                    ]
                },
            ),
        ]
    )

    tools = build_langfuse_inspect_tools(_inspect_settings(), _langfuse_settings())
    result = json.loads(await tools[0](ticket_id="test"))

    assert len(result["traces"]) == 1
    assert result["traces"][0]["id"] == "trace-1"
    assert route.call_count == 2

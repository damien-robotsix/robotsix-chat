"""Unit tests for :mod:`robotsix_chat.epic`."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from robotsix_http import RetryConfig

import robotsix_chat.epic as epic_mod
from robotsix_chat.config import DirectRepoSettings, Settings
from robotsix_chat.epic import (
    _board_connection,
    _summarize_events,
    build_decompose_epic_tool,
    load_epic_skill,
)
from robotsix_chat.repo.direct.board_client import BoardClient

BOARD_URL = "http://board.test:8077"


def _settings(board_url: str = BOARD_URL, token: str = "") -> Settings:
    return Settings(
        direct_repo=DirectRepoSettings(
            board_api_base_url=board_url,
            board_api_token=SecretStr(token),
        )
    )


def _patch_httpx(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Route ``httpx.AsyncClient`` in the epic module through a mock transport."""
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def _recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_recording)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return seen


def _patch_fetch(
    monkeypatch: pytest.MonkeyPatch,
    data: dict[str, Any] | None,
    reason: str | None = None,
) -> None:
    async def _fake_fetch(
        self: BoardClient,
        ticket_id: str,
        label_suffix: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        return data, reason

    monkeypatch.setattr(BoardClient, "_fetch_ticket", _fake_fetch)


def _patch_cycles(monkeypatch: pytest.MonkeyPatch, cycles: int) -> None:
    import robotsix_chat.repo.direct.client as client_mod

    monkeypatch.setattr(client_mod, "_count_cycles_from_data", lambda _d: cycles)


def _roster(response: str) -> Callable[..., Any]:
    calls: list[tuple[Any, ...]] = []

    async def _request(*args: Any) -> str:
        calls.append(args)
        return response

    _request.calls = calls  # type: ignore[attr-defined]
    return _request


# ---------------------------------------------------------------------------
# load_epic_skill
# ---------------------------------------------------------------------------


def test_load_epic_skill_returns_content() -> None:
    assert load_epic_skill().strip()


def test_load_epic_skill_missing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: Any, **_k: Any) -> str:
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", _raise)
    assert load_epic_skill() == ""


# ---------------------------------------------------------------------------
# _board_connection / build_decompose_epic_tool
# ---------------------------------------------------------------------------


def test_board_connection_none_without_url_or_roster() -> None:
    assert _board_connection(_settings(board_url="  "), None) is None


def test_board_connection_strips_trailing_slash() -> None:
    conn = _board_connection(_settings(board_url=f"{BOARD_URL}/", token="t"), None)
    assert conn is not None
    url, token, timeout = conn
    assert url == BOARD_URL
    assert token == "t"
    assert timeout == _settings().direct_repo.timeout


def test_board_connection_roster_only() -> None:
    conn = _board_connection(_settings(board_url=""), _roster(""))
    assert conn is not None
    assert conn[0] == ""


def test_build_tool_returns_empty_without_connection() -> None:
    assert build_decompose_epic_tool(_settings(board_url="")) == []


def test_build_tool_returns_decompose_epic() -> None:
    tools = build_decompose_epic_tool(_settings())
    assert [t.__name__ for t in tools] == ["decompose_epic"]


# ---------------------------------------------------------------------------
# _summarize_events
# ---------------------------------------------------------------------------


def test_summarize_events_keeps_last_20_and_skips_non_dicts() -> None:
    events: list[Any] = [{"type": f"e{i}"} for i in range(25)]
    events.append("not-a-dict")
    out = _summarize_events(events)
    assert len(out) == 19
    assert out[0]["type"] == "e6"
    assert out[-1]["type"] == "e24"


def test_summarize_events_state_branch() -> None:
    out = _summarize_events([{"type": "transition", "timestamp": "t1", "state": "x"}])
    assert out == [{"type": "transition", "timestamp": "t1", "detail": "state=x"}]


def test_summarize_events_from_to_state_with_reason() -> None:
    out = _summarize_events(
        [
            {
                "action": "move",
                "created_at": "t2",
                "from_state": "a",
                "to_state": "b",
                "reason": "  spawn exhausted  ",
            }
        ]
    )
    assert out == [
        {"type": "move", "timestamp": "t2", "detail": "a → b; reason=spawn exhausted"}
    ]


def test_summarize_events_message_only_and_coercion() -> None:
    out = _summarize_events(
        [{"event": 7, "ts": 123, "message": "", "error": "x" * 200, "note": "n"}]
    )
    assert out[0]["type"] == "7"
    assert out[0]["timestamp"] == "123"
    # Empty message is skipped; first non-empty key wins and is truncated.
    assert out[0]["detail"] == "x" * 120


def test_summarize_events_defaults() -> None:
    assert _summarize_events([{}]) == [{"type": "", "timestamp": "", "detail": ""}]


# ---------------------------------------------------------------------------
# decompose_epic
# ---------------------------------------------------------------------------


_EPIC = {
    "title": "Big epic",
    "state": "blocked",
    "kind": "epic",
    "description": "Do A then B",
    "events": [{"type": "implement", "timestamp": "t"}],
}


async def _run(
    settings: Settings,
    component_request: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    (tool,) = build_decompose_epic_tool(settings, component_request=component_request)
    result: dict[str, Any] = json.loads(await tool("EPIC-1"))
    return result


@pytest.mark.asyncio
async def test_decompose_epic_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch(monkeypatch, None, "ticket not found (404)")
    result = await _run(_settings())
    assert result == {"epic_ticket_id": "EPIC-1", "error": "ticket not found (404)"}


@pytest.mark.asyncio
async def test_decompose_epic_fetch_error_default_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, None, None)
    result = await _run(_settings())
    assert result["error"] == "Failed to fetch epic ticket data"


@pytest.mark.asyncio
async def test_decompose_epic_spawn_exhausted_builds_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, dict(_EPIC))
    _patch_cycles(monkeypatch, 3)
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, json=[]))
    result = await _run(_settings())
    assert result["error"] == ""
    assert result["epic_title"] == "Big epic"
    assert result["epic_state"] == "blocked"
    assert result["implement_cycles"] == 3
    assert result["spawn_exhausted"] is True
    assert result["existing_children"] == []
    assert len(result["decomposition_plan"]) == 2
    assert result["decomposition_plan"][0]["kind"] == "task"
    assert result["epic_events_summary"] == [
        {"type": "implement", "timestamp": "t", "detail": ""}
    ]


@pytest.mark.asyncio
async def test_decompose_epic_not_exhausted_has_no_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, dict(_EPIC))
    _patch_cycles(monkeypatch, 2)
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, json=[]))
    result = await _run(_settings())
    assert result["spawn_exhausted"] is False
    assert result["decomposition_plan"] == []


@pytest.mark.asyncio
async def test_decompose_epic_empty_description_has_no_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, {**_EPIC, "description": "", "events": "bogus"})
    _patch_cycles(monkeypatch, 5)
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, json=[]))
    result = await _run(_settings())
    assert result["decomposition_plan"] == []
    assert result["epic_events_summary"] == []


@pytest.mark.asyncio
async def test_decompose_epic_children_suppress_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, {**_EPIC, "kind": "task"})
    _patch_cycles(monkeypatch, 3)
    children = [
        {"ticket_id": "C1", "title": "child one", "state": "done"},
        {"id": "C2"},
        "junk",
    ]
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, json=children))
    result = await _run(_settings())
    assert result["epic_kind"] == "task"
    assert result["existing_children"] == [
        {"id": "C1", "title": "child one", "state": "done"},
        {"id": "C2", "title": "(untitled)", "state": "unknown"},
    ]
    assert result["decomposition_plan"] == []


# ---------------------------------------------------------------------------
# _list_epic_children — direct board path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_children_sends_auth_and_parses_tickets_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, dict(_EPIC))
    _patch_cycles(monkeypatch, 0)
    seen = _patch_httpx(
        monkeypatch,
        lambda _r: httpx.Response(200, json={"tickets": [{"id": "C9"}]}),
    )
    result = await _run(_settings(token="tok"))  # pragma: allowlist secret
    assert [c["id"] for c in result["existing_children"]] == ["C9"]
    assert str(seen[0].url) == f"{BOARD_URL}/tickets/EPIC-1/children"
    assert seen[0].headers["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_direct_children_no_token_no_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, dict(_EPIC))
    _patch_cycles(monkeypatch, 0)
    seen = _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, json={"x": 1}))
    result = await _run(_settings())
    assert result["existing_children"] == []
    assert result["existing_children_error"] == ""
    assert "Authorization" not in seen[0].headers


@pytest.mark.asyncio
async def test_direct_children_unexpected_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, dict(_EPIC))
    _patch_cycles(monkeypatch, 0)
    _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, json="string"))
    result = await _run(_settings())
    assert result["existing_children"] == []
    assert result["existing_children_error"] == ""


@pytest.mark.asyncio
async def test_direct_children_exception_sets_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, dict(_EPIC))
    _patch_cycles(monkeypatch, 3)

    def _boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    _patch_httpx(monkeypatch, _boom)
    monkeypatch.setattr(
        epic_mod,
        "_EPIC_RETRY_CONFIG",
        RetryConfig(max_retries=0, backoff_base=0.01, backoff_cap=0.01),
    )
    result = await _run(_settings())
    assert result["existing_children"] == []
    assert result["existing_children_error"].startswith("Failed to list epic children:")
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# _list_epic_children — roster path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ('HTTP 200\n[{"id": "R1"}]', ["R1"]),
        ('HTTP 200\n{"tickets": [{"id": "R2"}]}', ["R2"]),
    ],
)
async def test_roster_children_success(
    monkeypatch: pytest.MonkeyPatch, response: str, expected: list[str]
) -> None:
    _patch_fetch(monkeypatch, dict(_EPIC))
    _patch_cycles(monkeypatch, 0)
    seen = _patch_httpx(monkeypatch, lambda _r: httpx.Response(200, json=[]))
    roster = _roster(response)
    result = await _run(_settings(), roster)
    assert [c["id"] for c in result["existing_children"]] == expected
    assert roster.calls == [("mill", "GET", "/tickets/EPIC-1/children")]  # type: ignore[attr-defined]
    assert seen == []  # no direct fallback


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "Error: mill unreachable",
        "HTTP 404\nnot found",
        "HTTP 200\nnot json",
        "no newline at all",
        "HTTP\n[]",
        "HTTP abc\n[]",
        "STATUS 200\n[]",
        'HTTP 200\n{"tickets": "nope"}',
        'HTTP 200\n"scalar"',
    ],
)
async def test_roster_children_falls_back_to_direct(
    monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    _patch_fetch(monkeypatch, dict(_EPIC))
    _patch_cycles(monkeypatch, 0)
    seen = _patch_httpx(
        monkeypatch, lambda _r: httpx.Response(200, json=[{"id": "D1"}])
    )
    result = await _run(_settings(), _roster(response))
    assert [c["id"] for c in result["existing_children"]] == ["D1"]
    assert len(seen) == 1

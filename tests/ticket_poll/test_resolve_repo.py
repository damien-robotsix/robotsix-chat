"""Tests for the ``resolve_repo`` tool (mill ``repo_id`` → GitHub ``owner/repo``)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings, Settings
from robotsix_chat.ticket_poll import build_resolve_repo_tool

_REGISTRY = [
    {
        "repo_id": "robotsix-central-deploy",
        "board_id": "robotsix-central-deploy",
        "forge_remote_url": (
            "https://github.com/damien-robotsix/robotsix-central-deploy.git"
        ),
    },
    {
        "repo_id": "robotsix-chat",
        "board_id": "chat",
        "forge_remote_url": "https://github.com/damien-robotsix/robotsix-chat",
    },
    {"repo_id": "meta", "board_id": "meta", "forge_remote_url": None},
]


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "board_api_base_url": "http://board:8077",
        "board_api_token": "",
        "timeout": 10.0,
    }
    base.update(kw)
    return Settings(direct_repo=DirectRepoSettings(**base))


def test_no_board_no_tool() -> None:
    """Without a board URL or component_request the factory returns nothing."""
    assert build_resolve_repo_tool(_settings(board_api_base_url="")) == []


@pytest.mark.asyncio
async def test_resolve_repo_direct_parses_owner_repo_from_git_url(
    respx_mock: respx.MockRouter,
) -> None:
    """The 69be repo id maps to damien-robotsix/robotsix-central-deploy."""
    respx_mock.get("http://board:8077/repos").mock(
        return_value=httpx.Response(200, text=json.dumps(_REGISTRY))
    )
    (resolve_repo,) = build_resolve_repo_tool(_settings())
    out = json.loads(await resolve_repo("robotsix-central-deploy"))
    assert out["error"] == ""
    assert out["full_name"] == "damien-robotsix/robotsix-central-deploy"
    assert out["owner"] == "damien-robotsix"
    assert out["repo"] == "robotsix-central-deploy"
    assert out["forge_remote_url"].endswith("robotsix-central-deploy.git")


@pytest.mark.asyncio
async def test_resolve_repo_matches_board_id_and_is_case_insensitive(
    respx_mock: respx.MockRouter,
) -> None:
    """A board_id alias and odd casing still resolve."""
    respx_mock.get("http://board:8077/repos").mock(
        return_value=httpx.Response(200, text=json.dumps(_REGISTRY))
    )
    (resolve_repo,) = build_resolve_repo_tool(_settings())
    out = json.loads(await resolve_repo("CHAT"))
    assert out["full_name"] == "damien-robotsix/robotsix-chat"


@pytest.mark.asyncio
async def test_resolve_repo_unknown_id_lists_known_ids_never_guesses(
    respx_mock: respx.MockRouter,
) -> None:
    """An unknown id yields full_name=null plus the registered ids — never a guess."""
    respx_mock.get("http://board:8077/repos").mock(
        return_value=httpx.Response(200, text=json.dumps(_REGISTRY))
    )
    (resolve_repo,) = build_resolve_repo_tool(_settings())
    out = json.loads(await resolve_repo("central-deploy"))
    assert out["full_name"] is None
    assert "not a registered mill repo id" in out["error"]
    assert out["known_repo_ids"] == ["meta", "robotsix-central-deploy", "robotsix-chat"]
    assert "robotsix/central-deploy" not in json.dumps(out)


@pytest.mark.asyncio
async def test_resolve_repo_passes_through_full_name() -> None:
    """An explicit owner/repo is returned unchanged without touching the board."""
    (resolve_repo,) = build_resolve_repo_tool(_settings())
    out = json.loads(await resolve_repo("damien-robotsix/robotsix-mill"))
    assert out["full_name"] == "damien-robotsix/robotsix-mill"
    assert out["owner"] == "damien-robotsix"


@pytest.mark.asyncio
async def test_resolve_repo_registry_unreachable(respx_mock: respx.MockRouter) -> None:
    """A board outage is reported, not turned into a guess."""
    respx_mock.get("http://board:8077/repos").mock(
        return_value=httpx.Response(503, text="down")
    )
    (resolve_repo,) = build_resolve_repo_tool(_settings())
    out = json.loads(await resolve_repo("robotsix-chat"))
    assert out["full_name"] is None
    assert "unreachable" in out["error"]


@pytest.mark.asyncio
async def test_resolve_repo_prefers_component_request() -> None:
    """The roster path (component_request GET /repos) is used when available."""
    calls: list[tuple[str, str, str]] = []

    async def _req(component: str, method: str, path: str, **kwargs: Any) -> str:
        calls.append((component, method, path))
        return "HTTP 200 OK\n" + json.dumps(_REGISTRY)

    (resolve_repo,) = build_resolve_repo_tool(_settings(), component_request=_req)
    out = json.loads(await resolve_repo("robotsix-central-deploy"))
    assert calls == [("mill", "GET", "/repos")]
    assert out["full_name"] == "damien-robotsix/robotsix-central-deploy"


@pytest.mark.asyncio
async def test_resolve_repo_component_failure_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """A roster error falls back to the direct board URL."""
    respx_mock.get("http://board:8077/repos").mock(
        return_value=httpx.Response(200, text=json.dumps(_REGISTRY))
    )

    async def _req(component: str, method: str, path: str, **kwargs: Any) -> str:
        return "Error: connection refused"

    (resolve_repo,) = build_resolve_repo_tool(_settings(), component_request=_req)
    out = json.loads(await resolve_repo("robotsix-chat"))
    assert out["full_name"] == "damien-robotsix/robotsix-chat"


@pytest.mark.asyncio
async def test_resolve_repo_accepts_query_alias(
    respx_mock: respx.MockRouter,
) -> None:
    """``query`` is accepted as an alias for ``repo_id`` (agents guess it)."""
    respx_mock.get("http://board:8077/repos").mock(
        return_value=httpx.Response(200, text=json.dumps(_REGISTRY))
    )
    (resolve_repo,) = build_resolve_repo_tool(_settings())
    out = json.loads(await resolve_repo(query="robotsix-chat"))
    assert out["error"] == ""
    assert out["full_name"] == "damien-robotsix/robotsix-chat"


@pytest.mark.asyncio
async def test_resolve_repo_empty_args_is_a_soft_error() -> None:
    """No repo at all returns a JSON error string, not a validation crash."""
    (resolve_repo,) = build_resolve_repo_tool(_settings())
    out = json.loads(await resolve_repo())
    assert out["full_name"] is None
    assert "repo_id" in out["error"]

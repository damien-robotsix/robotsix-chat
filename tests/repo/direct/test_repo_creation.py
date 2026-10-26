"""Tests for PAT-authenticated repo creation / forking (client + tools)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.direct.client import DirectRepoClient, is_valid_repo_name
from tests.repo.direct.conftest import _settings

_PAT = "ghp_test_personal_token"  # pragma: allowlist secret
_APP_TOKEN = "ghs_test_installation_token"  # pragma: allowlist secret
_API = "https://api.github.com"


def _pat_settings(**kw: Any) -> Any:
    return _settings(github_pat=_PAT, **kw)


def _tool(tools: list[Callable[..., Any]], name: str) -> Callable[..., Any]:
    return next(t for t in tools if t.__name__ == name)


def _assert_pat_auth(request: httpx.Request) -> None:
    auth = request.headers["Authorization"]
    assert auth == f"Bearer {_PAT}"
    assert _APP_TOKEN not in auth


def _mock_user(respx_mock: respx.MockRouter, login: str = "damien-robotsix") -> Any:
    return respx_mock.get(f"{_API}/user").mock(
        return_value=httpx.Response(200, text=json.dumps({"login": login}))
    )


# ---------------------------------------------------------------------------
# name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["repo", "my-repo_1.x", "A" * 100])
def test_valid_repo_names(name: str) -> None:
    assert is_valid_repo_name(name)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a b", "../x", "A" * 101])
def test_invalid_repo_names(name: str) -> None:
    assert not is_valid_repo_name(name)


# ---------------------------------------------------------------------------
# client — create_repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_repo_uses_pat_not_app_token(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(f"{_API}/user/repos").mock(
        return_value=httpx.Response(
            201,
            text=json.dumps(
                {
                    "full_name": "damien-robotsix/new-repo",
                    "html_url": "https://github.com/damien-robotsix/new-repo",
                }
            ),
        )
    )
    client = DirectRepoClient(_pat_settings())

    result = await client.create_repo("new-repo", True, description="d")

    assert "created successfully" in result
    assert "damien-robotsix/new-repo" in result
    request = route.calls.last.request
    _assert_pat_auth(request)
    assert json.loads(request.content) == {
        "name": "new-repo",
        "private": True,
        "auto_init": True,
        "description": "d",
    }


@pytest.mark.asyncio
async def test_create_repo_without_pat_errors_without_request(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(f"{_API}/user/repos")
    client = DirectRepoClient(_settings())

    result = await client.create_repo("new-repo", True)

    assert result.startswith("Error creating repo")
    assert "github_pat" in result
    assert not route.called


@pytest.mark.asyncio
async def test_create_repo_api_error_does_not_leak_token(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(f"{_API}/user/repos").mock(
        return_value=httpx.Response(422, text="name already exists")
    )
    client = DirectRepoClient(_pat_settings())

    result = await client.create_repo("dup", False)

    assert result.startswith("Error creating repo")
    assert _PAT not in result


# ---------------------------------------------------------------------------
# client — fork_repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_repo_polls_until_fork_exists(
    respx_mock: respx.MockRouter,
) -> None:
    fork_route = respx_mock.post(f"{_API}/repos/upstream/proj/forks").mock(
        return_value=httpx.Response(
            202,
            text=json.dumps(
                {
                    "full_name": "damien-robotsix/proj",
                    "html_url": "https://github.com/damien-robotsix/proj",
                }
            ),
        )
    )
    poll_route = respx_mock.get(f"{_API}/repos/damien-robotsix/proj").mock(
        side_effect=[
            httpx.Response(404, text="Not Found"),
            httpx.Response(404, text="Not Found"),
            httpx.Response(200, text="{}"),
        ]
    )
    client = DirectRepoClient(_pat_settings())

    result = await client.fork_repo("upstream", "proj", poll_interval=0)

    assert "created successfully" in result
    assert "upstream/proj" in result
    assert "damien-robotsix/proj" in result
    assert poll_route.call_count == 3
    _assert_pat_auth(fork_route.calls.last.request)
    assert json.loads(fork_route.calls.last.request.content) == {
        "default_branch_only": False
    }
    for call in poll_route.calls:
        _assert_pat_auth(call.request)


@pytest.mark.asyncio
async def test_fork_repo_poll_timeout(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_API}/repos/upstream/proj/forks").mock(
        return_value=httpx.Response(
            202, text=json.dumps({"full_name": "damien-robotsix/proj"})
        )
    )
    respx_mock.get(f"{_API}/repos/damien-robotsix/proj").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    client = DirectRepoClient(_pat_settings())

    result = await client.fork_repo("upstream", "proj", poll_interval=0, poll_timeout=0)

    assert "accepted by GitHub" in result
    assert "did not appear" in result


@pytest.mark.asyncio
async def test_fork_repo_rejects_invalid_upstream(
    respx_mock: respx.MockRouter,
) -> None:
    client = DirectRepoClient(_pat_settings())

    result = await client.fork_repo("..", "proj")

    assert result.startswith("Error forking repo: invalid repository")
    assert not respx_mock.calls


# ---------------------------------------------------------------------------
# tool registration gating
# ---------------------------------------------------------------------------


def _names(tools: list[Callable[..., Any]]) -> set[str]:
    return {t.__name__ for t in tools}


def test_tools_absent_without_pat() -> None:
    names = _names(build_direct_repo_tools(_settings()))
    assert "create_direct_repo" not in names
    assert "fork_direct_repo" not in names


def test_tools_present_with_pat() -> None:
    names = _names(build_direct_repo_tools(_pat_settings()))
    assert {"create_direct_repo", "fork_direct_repo"} <= names


def test_tools_absent_for_unattended_agents() -> None:
    names = _names(build_direct_repo_tools(_pat_settings(), operator_attended=False))
    assert "create_direct_repo" not in names
    assert "fork_direct_repo" not in names


def test_pat_secret_is_masked_by_config_api() -> None:
    from robotsix_chat.chat.server.routes.config import (
        _MASKED_SECRET_SENTINEL,
        _mask_secrets,
    )

    masked = _mask_secrets({"direct_repo": {"github_pat": _PAT}})
    assert masked["direct_repo"]["github_pat"] == _MASKED_SECRET_SENTINEL


# ---------------------------------------------------------------------------
# tools — confirmation echo and refusal paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tool_unconfirmed_only_echoes(
    respx_mock: respx.MockRouter,
) -> None:
    _mock_user(respx_mock)
    create_route = respx_mock.post(f"{_API}/user/repos")
    tool = _tool(build_direct_repo_tools(_pat_settings()), "create_direct_repo")

    result = await tool(name="new-repo", private=False)

    assert "NOT created yet" in result
    assert "damien-robotsix/new-repo" in result
    assert "public" in result
    assert not create_route.called


@pytest.mark.asyncio
async def test_create_tool_confirmed_creates_and_suggests_next_steps(
    respx_mock: respx.MockRouter,
) -> None:
    _mock_user(respx_mock)
    create_route = respx_mock.post(f"{_API}/user/repos").mock(
        return_value=httpx.Response(
            201, text=json.dumps({"full_name": "damien-robotsix/new-repo"})
        )
    )
    tool = _tool(build_direct_repo_tools(_pat_settings()), "create_direct_repo")

    result = await tool(name="new-repo", confirmed=True)

    assert "created successfully" in result
    assert "Visibility: private" in result
    assert "POST /repos" in result
    assert "apply-branch-protection.sh" in result
    _assert_pat_auth(create_route.calls.last.request)


@pytest.mark.asyncio
async def test_create_tool_refuses_invalid_name(
    respx_mock: respx.MockRouter,
) -> None:
    tool = _tool(build_direct_repo_tools(_pat_settings()), "create_direct_repo")

    result = await tool(name="../evil", confirmed=True)

    assert result.startswith("Refused")
    assert not respx_mock.calls


@pytest.mark.asyncio
async def test_create_tool_reports_pat_lookup_failure(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{_API}/user").mock(
        return_value=httpx.Response(401, text="Bad credentials")
    )
    create_route = respx_mock.post(f"{_API}/user/repos")
    tool = _tool(build_direct_repo_tools(_pat_settings()), "create_direct_repo")

    result = await tool(name="new-repo", confirmed=True)

    assert "could not resolve the github_pat account" in result
    assert _PAT not in result
    assert not create_route.called


@pytest.mark.asyncio
async def test_fork_tool_unconfirmed_only_echoes(
    respx_mock: respx.MockRouter,
) -> None:
    _mock_user(respx_mock)
    fork_route = respx_mock.post(f"{_API}/repos/upstream/proj/forks")
    tool = _tool(build_direct_repo_tools(_pat_settings()), "fork_direct_repo")

    result = await tool(upstream_full_name="upstream/proj")

    assert "NOT forked yet" in result
    assert "upstream/proj → damien-robotsix/proj" in result
    assert not fork_route.called


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["no-slash", "a/b/c", "/proj", "owner/"])
async def test_fork_tool_refuses_invalid_reference(
    respx_mock: respx.MockRouter, ref: str
) -> None:
    tool = _tool(build_direct_repo_tools(_pat_settings()), "fork_direct_repo")

    result = await tool(upstream_full_name=ref, confirmed=True)

    assert result.startswith("Refused")
    assert not respx_mock.calls


@pytest.mark.asyncio
async def test_fork_tool_refuses_own_repo(respx_mock: respx.MockRouter) -> None:
    _mock_user(respx_mock)
    fork_route = respx_mock.post(f"{_API}/repos/damien-robotsix/proj/forks")
    tool = _tool(build_direct_repo_tools(_pat_settings()), "fork_direct_repo")

    result = await tool(upstream_full_name="Damien-Robotsix/proj", confirmed=True)

    assert result.startswith("Refused")
    assert not fork_route.called

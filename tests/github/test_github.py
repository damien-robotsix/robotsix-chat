"""Tests for the GitHub repository management integration.

:func:`build_github_tools` and :class:`GitHubClient`, with ``respx``
mocked so there are no real network calls.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import GitHubSettings
from robotsix_chat.github import build_github_tools, load_github_skill
from robotsix_chat.github.client import GitHubClient


def _settings(**kw: Any) -> GitHubSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "api_base_url": "https://api.github.com",
        "token": "test-github-token",  # pragma: allowlist secret
    }
    base.update(kw)
    return GitHubSettings(**base)


# ---------------------------------------------------------------------------
# build_github_tools
# ---------------------------------------------------------------------------


def test_build_github_tools_disabled() -> None:
    """Verify that disabled GitHub returns no tools."""
    assert build_github_tools(GitHubSettings(enabled=False)) == []


def test_build_github_tools_returns_three_tools() -> None:
    """Enabled GitHub returns three callables: create, update, get."""
    tools = build_github_tools(_settings())
    names = {t.__name__ for t in tools}
    assert names == {
        "create_github_repo",
        "update_github_repo",
        "get_github_repo",
    }


# ---------------------------------------------------------------------------
# load_github_skill
# ---------------------------------------------------------------------------


def test_load_github_skill_returns_non_empty_markdown() -> None:
    """The shipped skill.md is loadable and contains safety rules."""
    skill = load_github_skill()
    assert len(skill) > 100
    assert "confirmation" in skill.lower()
    assert "Confirmation gate" in skill
    assert "create_github_repo" in skill
    assert "update_github_repo" in skill


# ---------------------------------------------------------------------------
# GitHubClient — Authorization header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_sends_bearer_token(
    respx_mock: respx.MockRouter,
) -> None:
    """The GitHub client sends Authorization: Bearer when a token is set."""
    route = respx_mock.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(200, json={"full_name": "owner/repo"})
    )

    client = GitHubClient(_settings(token="ghp_secret"))
    await client.get_repo("owner", "repo")

    assert route.calls.last.request.headers["authorization"] == "Bearer ghp_secret"


@pytest.mark.asyncio
async def test_client_no_auth_when_token_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """The GitHub client does NOT send Authorization when token is empty."""
    route = respx_mock.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(200, json={"full_name": "owner/repo"})
    )

    client = GitHubClient(_settings(token=""))
    await client.get_repo("owner", "repo")

    assert "authorization" not in route.calls.last.request.headers


# ---------------------------------------------------------------------------
# GitHubClient — get_repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_repo_returns_json(
    respx_mock: respx.MockRouter,
) -> None:
    """get_github_repo returns formatted JSON on success."""
    respx_mock.get("https://api.github.com/repos/owner/my-repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "owner/my-repo",
                "description": "Test repo",
                "private": True,
            },
        )
    )

    client = GitHubClient(_settings())
    out = await client.get_repo("owner", "my-repo")
    assert "owner/my-repo" in out
    assert "Test repo" in out


# ---------------------------------------------------------------------------
# GitHubClient — create_repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_repo_returns_created_repo_json(
    respx_mock: respx.MockRouter,
) -> None:
    """create_github_repo returns the created repo's JSON on success."""
    respx_mock.post("https://api.github.com/user/repos").mock(
        return_value=httpx.Response(
            201,
            json={
                "full_name": "owner/new-repo",
                "html_url": "https://github.com/owner/new-repo",
                "clone_url": "https://github.com/owner/new-repo.git",
                "private": True,
            },
        )
    )

    client = GitHubClient(_settings())
    out = await client.create_repo("new-repo", "A new repo", "private")
    assert "owner/new-repo" in out
    assert "html_url" in out


# ---------------------------------------------------------------------------
# GitHubClient — update_repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_repo_returns_updated_repo_json(
    respx_mock: respx.MockRouter,
) -> None:
    """update_github_repo returns the updated repo's JSON on success."""
    respx_mock.patch("https://api.github.com/repos/owner/my-repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "owner/my-repo",
                "description": "Updated desc",
                "private": False,
            },
        )
    )

    client = GitHubClient(_settings())
    out = await client.update_repo(
        "owner",
        "my-repo",
        description="Updated desc",
        visibility="public",
        has_issues=None,
        has_wiki=None,
    )
    assert "Updated desc" in out
    assert "owner/my-repo" in out


def test_update_repo_no_fields_returns_error() -> None:
    """update_github_repo returns an error when no fields are provided."""
    # This test is synchronous because the validation happens before any
    # network call.
    import asyncio

    async def _run() -> str:
        client = GitHubClient(_settings())
        return await client.update_repo("owner", "repo", None, None, None, None)

    out = asyncio.run(_run())
    assert "at least one field" in out.lower()


# ---------------------------------------------------------------------------
# GitHubClient — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_repo_http_error_returns_string(
    respx_mock: respx.MockRouter,
) -> None:
    """An HTTP error is returned as a concise string, never raised."""
    respx_mock.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    client = GitHubClient(_settings())
    out = await client.get_repo("owner", "repo")
    assert "GitHub API" in out
    assert "404" in out


@pytest.mark.asyncio
async def test_get_repo_network_error_returns_string(
    respx_mock: respx.MockRouter,
) -> None:
    """A network/connection error is returned as a string, never raised."""
    respx_mock.get("https://api.github.com/repos/owner/repo").mock(
        side_effect=ConnectionError("connection refused")
    )

    client = GitHubClient(_settings())
    out = await client.get_repo("owner", "repo")
    assert "GitHub API" in out
    assert "connection refused" in out.lower()


# ---------------------------------------------------------------------------
# create_github_repo — confirmation gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_github_repo_unconfirmed_returns_preview() -> None:
    """create_github_repo with confirmed=False returns a preview, no API call."""
    tools = build_github_tools(_settings())
    create_tool = next(t for t in tools if t.__name__ == "create_github_repo")

    out = await create_tool(
        name="test-repo",
        description="Test description",
        visibility="private",
        confirmed=False,
    )
    assert "Confirmation required" in out
    assert "test-repo" in out
    assert "Test description" in out


@pytest.mark.asyncio
async def test_create_github_repo_confirmed_calls_api(
    respx_mock: respx.MockRouter,
) -> None:
    """create_github_repo with confirmed=True makes the API call."""
    route = respx_mock.post("https://api.github.com/user/repos").mock(
        return_value=httpx.Response(
            201,
            json={
                "full_name": "owner/test-repo",
                "html_url": "https://github.com/owner/test-repo",
            },
        )
    )

    tools = build_github_tools(_settings())
    create_tool = next(t for t in tools if t.__name__ == "create_github_repo")

    out = await create_tool(
        name="test-repo",
        description="A repo",
        visibility="private",
        confirmed=True,
    )
    assert "owner/test-repo" in out
    assert route.called


@pytest.mark.asyncio
async def test_create_github_repo_bad_visibility() -> None:
    """create_github_repo rejects invalid visibility values."""
    tools = build_github_tools(_settings())
    create_tool = next(t for t in tools if t.__name__ == "create_github_repo")

    out = await create_tool(
        name="test-repo",
        visibility="internal",
        confirmed=True,
    )
    assert "Error" in out
    assert "visibility" in out.lower()

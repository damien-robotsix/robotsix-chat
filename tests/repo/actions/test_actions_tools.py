"""Tests for the GitHub Actions LLM tools."""

from __future__ import annotations

import time

import pytest
import respx

from robotsix_chat.config import DirectRepoSettings, GitHubActionsSettings
from robotsix_chat.repo.actions import (
    build_github_actions_tools,
    load_github_actions_skill,
)


def _actions_settings(**kw: object) -> GitHubActionsSettings:
    base: dict[str, object] = {
        "enabled": True,
        "github_org": "damien-robotsix",
    }
    base.update(kw)
    return GitHubActionsSettings(**base)  # type: ignore[arg-type]


def _direct_repo_settings(**kw: object) -> DirectRepoSettings:
    base: dict[str, object] = {
        "enabled": True,
        "github_app_id": "12345",
        "github_app_private_key": "fake-key",  # pragma: allowlist secret
        "github_app_installation_id": "67890",
        "board_api_base_url": "http://127.0.0.1:8077",
    }
    base.update(kw)
    return DirectRepoSettings(**base)  # type: ignore[arg-type]


def _prepopulate_installation_token(settings: DirectRepoSettings) -> str:
    """Set the installation token cache so JWT creation is skipped in tests."""
    from robotsix_chat.repo.direct.client import (
        _INSTALLATION_TOKEN_CACHE as _cache,
    )

    token = "ghs_test_installation_token"  # pragma: allowlist secret
    _cache[settings.github_app_installation_id] = (time.monotonic(), token)
    return token


@pytest.fixture(autouse=True)
def _clear_token_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear caches and mock JWT creation before each test."""
    from robotsix_chat.repo.direct.client import (
        _INSTALLATION_TOKEN_CACHE as _cache,
    )

    _cache.clear()
    from robotsix_chat.repo.direct.client import (
        _GITHUB_APP_JWT_CACHE as _jwt_cache,
    )

    _jwt_cache.clear()
    from robotsix_chat.repo.direct import client as _client_mod

    monkeypatch.setattr(_client_mod, "_make_jwt", lambda app_id, key: "fake-jwt-token")


# ---------------------------------------------------------------------------
# build_github_actions_tools
# ---------------------------------------------------------------------------


def test_build_github_actions_tools_disabled() -> None:
    """Disabled github_actions returns no tools."""
    assert (
        build_github_actions_tools(
            GitHubActionsSettings(enabled=False), _direct_repo_settings()
        )
        == []
    )


def test_build_github_actions_tools_returns_two_tools() -> None:
    """Enabled github_actions returns set_actions_secret and dispatch_workflow."""
    tools = build_github_actions_tools(_actions_settings(), _direct_repo_settings())
    assert len(tools) == 2
    names = {getattr(f, "__name__", str(f)) for f in tools}
    assert names == {"set_actions_secret", "dispatch_workflow"}


# ---------------------------------------------------------------------------
# set_actions_secret — scope check (no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_actions_secret_refuses_out_of_scope_repo(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → refused message."""
    dr = _direct_repo_settings()
    _prepopulate_installation_token(dr)

    # Mock list-installation-repos to return only one repo
    respx_mock.get(f"{dr.github_api_base_url}/installation/repositories").respond(
        json={"repositories": [{"full_name": "damien-robotsix/allowed-repo"}]}
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    set_secret = tools[0]

    result = await set_secret("other-repo", "MY_SECRET", "value")
    assert "Refused" in result
    assert "other-repo" in result


@pytest.mark.asyncio
async def test_dispatch_workflow_refuses_out_of_scope_repo(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → refused message."""
    dr = _direct_repo_settings()
    _prepopulate_installation_token(dr)

    respx_mock.get(f"{dr.github_api_base_url}/installation/repositories").respond(
        json={"repositories": [{"full_name": "damien-robotsix/allowed-repo"}]}
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    dispatch = tools[1]

    result = await dispatch("other-repo", "deploy.yml", ref="main")
    assert "Refused" in result
    assert "other-repo" in result


# ---------------------------------------------------------------------------
# dispatch_workflow — inputs parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_workflow_rejects_invalid_inputs_json(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-JSON inputs string → error message."""
    dr = _direct_repo_settings()
    _prepopulate_installation_token(dr)

    respx_mock.get(f"{dr.github_api_base_url}/installation/repositories").respond(
        json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]}
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    dispatch = tools[1]

    result = await dispatch("test-repo", "deploy.yml", inputs="not json")
    assert "Error" in result
    assert "inputs" in result.lower() or "JSON" in result


@pytest.mark.asyncio
async def test_dispatch_workflow_rejects_non_object_inputs(
    respx_mock: respx.MockRouter,
) -> None:
    """Inputs that parse but are not a dict → error message."""
    dr = _direct_repo_settings()
    _prepopulate_installation_token(dr)

    respx_mock.get(f"{dr.github_api_base_url}/installation/repositories").respond(
        json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]}
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    dispatch = tools[1]

    result = await dispatch("test-repo", "deploy.yml", inputs='["a", "list"]')
    assert "Error" in result


# ---------------------------------------------------------------------------
# load_github_actions_skill
# ---------------------------------------------------------------------------


def test_load_github_actions_skill_returns_string() -> None:
    """The shipped skill.md is readable and non-empty."""
    skill = load_github_actions_skill()
    assert isinstance(skill, str)
    assert len(skill) > 0
    assert "PUT /chat/github/repos" in skill
    assert "workflow_dispatch" in skill

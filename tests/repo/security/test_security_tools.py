"""Tests for the GitHub security-feature tools.

:func:`build_github_security_tools` and the ``set_repo_security_and_analysis``
tool, with ``respx`` mocked so there are no real network calls.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings, GitHubSecuritySettings
from robotsix_chat.repo.security import build_github_security_tools

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _gh_sec_settings(**kw: Any) -> GitHubSecuritySettings:
    base: dict[str, Any] = {
        "enabled": True,
        "github_org": "damien-robotsix",
    }
    base.update(kw)
    return GitHubSecuritySettings(**base)


def _direct_repo_settings(**kw: Any) -> DirectRepoSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "github_app_id": "12345",
        "github_app_private_key": "fake-key",  # pragma: allowlist secret
        "github_app_installation_id": "67890",
        "board_api_base_url": "http://127.0.0.1:8077",
    }
    base.update(kw)
    return DirectRepoSettings(**base)


@pytest.fixture(autouse=True)
def _mock_github_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock mint_installation_token so the shared library is never imported."""
    import sys
    from types import SimpleNamespace

    async def _fake_mint(**kw: object) -> str:
        return "ghs_test_installation_token"

    fake = SimpleNamespace()
    fake.mint_installation_token = _fake_mint
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)


# ---------------------------------------------------------------------------
# build_github_security_tools
# ---------------------------------------------------------------------------


def test_build_github_security_tools_disabled() -> None:
    """Verify that disabled github_security returns no tools."""
    assert (
        build_github_security_tools(
            GitHubSecuritySettings(enabled=False),
            DirectRepoSettings(),
        )
        == []
    )


def test_build_github_security_tools_returns_one_tool() -> None:
    """Verify that enabled github_security returns the tool."""
    tools = build_github_security_tools(_gh_sec_settings(), _direct_repo_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "set_repo_security_and_analysis"


# ---------------------------------------------------------------------------
# Scope check — repo not in installation scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuses_repo_not_in_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → refused with a descriptive message."""
    dr_settings = _direct_repo_settings()

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "damien-robotsix/other"}]}),
        )
    )

    tools = build_github_security_tools(_gh_sec_settings(), dr_settings)
    tool = tools[0]

    out = await tool(repo_name="my-repo", dependency_graph="enabled")
    assert "Refused" in out
    assert "damien-robotsix/my-repo" in out
    assert "scope" in out.lower()


# ---------------------------------------------------------------------------
# Successful PATCH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enables_dependency_graph(
    respx_mock: respx.MockRouter,
) -> None:
    """Enabling dependency_graph on a scoped repo calls the PATCH endpoint."""
    dr_settings = _direct_repo_settings()

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"repositories": [{"full_name": "damien-robotsix/my-repo"}]}
            ),
        )
    )
    patch_route = respx_mock.patch(
        "https://api.github.com/repos/damien-robotsix/my-repo"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "security_and_analysis": {
                        "dependency_graph": {"status": "enabled"},
                    }
                }
            ),
        )
    )

    tools = build_github_security_tools(_gh_sec_settings(), dr_settings)
    tool = tools[0]

    out = await tool(repo_name="my-repo", dependency_graph="enabled")
    assert "updated" in out.lower()
    assert "dependency_graph" in out

    # Verify the PATCH payload
    body = json.loads(patch_route.calls.last.request.content.decode())
    assert body == {
        "security_and_analysis": {"dependency_graph": {"status": "enabled"}}
    }


# ---------------------------------------------------------------------------
# Invalid toggle values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_invalid_toggle_value(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-'enabled'/'disabled' values are rejected early."""
    dr_settings = _direct_repo_settings()

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"repositories": [{"full_name": "damien-robotsix/my-repo"}]}
            ),
        )
    )

    tools = build_github_security_tools(_gh_sec_settings(), dr_settings)
    tool = tools[0]

    out = await tool(repo_name="my-repo", advanced_security="true")
    assert "Error" in out
    assert "advanced_security" in out
    assert "'enabled' or 'disabled'" in out


# ---------------------------------------------------------------------------
# No-op — all toggles None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_no_toggles(
    respx_mock: respx.MockRouter,
) -> None:
    """Passing no toggles (all None) is rejected."""
    dr_settings = _direct_repo_settings()

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"repositories": [{"full_name": "damien-robotsix/my-repo"}]}
            ),
        )
    )

    tools = build_github_security_tools(_gh_sec_settings(), dr_settings)
    tool = tools[0]

    out = await tool(repo_name="my-repo")
    assert "at least one" in out.lower()


# ---------------------------------------------------------------------------
# Multiple toggles at once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_toggles(
    respx_mock: respx.MockRouter,
) -> None:
    """Setting multiple features in one call works correctly."""
    dr_settings = _direct_repo_settings()

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"repositories": [{"full_name": "damien-robotsix/my-repo"}]}
            ),
        )
    )
    patch_route = respx_mock.patch(
        "https://api.github.com/repos/damien-robotsix/my-repo"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "security_and_analysis": {
                        "dependency_graph": {"status": "enabled"},
                        "advanced_security": {"status": "disabled"},
                        "secret_scanning": {"status": "enabled"},
                    }
                }
            ),
        )
    )

    tools = build_github_security_tools(_gh_sec_settings(), dr_settings)
    tool = tools[0]

    out = await tool(
        repo_name="my-repo",
        dependency_graph="enabled",
        advanced_security="disabled",
        secret_scanning="enabled",  # pragma: allowlist secret
    )
    assert "updated" in out.lower()

    body = json.loads(patch_route.calls.last.request.content.decode())
    assert body["security_and_analysis"] == {
        "dependency_graph": {"status": "enabled"},
        "advanced_security": {"status": "disabled"},
        "secret_scanning": {"status": "enabled"},
    }


# ---------------------------------------------------------------------------
# secret_scanning_push_protection toggle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_protection_toggle(
    respx_mock: respx.MockRouter,
) -> None:
    """secret_scanning_push_protection toggle is sent correctly."""
    dr_settings = _direct_repo_settings()

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"repositories": [{"full_name": "damien-robotsix/my-repo"}]}
            ),
        )
    )
    patch_route = respx_mock.patch(
        "https://api.github.com/repos/damien-robotsix/my-repo"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"security_and_analysis": {}}),
        )
    )

    tools = build_github_security_tools(_gh_sec_settings(), dr_settings)
    tool = tools[0]

    out = await tool(
        repo_name="my-repo",
        secret_scanning_push_protection="enabled",  # pragma: allowlist secret
    )
    assert "updated" in out.lower()

    body = json.loads(patch_route.calls.last.request.content.decode())
    assert body["security_and_analysis"] == {
        "secret_scanning_push_protection": {"status": "enabled"},
    }

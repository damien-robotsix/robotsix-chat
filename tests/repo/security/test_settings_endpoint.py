"""Tests for the ``PATCH /chat/github/repos/{owner}/{repo}/settings`` endpoint."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings, GitHubSecuritySettings


def _gh_sec_settings(**kw: object) -> GitHubSecuritySettings:
    base: dict[str, object] = {
        "enabled": True,
        "github_org": "damien-robotsix",
        "deploy_api_key": "test-api-key",  # pragma: allowlist secret
    }
    base.update(kw)
    return GitHubSecuritySettings(**base)


def _direct_repo_settings(**kw: object) -> DirectRepoSettings:
    base: dict[str, object] = {
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
# 503 — unconfigured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_503_when_github_security_disabled() -> None:
    """Disabled github_security → 503."""
    from tests.conftest import mock_app

    gh = GitHubSecuritySettings(
        enabled=False,
        deploy_api_key="test-key",  # pragma: allowlist secret
    )
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.patch(
            "/chat/github/repos/damien-robotsix/my-repo/settings",
            json={"dependency_graph": "enabled"},
            headers={"X-API-Key": "test-key"},
        )
    assert response.status_code == 503
    assert "not enabled" in response.text


@pytest.mark.asyncio
async def test_endpoint_503_when_deploy_api_key_empty() -> None:
    """Empty deploy_api_key → 503."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings(deploy_api_key="")
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.patch(
            "/chat/github/repos/damien-robotsix/my-repo/settings",
            json={"dependency_graph": "enabled"},
        )
    assert response.status_code == 503
    assert "deploy_api_key" in response.text


# ---------------------------------------------------------------------------
# 403 — auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_403_when_api_key_missing() -> None:
    """Missing X-API-Key header → 403."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.patch(
            "/chat/github/repos/damien-robotsix/my-repo/settings",
            json={"dependency_graph": "enabled"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_403_when_api_key_wrong() -> None:
    """Wrong X-API-Key header → 403."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.patch(
            "/chat/github/repos/damien-robotsix/my-repo/settings",
            json={"dependency_graph": "enabled"},
            headers={"X-API-Key": "wrong-key"},
        )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 400 — invalid body / path params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_400_when_body_not_json() -> None:
    """Non-JSON body → 400."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.patch(
            "/chat/github/repos/damien-robotsix/my-repo/settings",
            content=b"not json",
            headers={
                "X-API-Key": "test-api-key",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_400_when_no_toggles() -> None:
    """Empty body (no toggles) → 400."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.patch(
            "/chat/github/repos/damien-robotsix/my-repo/settings",
            json={},
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 400
    assert "at least one" in response.text.lower()


@pytest.mark.asyncio
async def test_endpoint_400_when_invalid_toggle_value() -> None:
    """Invalid toggle value → 400."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.patch(
            "/chat/github/repos/damien-robotsix/my-repo/settings",
            json={"dependency_graph": "true"},
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 400
    assert "dependency_graph" in response.text


# ---------------------------------------------------------------------------
# 404 — repo not in scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_404_when_repo_not_in_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → 404."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"repositories": [{"full_name": "damien-robotsix/other-repo"}]}
            ),
        )
    )

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.patch(
            "/chat/github/repos/damien-robotsix/my-repo/settings",
            json={"dependency_graph": "enabled"},
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 404
    data = response.json()
    assert "not in the GitHub App installation scope" in data["error"]
    assert "correlation_id" in data


# ---------------------------------------------------------------------------
# 200 — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_enables_dependency_graph(
    respx_mock: respx.MockRouter,
) -> None:
    """Successful dependency_graph enable → 200."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"repositories": [{"full_name": "damien-robotsix/my-repo"}]}
            ),
        )
    )
    respx_mock.patch("https://api.github.com/repos/damien-robotsix/my-repo").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"security_and_analysis": {"dependency_graph": {"status": "enabled"}}}
            ),
        )
    )

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.patch(
            "/chat/github/repos/damien-robotsix/my-repo/settings",
            json={"dependency_graph": "enabled"},
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["repo"] == "damien-robotsix/my-repo"
    assert "updated" in data["message"].lower()


@pytest.mark.asyncio
async def test_endpoint_cross_org_repo(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo under a non-default org works when in installation scope."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "other-org/some-repo"}]}),
        )
    )
    respx_mock.patch("https://api.github.com/repos/other-org/some-repo").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"security_and_analysis": {}}),
        )
    )

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.patch(
            "/chat/github/repos/other-org/some-repo/settings",
            json={"advanced_security": "disabled"},
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 200

"""Tests for the Actions HTTP endpoints."""

from __future__ import annotations

import time

import pytest

from robotsix_chat.config import DirectRepoSettings, GitHubActionsSettings
from robotsix_chat.repo.direct.client import (
    _INSTALLATION_TOKEN_CACHE as _token_cache,
)


def _actions_settings(**kw: object) -> GitHubActionsSettings:
    base: dict[str, object] = {
        "enabled": True,
        "github_org": "damien-robotsix",
        "deploy_api_key": "test-api-key",  # pragma: allowlist secret
    }
    base.update(kw)
    return GitHubActionsSettings(**base)


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


# ---------------------------------------------------------------------------
# 503 — unconfigured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_endpoint_503_when_disabled() -> None:
    """Disabled github_actions → 503 for secrets endpoint."""
    from tests.conftest import mock_app

    gh = GitHubActionsSettings(
        enabled=False,
        deploy_api_key="test-key",  # pragma: allowlist secret
    )
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_actions_settings=gh,
    ) as f:
        response = await f.client.put(
            "/chat/github/repos/damien-robotsix/my-repo/actions/secrets/MY_SECRET",
            json={"secret_value": "test"},
            headers={"X-API-Key": "test-key"},
        )
    assert response.status_code == 503
    assert "not enabled" in response.text


@pytest.mark.asyncio
async def test_workflow_endpoint_503_when_disabled() -> None:
    """Disabled github_actions → 503 for workflow dispatch endpoint."""
    from tests.conftest import mock_app

    gh = GitHubActionsSettings(
        enabled=False,
        deploy_api_key="test-key",  # pragma: allowlist secret
    )
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_actions_settings=gh,
    ) as f:
        response = await f.client.post(
            "/chat/github/repos/damien-robotsix/my-repo/actions/workflows/deploy.yml/dispatches",
            json={"ref": "main"},
            headers={"X-API-Key": "test-key"},
        )
    assert response.status_code == 503
    assert "not enabled" in response.text


@pytest.mark.asyncio
async def test_secret_endpoint_503_when_api_key_empty() -> None:
    """Empty deploy_api_key → 503."""
    from tests.conftest import mock_app

    gh = _actions_settings(deploy_api_key="")
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_actions_settings=gh,
    ) as f:
        response = await f.client.put(
            "/chat/github/repos/damien-robotsix/my-repo/actions/secrets/MY_SECRET",
            json={"secret_value": "test"},
            headers={"X-API-Key": "wrong"},
        )
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# 403 — auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_endpoint_403_when_bad_api_key() -> None:
    """Wrong API key → 403."""
    from tests.conftest import mock_app

    gh = _actions_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_actions_settings=gh,
    ) as f:
        response = await f.client.put(
            "/chat/github/repos/damien-robotsix/my-repo/actions/secrets/MY_SECRET",
            json={"secret_value": "test"},
            headers={"X-API-Key": "wrong-key"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_workflow_endpoint_403_when_bad_api_key() -> None:
    """Wrong API key → 403."""
    from tests.conftest import mock_app

    gh = _actions_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_actions_settings=gh,
    ) as f:
        response = await f.client.post(
            "/chat/github/repos/damien-robotsix/my-repo/actions/workflows/deploy.yml/dispatches",
            json={"ref": "main"},
            headers={"X-API-Key": "wrong-key"},
        )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 400 — bad request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_endpoint_400_missing_secret_value() -> None:
    """Missing secret_value → 400."""
    from tests.conftest import mock_app

    gh = _actions_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_actions_settings=gh,
    ) as f:
        response = await f.client.put(
            "/chat/github/repos/damien-robotsix/my-repo/actions/secrets/MY_SECRET",
            json={},
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_workflow_endpoint_400_missing_ref() -> None:
    """Missing ref → 400."""
    from tests.conftest import mock_app

    gh = _actions_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_actions_settings=gh,
    ) as f:
        response = await f.client.post(
            "/chat/github/repos/damien-robotsix/my-repo/actions/workflows/deploy.yml/dispatches",
            json={},
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 404 — repo not in scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_endpoint_404_repo_not_in_scope(respx_mock) -> None:
    """Repo not in installation scope → 404."""
    from tests.conftest import mock_app

    gh = _actions_settings()
    dr = _direct_repo_settings()
    dr_obj = dr
    _token_cache[dr_obj.github_app_installation_id] = (
        time.monotonic(),
        "ghs_test_token",
    )

    respx_mock.get(
        f"{dr.github_api_base_url}/installation/repositories"
    ).respond(
        json={
            "repositories": [
                {"full_name": "damien-robotsix/allowed-repo"}
            ]
        }
    )

    async with mock_app(
        direct_repo_settings=dr,
        github_actions_settings=gh,
    ) as f:
        response = await f.client.put(
            "/chat/github/repos/damien-robotsix/other-repo/actions/secrets/MY_SECRET",
            json={"secret_value": "test"},
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 404

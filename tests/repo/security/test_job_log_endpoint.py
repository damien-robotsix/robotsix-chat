"""Tests for the ``GET /chat/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs`` endpoint."""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings, GitHubSecuritySettings
from robotsix_chat.repo.direct.client import (
    _INSTALLATION_TOKEN_CACHE as _token_cache,
)


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


# ---------------------------------------------------------------------------
# 503 — unconfigured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_log_503_when_github_security_disabled() -> None:
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
        response = await f.client.get(
            "/chat/github/repos/damien-robotsix/my-repo/actions/jobs/12345/logs",
            headers={"X-API-Key": "test-key"},
        )
    assert response.status_code == 503
    assert "not enabled" in response.text


@pytest.mark.asyncio
async def test_job_log_503_when_deploy_api_key_empty() -> None:
    """Empty deploy_api_key → 503."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings(deploy_api_key="")
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.get(
            "/chat/github/repos/damien-robotsix/my-repo/actions/jobs/12345/logs",
        )
    assert response.status_code == 503
    assert "deploy_api_key" in response.text


# ---------------------------------------------------------------------------
# 403 — auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_log_403_when_api_key_missing() -> None:
    """Missing X-API-Key header → 403."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.get(
            "/chat/github/repos/damien-robotsix/my-repo/actions/jobs/12345/logs",
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_job_log_403_when_api_key_wrong() -> None:
    """Wrong X-API-Key header → 403."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.get(
            "/chat/github/repos/damien-robotsix/my-repo/actions/jobs/12345/logs",
            headers={"X-API-Key": "wrong-key"},
        )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 400 — invalid path params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_log_400_when_job_id_not_integer() -> None:
    """Non-integer job_id → 400."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.get(
            "/chat/github/repos/damien-robotsix/my-repo/actions/jobs/abc/logs",
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 400
    assert "job_id" in response.text


# ---------------------------------------------------------------------------
# 404 — repo not in scope / job not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_log_404_when_repo_not_in_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → 404."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()

    _token_cache["67890"] = (time.monotonic(), "ghs_test_token")

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
        response = await f.client.get(
            "/chat/github/repos/damien-robotsix/my-repo/actions/jobs/12345/logs",
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 404
    data = response.json()
    assert "not in the GitHub App installation scope" in data["error"]


# ---------------------------------------------------------------------------
# 200 — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_log_200_returns_log_text(
    respx_mock: respx.MockRouter,
) -> None:
    """Successful job log fetch → 200 with plain-text log content."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()
    log_content = "Run lftp deploy...\nTransfer complete.\nDone."

    _token_cache["67890"] = (time.monotonic(), "ghs_test_token")

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"repositories": [{"full_name": "damien-robotsix/my-repo"}]}
            ),
        )
    )
    # The GitHub Actions log endpoint returns a 302 to a signed URL; httpx
    # follows the redirect when follow_redirects=True, so we mock the final
    # content URL here.  The intermediate 302 is handled by httpx internally.
    respx_mock.get(
        "https://api.github.com/repos/damien-robotsix/my-repo/actions/jobs/12345/logs"
    ).mock(
        return_value=httpx.Response(
            200,
            text=log_content,
        )
    )

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.get(
            "/chat/github/repos/damien-robotsix/my-repo/actions/jobs/12345/logs",
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 200
    assert response.text == log_content
    # Should be plain text, not JSON
    assert response.headers.get("content-type", "").startswith("text/plain")


@pytest.mark.asyncio
async def test_job_log_200_different_org(
    respx_mock: respx.MockRouter,
) -> None:
    """Job log fetch for a repo under a non-default org."""
    from tests.conftest import mock_app

    gh = _gh_sec_settings()
    dr = _direct_repo_settings()
    log_content = "Build output here."

    _token_cache["67890"] = (time.monotonic(), "ghs_test_token")

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "other-org/some-repo"}]}),
        )
    )
    respx_mock.get(
        "https://api.github.com/repos/other-org/some-repo/actions/jobs/99999/logs"
    ).mock(
        return_value=httpx.Response(
            200,
            text=log_content,
        )
    )

    async with mock_app(
        direct_repo_settings=dr,
        github_security_settings=gh,
    ) as f:
        response = await f.client.get(
            "/chat/github/repos/other-org/some-repo/actions/jobs/99999/logs",
            headers={"X-API-Key": "test-api-key"},
        )
    assert response.status_code == 200
    assert response.text == log_content

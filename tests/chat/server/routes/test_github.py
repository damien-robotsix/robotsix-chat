"""Unit tests for the GitHub settings endpoint in ``github.py``."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.chat.server.routes.github import github_settings_endpoint

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mock_settings(*, enabled: bool = True, api_key: str = "secret-key") -> Mock:
    """Build a mock ``GitHubSecuritySettings``."""
    settings = Mock()
    settings.enabled = enabled
    settings.deploy_api_key = Mock()
    settings.deploy_api_key.get_secret_value.return_value = api_key
    return settings


def _mock_direct_repo_settings(*, enabled: bool = True) -> Mock:
    """Build a mock ``DirectRepoSettings``."""
    settings = Mock()
    settings.enabled = enabled
    return settings


def _make_patch_request(
    *,
    owner: str = "test-org",
    repo: str = "test-repo",
    body: object | None = None,
    api_key: str | None = "secret-key",
    github_settings: Mock | None = None,
    direct_repo_settings: Mock | None = None,
) -> Request:
    """Build a minimal Starlette ``Request`` for a PATCH with path params.

    When *github_settings* and *direct_repo_settings* are provided they
    are attached to ``request.app.state`` automatically.
    """
    app = Mock()
    app.state.github_security_settings = (
        github_settings if github_settings is not None else _mock_settings()
    )
    app.state.direct_repo_settings = (
        direct_repo_settings
        if direct_repo_settings is not None
        else _mock_direct_repo_settings()
    )

    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "PATCH",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": f"/chat/github/repos/{owner}/{repo}/settings",
        "path_params": {"owner": owner, "repo": repo},
        "query_string": b"",
        "headers": [],
        "app": app,
    }
    if api_key is not None:
        scope["headers"] = [
            (b"content-type", b"application/json"),
            (b"x-api-key", api_key.encode()),
        ]
    else:
        scope["headers"] = [(b"content-type", b"application/json")]

    body_bytes = json.dumps(body).encode() if body is not None else b"{}"

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    return Request(scope, receive)


# ---------------------------------------------------------------------------
# 503 — unconfigured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_503_when_github_security_disabled() -> None:
    """Returns 503 when ``github_security_settings.enabled`` is False."""
    request = _make_patch_request(github_settings=_mock_settings(enabled=False))
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "github_security is not enabled"


@pytest.mark.asyncio
async def test_503_when_direct_repo_disabled() -> None:
    """Returns 503 when ``direct_repo_settings.enabled`` is False."""
    request = _make_patch_request(
        direct_repo_settings=_mock_direct_repo_settings(enabled=False),
    )
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "direct_repo is not enabled"


@pytest.mark.asyncio
async def test_503_when_api_key_empty() -> None:
    """Returns 503 when ``deploy_api_key`` is an empty secret."""
    request = _make_patch_request(
        api_key="",
        github_settings=_mock_settings(api_key=""),
    )
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == ("github_security.deploy_api_key is not configured")


# ---------------------------------------------------------------------------
# 403 — auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_403_when_api_key_missing() -> None:
    """Returns 403 when the ``X-API-Key`` header is absent."""
    settings = _mock_settings(api_key="secret-key")  # pragma: allowlist secret
    request = _make_patch_request(
        api_key=None,
        github_settings=settings,
    )
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "invalid or missing X-API-Key"


@pytest.mark.asyncio
async def test_403_when_api_key_mismatch() -> None:
    """Returns 403 when ``X-API-Key`` does not match the configured key."""
    settings = _mock_settings(api_key="secret-key")  # pragma: allowlist secret
    request = _make_patch_request(
        api_key="wrong-key",
        github_settings=settings,
    )
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "invalid or missing X-API-Key"


# ---------------------------------------------------------------------------
# 400 — path params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_400_missing_owner() -> None:
    """Returns 400 when the owner path param is empty."""
    request = _make_patch_request(owner="")
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == ("owner and repo path parameters are required")


@pytest.mark.asyncio
async def test_400_missing_repo() -> None:
    """Returns 400 when the repo path param is empty."""
    request = _make_patch_request(repo="")
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == ("owner and repo path parameters are required")


@pytest.mark.asyncio
async def test_400_whitespace_only_owner() -> None:
    """Returns 400 when the owner path param is only whitespace."""
    request = _make_patch_request(owner="   ")
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == ("owner and repo path parameters are required")


# ---------------------------------------------------------------------------
# 400 — body validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_400_malformed_json() -> None:
    """Returns 400 when the request body is not valid JSON."""
    app = Mock()
    app.state.github_security_settings = _mock_settings()
    app.state.direct_repo_settings = _mock_direct_repo_settings()

    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "PATCH",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/chat/github/repos/org/repo/settings",
        "path_params": {"owner": "org", "repo": "repo"},
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"x-api-key", b"secret-key"),
        ],
        "app": app,
    }

    async def receive() -> dict[str, object]:
        return {
            "type": "http.request",
            "body": b"not valid json",
            "more_body": False,
        }

    request = Request(scope, receive)
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "invalid JSON body"


@pytest.mark.asyncio
async def test_400_body_is_array() -> None:
    """Returns 400 when the body is a JSON array instead of an object."""
    request = _make_patch_request(body=[1, 2, 3])
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "expected a JSON object"


@pytest.mark.asyncio
async def test_400_no_features_specified() -> None:
    """Returns 400 when no feature keys are present in the body."""
    request = _make_patch_request(body={"unrelated": "value"})
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == ("at least one security feature must be specified")


@pytest.mark.asyncio
async def test_400_empty_body_object() -> None:
    """Returns 400 when the body is an empty JSON object."""
    request = _make_patch_request(body={})
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == ("at least one security feature must be specified")


@pytest.mark.asyncio
async def test_400_invalid_feature_value() -> None:
    """Returns 400 when a feature value is not 'enabled' or 'disabled'."""
    request = _make_patch_request(body={"dependency_graph": "maybe"})
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 400
    assert "dependency_graph" in exc_info.value.detail
    assert "maybe" in exc_info.value.detail


@pytest.mark.asyncio
async def test_400_invalid_feature_value_non_string() -> None:
    """Returns 400 when a feature value is not a string at all."""
    request = _make_patch_request(body={"secret_scanning": 123})
    with pytest.raises(HTTPException) as exc_info:
        await github_settings_endpoint(request)
    assert exc_info.value.status_code == 400
    assert "secret_scanning" in exc_info.value.detail
    assert "123" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 404 — repo not in scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_repo_not_in_installation_scope() -> None:
    """Returns 404 when the repo is not in the installation's repo list."""
    request = _make_patch_request(body={"dependency_graph": "enabled"})
    with patch(
        "robotsix_chat.chat.server.routes.github.DirectRepoClient",
        autospec=True,
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.list_installation_repos = AsyncMock(
            return_value=["other-org/other-repo"],
        )
        with pytest.raises(HTTPException) as exc_info:
            await github_settings_endpoint(request)
        assert exc_info.value.status_code == 404
        assert "not in the GitHub App installation scope" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 502 — client errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_502_list_installation_repos_failure() -> None:
    """Returns 502 when ``list_installation_repos`` raises an exception."""
    request = _make_patch_request(body={"dependency_graph": "enabled"})
    with patch(
        "robotsix_chat.chat.server.routes.github.DirectRepoClient",
        autospec=True,
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.list_installation_repos = AsyncMock(
            side_effect=RuntimeError("API down"),
        )
        with pytest.raises(HTTPException) as exc_info:
            await github_settings_endpoint(request)
        assert exc_info.value.status_code == 502
        assert "GitHub API error" in exc_info.value.detail
        assert "API down" in exc_info.value.detail


@pytest.mark.asyncio
async def test_502_set_security_and_analysis_returns_error() -> None:
    """Returns 502 when ``set_security_and_analysis`` returns an error string."""
    request = _make_patch_request(body={"dependency_graph": "enabled"})
    with patch(
        "robotsix_chat.chat.server.routes.github.DirectRepoClient",
        autospec=True,
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.list_installation_repos = AsyncMock(
            return_value=["test-org/test-repo"],
        )
        mock_client.set_security_and_analysis = AsyncMock(
            return_value="Error: something went wrong",
        )
        with pytest.raises(HTTPException) as exc_info:
            await github_settings_endpoint(request)
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail == "Error: something went wrong"


# ---------------------------------------------------------------------------
# 200 — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_200_single_feature() -> None:
    """Returns 200 when a single valid feature is toggled successfully."""
    request = _make_patch_request(body={"dependency_graph": "enabled"})
    with patch(
        "robotsix_chat.chat.server.routes.github.DirectRepoClient",
        autospec=True,
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.list_installation_repos = AsyncMock(
            return_value=["test-org/test-repo"],
        )
        mock_client.set_security_and_analysis = AsyncMock(
            return_value="settings applied",
        )
        response = await github_settings_endpoint(request)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["status"] == "ok"
    assert body["repo"] == "test-org/test-repo"
    assert body["message"] == "settings applied"

    # Verify the correct kwargs were passed to set_security_and_analysis.
    mock_client.set_security_and_analysis.assert_called_once_with(
        "test-org/test-repo",
        dependency_graph="enabled",
        advanced_security=None,
        secret_scanning=None,
        secret_scanning_push_protection=None,
    )


@pytest.mark.asyncio
async def test_200_multiple_features() -> None:
    """Returns 200 when multiple features are toggled at once."""
    request = _make_patch_request(
        body={
            "dependency_graph": "enabled",
            "advanced_security": "disabled",
            "secret_scanning": "enabled",  # pragma: allowlist secret
            "secret_scanning_push_protection": "disabled",  # pragma: allowlist secret
        },
    )
    with patch(
        "robotsix_chat.chat.server.routes.github.DirectRepoClient",
        autospec=True,
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.list_installation_repos = AsyncMock(
            return_value=["test-org/test-repo"],
        )
        mock_client.set_security_and_analysis = AsyncMock(
            return_value="settings applied",
        )
        response = await github_settings_endpoint(request)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["status"] == "ok"

    mock_client.set_security_and_analysis.assert_called_once_with(
        "test-org/test-repo",
        dependency_graph="enabled",
        advanced_security="disabled",
        secret_scanning="enabled",
        secret_scanning_push_protection="disabled",
    )


@pytest.mark.asyncio
async def test_200_disabled_feature() -> None:
    """Returns 200 when a feature is toggled to 'disabled'."""
    request = _make_patch_request(body={"secret_scanning": "disabled"})
    with patch(
        "robotsix_chat.chat.server.routes.github.DirectRepoClient",
        autospec=True,
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.list_installation_repos = AsyncMock(
            return_value=["test-org/test-repo"],
        )
        mock_client.set_security_and_analysis = AsyncMock(
            return_value="settings applied",
        )
        response = await github_settings_endpoint(request)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    mock_client.set_security_and_analysis.assert_called_once_with(
        "test-org/test-repo",
        dependency_graph=None,
        advanced_security=None,
        secret_scanning="disabled",
        secret_scanning_push_protection=None,
    )

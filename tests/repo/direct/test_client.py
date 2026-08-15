"""Tests for the direct repo client (:class:`DirectRepoClient`).

Uses ``respx`` for HTTP mocking — no real network calls.
Shared fixtures live in ``tests/repo/direct/conftest.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.common.http import HttpResult
from robotsix_chat.repo.direct.client import (
    _INSTALLATION_TOKEN_CACHE,
    DirectRepoClient,
    _b64decode,
    _b64encode,
    _count_cycles_from_data,
    _get_installation_token,
)

# ============================================================================
# _b64decode / _b64encode
# ============================================================================


def test_b64decode_normal() -> None:
    assert _b64decode("aGVsbG8=") == b"hello"


def test_b64decode_missing_padding() -> None:
    assert _b64decode("aGVsbG8") == b"hello"


def test_b64encode_no_padding() -> None:
    assert _b64encode(b"hello") == "aGVsbG8"


# ============================================================================
# _get_installation_token
# ============================================================================


@pytest.mark.asyncio
async def test_get_installation_token_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.repo.direct.conftest import _settings

    settings = _settings()

    async def _fake_build(dr: Any, label: str, token_cache: Any = None) -> str | None:
        return "ghs_fresh_token"

    monkeypatch.setattr(
        "robotsix_chat.repo.direct.client._build_github_app_auth_headers",
        _fake_build,
    )
    token = await _get_installation_token(settings)
    assert token == "ghs_fresh_token"


@pytest.mark.asyncio
async def test_get_installation_token_none_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.repo.direct.conftest import _settings

    settings = _settings()

    async def _fake_build(dr: Any, label: str, token_cache: Any = None) -> str | None:
        return None

    monkeypatch.setattr(
        "robotsix_chat.repo.direct.client._build_github_app_auth_headers",
        _fake_build,
    )
    with pytest.raises(RuntimeError, match="Failed to mint GitHub App"):
        await _get_installation_token(settings)


# ============================================================================
# _count_cycles_from_data
# ============================================================================


def test_count_cycles_from_events() -> None:
    data: dict[str, Any] = {
        "events": [
            {"type": "implement_started"},
            {"type": "implement_complete"},
            {"type": "resume"},
        ]
    }
    assert _count_cycles_from_data(data) == 5


def test_count_cycles_from_history() -> None:
    data: dict[str, Any] = {
        "history": [
            {"state": "implement_complete"},
            {"action": "implement"},
            {"state": "blocked"},
        ]
    }
    assert _count_cycles_from_data(data) == 2


def test_count_cycles_from_direct_field() -> None:
    data: dict[str, Any] = {"cycle_count": 7}
    assert _count_cycles_from_data(data) == 7


def test_count_cycles_empty_data() -> None:
    assert _count_cycles_from_data({}) == 0


def test_count_cycles_resume_counts_as_three() -> None:
    data: dict[str, Any] = {
        "events": [
            {"action": "resume"},
            {"type": "unblock"},
        ]
    }
    assert _count_cycles_from_data(data) == 6


def test_count_cycles_skips_non_dict_entries() -> None:
    data: dict[str, Any] = {
        "events": [
            {"type": "implement_started"},
            "not a dict",
            None,
            {"action": "resume"},
        ]
    }
    assert _count_cycles_from_data(data) == 4


# ============================================================================
# DirectRepoClient — construction
# ============================================================================


def test_init_strips_trailing_slash() -> None:
    from tests.repo.direct.conftest import _settings

    s = _settings(github_api_base_url="https://api.github.com/")
    client = DirectRepoClient(s)
    assert client._base_url == "https://api.github.com"


def test_init_no_trailing_slash_unchanged() -> None:
    from tests.repo.direct.conftest import _settings

    s = _settings(github_api_base_url="https://api.github.com")
    client = DirectRepoClient(s)
    assert client._base_url == "https://api.github.com"


# ============================================================================
# DirectRepoClient — token and auth helpers
# ============================================================================


def test_invalidate_token_clears_cache() -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    assert _INSTALLATION_TOKEN_CACHE.get(s.github_app_installation_id) is not None
    client._invalidate_token()
    assert _INSTALLATION_TOKEN_CACHE.get(s.github_app_installation_id) is None


@pytest.mark.asyncio
async def test_gh_headers_contains_bearer() -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    headers = await client._gh_headers()
    assert headers["Authorization"] == "Bearer ghs_prepopulated_token"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


# ============================================================================
# DirectRepoClient — _http_with_retry
# ============================================================================


@pytest.mark.asyncio
async def test_http_with_retry_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    async def fake_safe(method: str, url: str, **kwargs: Any) -> HttpResult:
        return HttpResult(text="ok", status_code=200)

    monkeypatch.setattr("robotsix_chat.repo.direct.client.safe_http_request", fake_safe)

    result = await client._http_with_retry("GET", "https://example.com")
    assert result.ok
    assert result.text == "ok"


@pytest.mark.asyncio
async def test_http_with_retry_401_retries_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    call_count = 0

    async def fake_safe(method: str, url: str, **kwargs: Any) -> HttpResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return HttpResult(status_code=401, error="Unauthorized")
        return HttpResult(text="ok", status_code=200)

    monkeypatch.setattr("robotsix_chat.repo.direct.client.safe_http_request", fake_safe)

    result = await client._http_with_retry("GET", "https://example.com")
    assert result.ok
    assert call_count == 2


@pytest.mark.asyncio
async def test_http_with_retry_401_both_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    async def fake_safe(method: str, url: str, **kwargs: Any) -> HttpResult:
        return HttpResult(status_code=401, error="Unauthorized")

    monkeypatch.setattr("robotsix_chat.repo.direct.client.safe_http_request", fake_safe)

    result = await client._http_with_retry("GET", "https://example.com")
    assert not result.ok
    assert result.status_code == 401


# ============================================================================
# DirectRepoClient — _get_json
# ============================================================================


@pytest.mark.asyncio
async def test_get_json_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"full_name": "org/repo", "default_branch": "main"}),
        )
    )

    result = await client._get_json("/repos/org/repo")
    assert result["full_name"] == "org/repo"


@pytest.mark.asyncio
async def test_get_json_error_raises_runtime_error(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    with pytest.raises(RuntimeError, match="GitHub API GET /repos/org/repo"):
        await client._get_json("/repos/org/repo")


@pytest.mark.asyncio
async def test_get_json_invalid_json_raises_runtime_error(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="not json")
    )

    with pytest.raises(RuntimeError, match="invalid JSON"):
        await client._get_json("/repos/org/repo")


# ============================================================================
# DirectRepoClient — _request_json / _post_json / _patch_json
# ============================================================================


@pytest.mark.asyncio
async def test_post_json_204_no_content(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.post("https://api.github.com/repos/org/repo/endpoint").mock(
        return_value=httpx.Response(204)
    )

    result = await client._post_json("/repos/org/repo/endpoint", {"key": "val"})
    assert result == {}


@pytest.mark.asyncio
async def test_request_json_error_raises_runtime_error(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.post("https://api.github.com/repos/org/repo/endpoint").mock(
        return_value=httpx.Response(500, text="Server Error")
    )

    with pytest.raises(RuntimeError, match="GitHub API POST /repos/org/repo/endpoint"):
        await client._post_json("/repos/org/repo/endpoint", {"key": "val"})


# ============================================================================
# DirectRepoClient — _git_push_files
# ============================================================================


@pytest.mark.asyncio
async def test_git_push_files_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.post("https://api.github.com/repos/org/repo/git/blobs").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "blob_sha_1"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/commits/base_sha").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"tree": {"sha": "base_tree_sha"}})
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/trees").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "new_tree_sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/commits").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "commit_sha_1"}))
    )

    commit_sha = await client._git_push_files(
        repo_full_name="org/repo",
        base_sha="base_sha",
        files=[{"path": "README.md", "content": "# Hello"}],
        commit_message="test commit",
    )
    assert commit_sha == "commit_sha_1"


@pytest.mark.asyncio
async def test_git_push_files_missing_path_raises(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    with pytest.raises(ValueError, match="must have a 'path' field"):
        await client._git_push_files(
            repo_full_name="org/repo",
            base_sha="base_sha",
            files=[{"content": "no path here"}],
            commit_message="test",
        )


@pytest.mark.asyncio
async def test_git_push_files_changelog_trailing_newline(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.post("https://api.github.com/repos/org/repo/git/blobs").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "blob_sha_c"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/commits/base_sha").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"tree": {"sha": "base_tree_sha"}})
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/trees").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "new_tree_sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/commits").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "commit_sha_c"}))
    )

    await client._git_push_files(
        repo_full_name="org/repo",
        base_sha="base_sha",
        files=[
            {"path": "changelog.d/123.feature.md", "content": "no trailing newline"}
        ],
        commit_message="test",
    )

    blob_request = respx_mock.calls[0].request
    blob_body = json.loads(blob_request.content or "{}")
    assert blob_body["content"] == "no trailing newline\n"


@pytest.mark.asyncio
async def test_git_push_files_changelog_already_has_newline(
    respx_mock: respx.MockRouter,
) -> None:
    """Changelog content that already ends with newline is not double-appended."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.post("https://api.github.com/repos/org/repo/git/blobs").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "blob_sha_c2"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/commits/base_sha").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"tree": {"sha": "base_tree_sha"}})
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/trees").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "new_tree_sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/commits").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "commit_sha_c2"}))
    )

    await client._git_push_files(
        repo_full_name="org/repo",
        base_sha="base_sha",
        files=[{"path": "changelog.d/456.bugfix.md", "content": "has newline\n"}],
        commit_message="test",
    )

    blob_request = respx_mock.calls[0].request
    blob_body = json.loads(blob_request.content or "{}")
    assert blob_body["content"] == "has newline\n"


# ============================================================================
# DirectRepoClient — create_repo
# ============================================================================


@pytest.mark.asyncio
async def test_create_repo_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.post("https://api.github.com/orgs/myorg/repos").mock(
        return_value=httpx.Response(
            201,
            text=json.dumps({"html_url": "https://github.com/myorg/newrepo"}),
        )
    )

    result = await client.create_repo(org_name="myorg", repo_name="newrepo")
    assert "created successfully" in result
    assert "myorg/newrepo" in result


@pytest.mark.asyncio
async def test_create_repo_api_error(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.post("https://api.github.com/orgs/myorg/repos").mock(
        return_value=httpx.Response(422, text="Validation Failed")
    )

    result = await client.create_repo(org_name="myorg", repo_name="bad-name")
    assert "Error creating repo" in result


# ============================================================================
# DirectRepoClient — list_installation_repos
# ============================================================================


@pytest.mark.asyncio
async def test_list_installation_repos_single_page(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get(
        "https://api.github.com/installation/repositories?per_page=100&page=1"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "repositories": [
                        {"full_name": "org/repo1"},
                        {"full_name": "org/repo2"},
                    ]
                }
            ),
        )
    )

    repos = await client.list_installation_repos()
    assert repos == ["org/repo1", "org/repo2"]


@pytest.mark.asyncio
async def test_list_installation_repos_multi_page(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    page1 = [{"full_name": f"org/repo{i}"} for i in range(100)]
    respx_mock.get(
        "https://api.github.com/installation/repositories?per_page=100&page=1"
    ).mock(return_value=httpx.Response(200, text=json.dumps({"repositories": page1})))
    page2 = [{"full_name": f"org/repo{i}"} for i in range(100, 130)]
    respx_mock.get(
        "https://api.github.com/installation/repositories?per_page=100&page=2"
    ).mock(return_value=httpx.Response(200, text=json.dumps({"repositories": page2})))

    repos = await client.list_installation_repos()
    assert len(repos) == 130


@pytest.mark.asyncio
async def test_list_installation_repos_empty(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get(
        "https://api.github.com/installation/repositories?per_page=100&page=1"
    ).mock(return_value=httpx.Response(200, text=json.dumps({"repositories": []})))

    repos = await client.list_installation_repos()
    assert repos == []


# ============================================================================
# DirectRepoClient — check_installation_scope
# ============================================================================


@pytest.mark.asyncio
async def test_check_installation_scope_in_scope(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get(
        "https://api.github.com/installation/repositories?per_page=100&page=1"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/target"}]}),
        )
    )

    result = await client.check_installation_scope("org/target")
    assert result is None


@pytest.mark.asyncio
async def test_check_installation_scope_not_in_scope(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get(
        "https://api.github.com/installation/repositories?per_page=100&page=1"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/other"}]}),
        )
    )

    result = await client.check_installation_scope("org/target")
    assert result is not None
    assert "not installed" in result
    assert "org/other" in result


@pytest.mark.asyncio
async def test_check_installation_scope_empty_installation(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get(
        "https://api.github.com/installation/repositories?per_page=100&page=1"
    ).mock(return_value=httpx.Response(200, text=json.dumps({"repositories": []})))

    result = await client.check_installation_scope("org/target")
    assert result is not None
    assert "not installed on any repository" in result


# ============================================================================
# DirectRepoClient — push_branch
# ============================================================================


@pytest.mark.asyncio
async def test_push_branch_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/ref/heads/main").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"object": {"sha": "base_sha"}})
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/blobs").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "blob_sha"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/commits/base_sha").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"tree": {"sha": "base_tree_sha"}})
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/trees").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "new_tree_sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/commits").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "commit_sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/refs").mock(
        return_value=httpx.Response(
            201, text=json.dumps({"ref": "refs/heads/feature/x"})
        )
    )

    result = await client.push_branch(
        repo_full_name="org/repo",
        branch_name="feature/x",
        files=[{"path": "file.txt", "content": "hello"}],
        commit_message="test",
        ticket_id="T-1",
    )
    assert "pushed successfully" in result
    assert "feature/x" in result


@pytest.mark.asyncio
async def test_push_branch_api_error(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    result = await client.push_branch(
        repo_full_name="org/repo",
        branch_name="feature/x",
        files=[{"path": "file.txt", "content": "hello"}],
        commit_message="test",
        ticket_id="T-1",
    )
    assert "Error pushing branch" in result


# ============================================================================
# DirectRepoClient — create_pr
# ============================================================================


@pytest.mark.asyncio
async def test_create_pr_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/pulls").mock(
        return_value=httpx.Response(
            201,
            text=json.dumps({"html_url": "https://github.com/org/repo/pull/42"}),
        )
    )

    result = await client.create_pr(
        repo_full_name="org/repo",
        head_branch="feature/x",
        title="My PR",
        body="Description",
    )
    assert "Pull request opened successfully" in result
    assert "human review required" in result


@pytest.mark.asyncio
async def test_create_pr_api_error(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )

    result = await client.create_pr(
        repo_full_name="org/repo",
        head_branch="feature/x",
        title="My PR",
        body="Desc",
    )
    assert "Error opening PR" in result


# ============================================================================
# DirectRepoClient — update_pr_branch
# ============================================================================


@pytest.mark.asyncio
async def test_update_pr_branch_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.put("https://api.github.com/repos/org/repo/pulls/42/update-branch").mock(
        return_value=httpx.Response(202, text="{}")
    )

    result = await client.update_pr_branch(repo_full_name="org/repo", pr_number=42)
    assert "queued for branch update" in result


@pytest.mark.asyncio
async def test_update_pr_branch_422_conflict(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.put("https://api.github.com/repos/org/repo/pulls/42/update-branch").mock(
        return_value=httpx.Response(422, text="Merge conflict")
    )

    result = await client.update_pr_branch(repo_full_name="org/repo", pr_number=42)
    assert "merge conflict detected" in result


@pytest.mark.asyncio
async def test_update_pr_branch_error(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.put("https://api.github.com/repos/org/repo/pulls/42/update-branch").mock(
        return_value=httpx.Response(500, text="Server Error")
    )

    result = await client.update_pr_branch(repo_full_name="org/repo", pr_number=42)
    assert "Error updating PR branch" in result


# ============================================================================
# DirectRepoClient — get_pr
# ============================================================================


@pytest.mark.asyncio
async def test_get_pr_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"number": 42, "title": "Fix bug", "state": "open"}),
        )
    )

    pr = await client.get_pr(repo_full_name="org/repo", pr_number=42)
    assert pr["number"] == 42
    assert pr["title"] == "Fix bug"


@pytest.mark.asyncio
async def test_get_pr_error_raises(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/99").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    with pytest.raises(RuntimeError, match="GitHub API GET"):
        await client.get_pr(repo_full_name="org/repo", pr_number=99)


# ============================================================================
# DirectRepoClient — search_open_prs
# ============================================================================


@pytest.mark.asyncio
async def test_search_open_prs_single_page(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get(
        "https://api.github.com/search/issues"
        "?q=type%3Apr%20state%3Aopen%20org%3Aorg&per_page=100&page=1"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "total_count": 1,
                    "items": [
                        {
                            "number": 7,
                            "title": "Batch PR listing",
                            "repository_url": "https://api.github.com/repos/org/repo1",
                        }
                    ],
                }
            ),
        )
    )

    items = await client.search_open_prs(org_name="org")
    assert len(items) == 1
    assert items[0]["number"] == 7


@pytest.mark.asyncio
async def test_search_open_prs_paginates(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    page1 = [{"number": i} for i in range(100)]
    respx_mock.get(
        "https://api.github.com/search/issues"
        "?q=type%3Apr%20state%3Aopen%20org%3Aorg&per_page=100&page=1"
    ).mock(return_value=httpx.Response(200, text=json.dumps({"items": page1})))
    page2 = [{"number": i} for i in range(100, 130)]
    respx_mock.get(
        "https://api.github.com/search/issues"
        "?q=type%3Apr%20state%3Aopen%20org%3Aorg&per_page=100&page=2"
    ).mock(return_value=httpx.Response(200, text=json.dumps({"items": page2})))

    items = await client.search_open_prs(org_name="org")
    assert len(items) == 130


@pytest.mark.asyncio
async def test_search_open_prs_error_raises(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get(
        "https://api.github.com/search/issues"
        "?q=type%3Apr%20state%3Aopen%20org%3Aorg&per_page=100&page=1"
    ).mock(return_value=httpx.Response(422, text="Validation Failed"))

    with pytest.raises(RuntimeError, match="GitHub API GET"):
        await client.search_open_prs(org_name="org")


# ============================================================================
# DirectRepoClient — push_commit_to_branch
# ============================================================================


@pytest.mark.asyncio
async def test_push_commit_to_branch_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/git/ref/heads/feature").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"object": {"sha": "base_sha"}})
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/blobs").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "blob_sha"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/commits/base_sha").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"tree": {"sha": "base_tree_sha"}})
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/trees").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "new_tree_sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/commits").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "commit_sha"}))
    )
    respx_mock.patch(
        "https://api.github.com/repos/org/repo/git/refs/heads/feature"
    ).mock(return_value=httpx.Response(200, text=json.dumps({})))

    result = await client.push_commit_to_branch(
        repo_full_name="org/repo",
        branch_name="feature",
        files=[{"path": "file.txt", "content": "hello"}],
        commit_message="test",
        ticket_id="T-1",
    )
    assert "Commit pushed successfully" in result


@pytest.mark.asyncio
async def test_push_commit_to_branch_api_error(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/git/ref/heads/feature").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    result = await client.push_commit_to_branch(
        repo_full_name="org/repo",
        branch_name="feature",
        files=[{"path": "file.txt", "content": "hello"}],
        commit_message="test",
        ticket_id="T-1",
    )
    assert "Error pushing commit" in result


# ============================================================================
# DirectRepoClient — set_security_and_analysis
# ============================================================================


@pytest.mark.asyncio
async def test_set_security_and_analysis_success(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.patch("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"security_and_analysis": {}}))
    )

    result = await client.set_security_and_analysis(
        "org/repo",
        dependency_graph="enabled",
        secret_scanning="disabled",  # pragma: allowlist secret
    )
    assert "Security settings updated" in result
    assert "dependency_graph" in result
    assert "secret_scanning" in result


@pytest.mark.asyncio
async def test_set_security_and_analysis_invalid_value() -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    result = await client.set_security_and_analysis(
        "org/repo",
        dependency_graph="invalid",
    )
    assert "must be 'enabled' or 'disabled'" in result


@pytest.mark.asyncio
async def test_set_security_and_analysis_no_features() -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    result = await client.set_security_and_analysis("org/repo")
    assert "at least one security feature must be specified" in result


@pytest.mark.asyncio
async def test_set_security_and_analysis_api_error(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.patch("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )

    result = await client.set_security_and_analysis(
        "org/repo",
        advanced_security="enabled",
    )
    assert "Error updating security settings" in result


# ============================================================================
# DirectRepoClient — merge_pr
# ============================================================================


@pytest.mark.asyncio
async def test_merge_pr_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    # get_pr
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "number": 42,
                    "draft": False,
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                }
            ),
        )
    )
    # merge
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/42/merge").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"merged": True, "sha": "merge_sha_1"}),
        )
    )

    result = await client.merge_pr(repo_full_name="org/repo", pr_number=42)
    assert "merged successfully" in result
    assert "merge_sha_1" in result


@pytest.mark.asyncio
async def test_merge_pr_draft_blocked(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"draft": True, "merged": False}),
        )
    )

    result = await client.merge_pr(repo_full_name="org/repo", pr_number=42)
    assert "draft state" in result


@pytest.mark.asyncio
async def test_merge_pr_conflict_blocked(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "draft": False,
                    "mergeable": False,
                    "mergeable_state": "dirty",
                    "merged": False,
                }
            ),
        )
    )

    result = await client.merge_pr(repo_full_name="org/repo", pr_number=42)
    assert "merge conflicts detected" in result


@pytest.mark.asyncio
async def test_merge_pr_not_yet_computed(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "draft": False,
                    "mergeable": None,
                    "mergeable_state": "unknown",
                    "merged": False,
                }
            ),
        )
    )

    result = await client.merge_pr(repo_full_name="org/repo", pr_number=42)
    assert "mergeability is still being computed" in result


@pytest.mark.asyncio
async def test_merge_pr_already_merged(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "draft": False,
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": True,
                    "merge_commit_sha": "abc123",
                }
            ),
        )
    )

    result = await client.merge_pr(repo_full_name="org/repo", pr_number=42)
    assert "already merged" in result


@pytest.mark.asyncio
async def test_merge_pr_405_blocked(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "draft": False,
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/42/merge").mock(
        return_value=httpx.Response(405, text="Not mergeable")
    )

    result = await client.merge_pr(repo_full_name="org/repo", pr_number=42)
    assert "not in a mergeable state" in result


@pytest.mark.asyncio
async def test_merge_pr_409_conflict(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "draft": False,
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/42/merge").mock(
        return_value=httpx.Response(409, text="Conflict")
    )

    result = await client.merge_pr(repo_full_name="org/repo", pr_number=42)
    assert "merge conflict or SHA mismatch" in result


@pytest.mark.asyncio
async def test_merge_pr_fetch_error(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    result = await client.merge_pr(repo_full_name="org/repo", pr_number=42)
    assert "Error fetching PR" in result


# ============================================================================
# DirectRepoClient — arm_auto_merge
# ============================================================================


@pytest.mark.asyncio
async def test_arm_auto_merge_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"draft": False, "merged": False}),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/42/auto-merge").mock(
        return_value=httpx.Response(200, text="{}")
    )

    result = await client.arm_auto_merge(repo_full_name="org/repo", pr_number=42)
    assert "Auto-merge enabled" in result


@pytest.mark.asyncio
async def test_arm_auto_merge_draft(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"draft": True, "merged": False})
        )
    )

    result = await client.arm_auto_merge(repo_full_name="org/repo", pr_number=42)
    assert "draft state" in result


@pytest.mark.asyncio
async def test_arm_auto_merge_already_merged(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"draft": False, "merged": True})
        )
    )

    result = await client.arm_auto_merge(repo_full_name="org/repo", pr_number=42)
    assert "already merged" in result


@pytest.mark.asyncio
async def test_arm_auto_merge_403_forbidden(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"draft": False, "merged": False}),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/42/auto-merge").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )

    result = await client.arm_auto_merge(repo_full_name="org/repo", pr_number=42)
    assert (
        "auto-merge enabled" in result.lower() or "branch protection" in result.lower()
    )


@pytest.mark.asyncio
async def test_arm_auto_merge_fetch_error(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(500, text="Error")
    )

    result = await client.arm_auto_merge(repo_full_name="org/repo", pr_number=42)
    assert "Error fetching PR" in result


# ============================================================================
# DirectRepoClient — get_file_content
# ============================================================================


@pytest.mark.asyncio
async def test_get_file_content_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    import base64

    content_b64 = base64.b64encode(b"file content").decode("ascii")
    respx_mock.get("https://api.github.com/repos/org/repo/contents/README.md").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "encoding": "base64",
                    "content": content_b64,
                    "sha": "blob_sha_123",
                }
            ),
        )
    )

    text, sha = await client.get_file_content("org/repo", "README.md")
    assert text == "file content"
    assert sha == "blob_sha_123"


@pytest.mark.asyncio
async def test_get_file_content_with_ref(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    import base64

    content_b64 = base64.b64encode(b"branch content").decode("ascii")
    respx_mock.get(
        "https://api.github.com/repos/org/repo/contents/README.md?ref=develop"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "encoding": "base64",
                    "content": content_b64,
                    "sha": "blob_sha_dev",
                }
            ),
        )
    )

    text, sha = await client.get_file_content("org/repo", "README.md", ref="develop")
    assert text == "branch content"
    assert sha == "blob_sha_dev"


@pytest.mark.asyncio
async def test_get_file_content_directory_raises_value_error(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    # GitHub returns a list for directories
    respx_mock.get("https://api.github.com/repos/org/repo/contents/src").mock(
        return_value=httpx.Response(200, text=json.dumps([]))
    )

    with pytest.raises(ValueError, match="is a directory"):
        await client.get_file_content("org/repo", "src")


@pytest.mark.asyncio
async def test_get_file_content_bad_encoding_raises_runtime_error(
    respx_mock: respx.MockRouter,
) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/contents/file.bin").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"encoding": "utf-8", "content": "not base64", "sha": "sha"}
            ),
        )
    )

    with pytest.raises(RuntimeError, match="Unexpected encoding"):
        await client.get_file_content("org/repo", "file.bin")


@pytest.mark.asyncio
async def test_get_file_content_api_error(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get("https://api.github.com/repos/org/repo/contents/missing.md").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    with pytest.raises(RuntimeError, match="GitHub API GET"):
        await client.get_file_content("org/repo", "missing.md")


# ============================================================================
# DirectRepoClient — push_patched_file
# ============================================================================


@pytest.mark.asyncio
async def test_push_patched_file_success(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    import base64

    # 1. get_file_content
    content_b64 = base64.b64encode(b"original content").decode("ascii")
    respx_mock.get(
        "https://api.github.com/repos/org/repo/contents/file.txt?ref=feature"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"encoding": "base64", "content": content_b64, "sha": "orig_sha"}
            ),
        )
    )

    # 2. push_commit_to_branch steps
    respx_mock.get("https://api.github.com/repos/org/repo/git/ref/heads/feature").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"object": {"sha": "base_sha"}})
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/blobs").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "blob_sha"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/commits/base_sha").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"tree": {"sha": "base_tree_sha"}})
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/trees").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "new_tree_sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/commits").mock(
        return_value=httpx.Response(201, text=json.dumps({"sha": "commit_sha"}))
    )
    respx_mock.patch(
        "https://api.github.com/repos/org/repo/git/refs/heads/feature"
    ).mock(return_value=httpx.Response(200, text="{}"))

    result = await client.push_patched_file(
        repo_full_name="org/repo",
        branch_name="feature",
        file_path="file.txt",
        patch_text="@@ -1 +1 @@\n-original content\n+patched content\n",
        commit_message="apply patch",
        ticket_id="T-1",
    )
    assert "Commit pushed successfully" in result


@pytest.mark.asyncio
async def test_push_patched_file_fetch_error(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    respx_mock.get(
        "https://api.github.com/repos/org/repo/contents/missing.txt?ref=main"
    ).mock(return_value=httpx.Response(404, text="Not Found"))

    result = await client.push_patched_file(
        repo_full_name="org/repo",
        branch_name="main",
        file_path="missing.txt",
        patch_text="dummy patch",
        commit_message="test",
        ticket_id="T-1",
    )
    assert "Error fetching file" in result


@pytest.mark.asyncio
async def test_push_patched_file_patch_error(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    import base64

    content_b64 = base64.b64encode(b"original content").decode("ascii")
    respx_mock.get(
        "https://api.github.com/repos/org/repo/contents/file.txt?ref=main"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"encoding": "base64", "content": content_b64, "sha": "sha"}
            ),
        )
    )

    # Patch that doesn't match → ValueError from apply_patch
    result = await client.push_patched_file(
        repo_full_name="org/repo",
        branch_name="main",
        file_path="file.txt",
        patch_text="@@ -1,1 +1,1 @@\n-wrong original\n+something\n",
        commit_message="test",
        ticket_id="T-1",
    )
    assert "Error applying patch" in result


@pytest.mark.asyncio
async def test_push_patched_file_no_change(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    client = DirectRepoClient(s)

    import base64

    content_b64 = base64.b64encode(b"unchanged").decode("ascii")
    respx_mock.get(
        "https://api.github.com/repos/org/repo/contents/file.txt?ref=main"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"encoding": "base64", "content": content_b64, "sha": "sha"}
            ),
        )
    )

    # A patch that, when applied, produces the same content
    result = await client.push_patched_file(
        repo_full_name="org/repo",
        branch_name="main",
        file_path="file.txt",
        patch_text="",  # empty patch → no change
        commit_message="test",
        ticket_id="T-1",
    )
    assert (
        "no changes" in result.lower()
        or "already in the desired state" in result.lower()
    )

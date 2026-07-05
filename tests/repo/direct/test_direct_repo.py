"""Tests for the direct-repo integration.

:func:`build_direct_repo_tools` and :class:`DirectRepoClient`, with ``respx``
mocked so there are no real network calls.  The installation token cache is
pre-populated in tests so PyJWT is never imported.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings
from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.direct.client import DirectRepoClient

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(**kw: Any) -> DirectRepoSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "github_app_id": "12345",
        "github_app_private_key": "fake-key",  # pragma: allowlist secret
        "github_app_installation_id": "67890",
        "board_api_base_url": "http://127.0.0.1:8077",
    }
    base.update(kw)
    return DirectRepoSettings(**base)


def _prepopulate_installation_token(settings: DirectRepoSettings) -> str:
    """Set the installation token cache so JWT creation is skipped in tests."""
    from robotsix_chat.repo.direct.client import (
        _INSTALLATION_TOKEN_CACHE as _cache,
    )

    token = "ghs_test_installation_token"
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
    # Mock _make_jwt globally for all tests so we never try to import jwt
    from robotsix_chat.repo.direct import client as _client_mod

    monkeypatch.setattr(_client_mod, "_make_jwt", lambda app_id, key: "fake-jwt-token")


# ---------------------------------------------------------------------------
# build_direct_repo_tools
# ---------------------------------------------------------------------------


def test_build_direct_repo_tools_disabled() -> None:
    """Verify that disabled direct_repo returns no tools."""
    assert build_direct_repo_tools(DirectRepoSettings(enabled=False)) == []


def test_build_direct_repo_tools_returns_two_tools() -> None:
    """Verify that enabled direct_repo returns push_branch and open_pr tools."""
    tools = build_direct_repo_tools(_settings())
    assert len(tools) == 2
    names = [t.__name__ for t in tools]
    assert "push_direct_repo_branch" in names
    assert "open_direct_repo_pr" in names


# ---------------------------------------------------------------------------
# BLOCKED-state precondition — push_direct_repo_branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_branch_rejects_non_blocked_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket in DRAFT state → push is refused with a descriptive message."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-1", "state": "draft"})
        )
    )

    tools = build_direct_repo_tools(_settings())
    push_fn = [t for t in tools if t.__name__ == "push_direct_repo_branch"][0]

    out = await push_fn(
        ticket_id="t-1",
        repo_full_name="org/repo",
        branch_name="fix/t-1",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    assert "Refused" in out
    assert "t-1" in out
    assert "draft" in out.lower()
    assert "BLOCKED" in out


@pytest.mark.asyncio
async def test_push_branch_allows_blocked_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket in BLOCKED state → push proceeds (scope guard passes, push runs)."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-1", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    # Catch-all for remaining GitHub API calls during push
    respx_mock.get(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )

    tools = build_direct_repo_tools(settings)
    push_fn = [t for t in tools if t.__name__ == "push_direct_repo_branch"][0]

    out = await push_fn(
        ticket_id="t-1",
        repo_full_name="org/repo",
        branch_name="fix/t-1",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    # Should have attempted the push (we see an error because we returned
    # empty JSON for all GitHub API calls, but the guards passed).
    assert "Error pushing branch" in out or "pushed successfully" in out


# ---------------------------------------------------------------------------
# Dynamic scope resolution — push_direct_repo_branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_branch_rejects_repo_not_in_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → push is refused."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-1", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/other-repo"}]}),
        )
    )

    tools = build_direct_repo_tools(_settings())
    push_fn = [t for t in tools if t.__name__ == "push_direct_repo_branch"][0]

    out = await push_fn(
        ticket_id="t-1",
        repo_full_name="org/repo",
        branch_name="fix/t-1",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    assert "Refused" in out
    assert "org/repo" in out
    assert "scope" in out.lower()


# ---------------------------------------------------------------------------
# BLOCKED-state precondition — open_direct_repo_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_pr_rejects_non_blocked_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket not in BLOCKED → PR open is refused."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-2").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-2", "state": "ready"})
        )
    )

    tools = build_direct_repo_tools(_settings())
    pr_fn = [t for t in tools if t.__name__ == "open_direct_repo_pr"][0]

    out = await pr_fn(
        ticket_id="t-2",
        repo_full_name="org/repo",
        branch_name="fix/t-2",
        title="Fix stuff",
    )
    assert "Refused" in out
    assert "t-2" in out
    assert "ready" in out.lower()
    assert "BLOCKED" in out


@pytest.mark.asyncio
async def test_open_pr_allows_blocked_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket in BLOCKED → PR open proceeds."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-2").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-2", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    # Catch-all for remaining GitHub API calls during PR creation
    respx_mock.get(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )

    tools = build_direct_repo_tools(settings)
    pr_fn = [t for t in tools if t.__name__ == "open_direct_repo_pr"][0]

    out = await pr_fn(
        ticket_id="t-2",
        repo_full_name="org/repo",
        branch_name="fix/t-2",
        title="Fix stuff",
    )
    # Should have attempted the PR (will fail because we return empty JSON)
    assert "Error opening PR" in out or "opened successfully" in out


# ---------------------------------------------------------------------------
# No merge capability
# ---------------------------------------------------------------------------


def test_no_merge_method_on_client() -> None:
    """Verify that DirectRepoClient has NO merge/auto-merge methods."""
    client = DirectRepoClient(_settings())
    public_methods = [
        m for m in dir(client) if not m.startswith("_") and callable(getattr(client, m))
    ]
    merge_related = [m for m in public_methods if "merge" in m.lower()]
    assert merge_related == [], (
        f"DirectRepoClient must not expose merge methods, found: {merge_related}"
    )


def test_no_merge_tool_returned() -> None:
    """Verify that build_direct_repo_tools returns no merge-related tools."""
    tools = build_direct_repo_tools(_settings())
    names = [t.__name__ for t in tools]
    for name in names:
        assert "merge" not in name.lower(), f"Tool '{name}' hints at merge capability"
    # Only push_branch and open_pr
    assert sorted(names) == ["open_direct_repo_pr", "push_direct_repo_branch"]


# ---------------------------------------------------------------------------
# PR human-review gate — verify no auto-merge requested
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pr_does_not_enable_auto_merge(
    respx_mock: respx.MockRouter,
) -> None:
    """create_pr must NOT set auto_merge or merge-related fields in the payload."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    pr_route = respx_mock.post("https://api.github.com/repos/org/repo/pulls").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"html_url": "https://github.com/org/repo/pull/1"}),
        )
    )

    client = DirectRepoClient(settings)
    result = await client.create_pr(
        repo_full_name="org/repo",
        head_branch="fix/t-1",
        title="Fix ticket t-1",
        body="PR body",
    )

    assert "opened successfully" in result
    assert "Auto-merge is NOT enabled" in result

    # Verify the POST payload does NOT include merge-related fields
    post_json = json.loads(pr_route.calls.last.request.content.decode())
    for key in post_json:
        assert "merge" not in key.lower(), f"Merge-related key in PR payload: {key}"
    assert "auto_merge" not in (str(k).lower() for k in post_json)


# ---------------------------------------------------------------------------
# files_json validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_branch_rejects_invalid_files_json(
    respx_mock: respx.MockRouter,
) -> None:
    """Malformed files_json → descriptive error, no API calls beyond guards."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-1", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )

    tools = build_direct_repo_tools(_settings())
    push_fn = [t for t in tools if t.__name__ == "push_direct_repo_branch"][0]

    # Not valid JSON
    out = await push_fn(
        ticket_id="t-1",
        repo_full_name="org/repo",
        branch_name="fix/t-1",
        files_json="not-json",
    )
    assert "Error" in out
    assert "JSON" in out

    # Valid JSON but not an array
    out2 = await push_fn(
        ticket_id="t-1",
        repo_full_name="org/repo",
        branch_name="fix/t-1",
        files_json=json.dumps({"path": "x.py"}),
    )
    assert "Error" in out2
    assert "JSON array" in out2


# ---------------------------------------------------------------------------
# get_ticket_state error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticket_state_returns_none_on_error(
    respx_mock: respx.MockRouter,
) -> None:
    """When the board API returns an error, get_ticket_state returns None."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-err").mock(
        return_value=httpx.Response(500, text="Board API error 500: boom")
    )

    tools = build_direct_repo_tools(_settings())
    push_fn = [t for t in tools if t.__name__ == "push_direct_repo_branch"][0]

    out = await push_fn(
        ticket_id="t-err",
        repo_full_name="org/repo",
        branch_name="fix/t-err",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    assert "could not determine state" in out.lower()
    assert "t-err" in out


# ---------------------------------------------------------------------------
# PR body defaults when body not provided
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_pr_default_body_links_ticket_id(
    respx_mock: respx.MockRouter,
) -> None:
    """When no body is provided, the tool generates one referencing the ticket."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-3").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-3", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    pr_route = respx_mock.post("https://api.github.com/repos/org/repo/pulls").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"html_url": "https://github.com/org/repo/pull/1"}),
        )
    )

    tools = build_direct_repo_tools(settings)
    pr_fn = [t for t in tools if t.__name__ == "open_direct_repo_pr"][0]

    await pr_fn(
        ticket_id="t-3",
        repo_full_name="org/repo",
        branch_name="fix/t-3",
        title="Fix blocked ticket",
        body="",  # empty → default generated
    )

    body = json.loads(pr_route.calls.last.request.content.decode()).get("body", "")
    assert "t-3" in body
    assert "human review required" in body.lower()


# ---------------------------------------------------------------------------
# Branch naming traceability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_branch_uses_ticket_id_in_commit(
    respx_mock: respx.MockRouter,
) -> None:
    """Commit message references the ticket id even when commit_message not given."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-4").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-4", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/ref/heads/main").mock(
        return_value=httpx.Response(200, text=json.dumps({"object": {"sha": "abc123"}}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/blobs").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "blob-sha"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/commits/abc123").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"sha": "commit-sha", "tree": {"sha": "tree-sha"}}),
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/trees").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "tree-sha"}))
    )
    commit_route = respx_mock.post(
        "https://api.github.com/repos/org/repo/git/commits"
    ).mock(return_value=httpx.Response(200, text=json.dumps({"sha": "commit-sha"})))
    respx_mock.post("https://api.github.com/repos/org/repo/git/refs").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"ref": "refs/heads/fix/t-4"}),
        )
    )

    tools = build_direct_repo_tools(settings)
    push_fn = [t for t in tools if t.__name__ == "push_direct_repo_branch"][0]

    await push_fn(
        ticket_id="t-4",
        repo_full_name="org/repo",
        branch_name="fix/t-4",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
        commit_message="",  # empty → default
    )

    assert commit_route.called
    commit_msg = json.loads(commit_route.calls.last.request.content.decode()).get(
        "message", ""
    )
    assert "t-4" in commit_msg


# ---------------------------------------------------------------------------
# Changelog fragment trailing newline normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_branch_ensures_changelog_fragment_trailing_newline(
    respx_mock: respx.MockRouter,
) -> None:
    """changelog.d/*.md files without trailing newline get one appended."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-cl").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-cl", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/ref/heads/main").mock(
        return_value=httpx.Response(200, text=json.dumps({"object": {"sha": "abc123"}}))
    )

    # Capture the blob POST to inspect the content
    blob_calls: list[dict[str, Any]] = []
    async def _capture_blob(request: httpx.Request) -> httpx.Response:
        blob_calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, text=json.dumps({"sha": "blob-sha"}))

    respx_mock.post("https://api.github.com/repos/org/repo/git/blobs").mock(
        side_effect=_capture_blob
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/commits/abc123").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"sha": "commit-sha", "tree": {"sha": "tree-sha"}}),
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/trees").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "tree-sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/commits").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "commit-sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/refs").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"ref": "refs/heads/fix/t-cl"}),
        )
    )

    tools = build_direct_repo_tools(settings)
    push_fn = [t for t in tools if t.__name__ == "push_direct_repo_branch"][0]

    await push_fn(
        ticket_id="t-cl",
        repo_full_name="org/repo",
        branch_name="fix/t-cl",
        files_json=json.dumps(
            [
                {
                    "path": "changelog.d/t-cl.misc.md",
                    "content": "Fixed a thing",  # no trailing newline
                },
                {
                    "path": "src/foo.py",
                    "content": "print(1)",  # not a changelog fragment
                },
            ]
        ),
    )

    assert len(blob_calls) == 2
    blob_by_path = {c["path"]: c["content"] for c in blob_calls}
    assert blob_by_path["changelog.d/t-cl.misc.md"] == "Fixed a thing\n"
    assert blob_by_path["src/foo.py"] == "print(1)"  # unchanged


@pytest.mark.asyncio
async def test_push_branch_preserves_existing_trailing_newline_in_changelog(
    respx_mock: respx.MockRouter,
) -> None:
    """changelog.d/*.md files that already end with \\n are not double-terminated."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-cl2").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-cl2", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/ref/heads/main").mock(
        return_value=httpx.Response(200, text=json.dumps({"object": {"sha": "abc123"}}))
    )

    blob_calls: list[dict[str, Any]] = []
    async def _capture_blob(request: httpx.Request) -> httpx.Response:
        blob_calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, text=json.dumps({"sha": "blob-sha"}))

    respx_mock.post("https://api.github.com/repos/org/repo/git/blobs").mock(
        side_effect=_capture_blob
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/commits/abc123").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"sha": "commit-sha", "tree": {"sha": "tree-sha"}}),
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/trees").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "tree-sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/commits").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "commit-sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/refs").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"ref": "refs/heads/fix/t-cl2"}),
        )
    )

    tools = build_direct_repo_tools(settings)
    push_fn = [t for t in tools if t.__name__ == "push_direct_repo_branch"][0]

    await push_fn(
        ticket_id="t-cl2",
        repo_full_name="org/repo",
        branch_name="fix/t-cl2",
        files_json=json.dumps(
            [
                {
                    "path": "changelog.d/t-cl2.feature.md",
                    "content": "Added a feature\n",  # already has trailing newline
                },
            ]
        ),
    )

    assert len(blob_calls) == 1
    assert blob_calls[0]["content"] == "Added a feature\n"


@pytest.mark.asyncio
async def test_push_branch_ignores_non_md_files_in_changelog_dir(
    respx_mock: respx.MockRouter,
) -> None:
    """Only .md files in changelog.d/ get newline normalization."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-cl3").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-cl3", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/ref/heads/main").mock(
        return_value=httpx.Response(200, text=json.dumps({"object": {"sha": "abc123"}}))
    )

    blob_calls: list[dict[str, Any]] = []
    async def _capture_blob(request: httpx.Request) -> httpx.Response:
        blob_calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, text=json.dumps({"sha": "blob-sha"}))

    respx_mock.post("https://api.github.com/repos/org/repo/git/blobs").mock(
        side_effect=_capture_blob
    )
    respx_mock.get("https://api.github.com/repos/org/repo/git/commits/abc123").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"sha": "commit-sha", "tree": {"sha": "tree-sha"}}),
        )
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/trees").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "tree-sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/commits").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "commit-sha"}))
    )
    respx_mock.post("https://api.github.com/repos/org/repo/git/refs").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"ref": "refs/heads/fix/t-cl3"}),
        )
    )

    tools = build_direct_repo_tools(settings)
    push_fn = [t for t in tools if t.__name__ == "push_direct_repo_branch"][0]

    await push_fn(
        ticket_id="t-cl3",
        repo_full_name="org/repo",
        branch_name="fix/t-cl3",
        files_json=json.dumps(
            [
                {
                    "path": "changelog.d/README.txt",
                    "content": "Instructions",  # not .md — no normalization
                },
                {
                    "path": "docs/changelog.md",
                    "content": "Not in changelog.d/",  # wrong dir — no normalization
                },
            ]
        ),
    )

    assert len(blob_calls) == 2
    blob_by_path = {c["path"]: c["content"] for c in blob_calls}
    assert blob_by_path["changelog.d/README.txt"] == "Instructions"
    assert blob_by_path["docs/changelog.md"] == "Not in changelog.d/"


# ---------------------------------------------------------------------------
# Dynamic scope resolution — coverage of the client method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_installation_repos_parses_response(
    respx_mock: respx.MockRouter,
) -> None:
    """list_installation_repos returns full_names from the API response."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "repositories": [
                        {"full_name": "org/repo-a"},
                        {"full_name": "org/repo-b"},
                    ]
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    repos = await client.list_installation_repos()
    assert repos == ["org/repo-a", "org/repo-b"]


# ---------------------------------------------------------------------------
# Tool docstrings — verify no merge-related language
# ---------------------------------------------------------------------------


def test_tool_docstrings_forbid_merge() -> None:
    """Tool docstrings must not suggest merge capability, only deny it."""
    tools = build_direct_repo_tools(_settings())
    for tool in tools:
        doc = (tool.__doc__ or "").lower()
        # Must not suggest merge as a capability
        assert "force-push" not in doc, (
            f"Tool {tool.__name__} docstring mentions 'force-push'"
        )
        # Must mention the BLOCKED guardrail
        assert "blocked" in doc, (
            f"Tool {tool.__name__} docstring missing BLOCKED mention"
        )
        # If "merge" appears it must be in a denial context
        if "merge" in doc:
            assert "not" in doc or "no" in doc, (
                f"Tool {tool.__name__} docstring"
                " mentions 'merge' outside denial context"
            )

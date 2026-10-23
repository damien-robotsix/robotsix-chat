"""Fix and commit-flow tests for the direct-repo integration.

Covers tool registration, branch/commit pushes, ``direct_fix``,
implement-cycle counting, the implement spawn-counter reset, ticket state
and installation-token handling, with ``respx`` mocked so there are no real
network calls.
``robotsix_github_auth`` is mocked by the autouse fixture in ``conftest.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings
from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.direct.client import (
    DirectRepoClient,
    _count_cycles_from_data,
)
from tests.repo.direct.conftest import (
    _mock_component_request_blocked,
    _mock_component_request_error,
    _prepopulate_installation_token,
    _settings,
)

# ---------------------------------------------------------------------------
# build_direct_repo_tools
# ---------------------------------------------------------------------------


def test_build_direct_repo_tools_disabled() -> None:
    """Verify that disabled direct_repo returns no tools."""
    assert build_direct_repo_tools(DirectRepoSettings(enabled=False)) == []


def test_build_direct_repo_tools_returns_twentytwo_tools() -> None:
    """Verify that enabled direct_repo returns the twenty-five expected tools."""
    tools = build_direct_repo_tools(_settings())
    assert len(tools) == 27
    names = [t.__name__ for t in tools]
    assert "push_direct_repo_branch" in names
    assert "open_direct_repo_pr" in names
    assert "open_simple_repo_pr" in names
    assert "update_simple_repo_pr" in names
    assert "update_pr_branch" in names
    assert "check_pr_merge_conflict" in names
    assert "verify_pr_ci_status" in names
    assert "inspect_pr_diff" in names
    assert "check_ci_health" in names
    assert "rerun_ci_workflow" in names
    assert "fetch_ci_job_logs" in names
    assert "fetch_trivy_findings" in names
    assert "file_ci_stabilization_ticket" in names
    assert "recover_auto_merge" in names
    assert "check_direct_repo_auto_merge" in names
    assert "list_open_prs" in names
    assert "merge_direct_repo_pr" in names
    assert "close_direct_repo_pr" in names
    assert "arm_direct_repo_auto_merge" in names
    assert "enable_repo_pages" in names
    assert "reset_implement_spawn_counter" in names
    assert "apply_patch_to_file" in names
    assert "push_patch_to_pr_branch" in names
    assert "resolve_pr_conflict" in names
    assert "inspect_github_installation_token" in names


# ---------------------------------------------------------------------------
# BLOCKED-state precondition — push_direct_repo_branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_branch_rejects_non_blocked_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket in DRAFT state → push is refused with a descriptive message."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
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

    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-1", "state": "blocked"})
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
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
    _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-1", "state": "blocked"})
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
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
    assert "not installed" in out.lower()
    assert "org/repo" in out
    assert "install" in out.lower()


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
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
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
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
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
# inspect_github_installation_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_github_installation_token_reports_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The diagnostic tool reports expiry and the effective permission map."""

    async def _fake_diagnostics(self: Any, repo_full_name: str) -> dict[str, Any]:
        return {
            "app_id": "12345",
            "configured_installation_id": "67890",
            "resolved_installation_id": "67890",
            "expires_at": "2030-01-02T03:04:05+00:00",
            "seconds_remaining": 12345.6,
            "permissions": {"pages": "write", "contents": "read"},
        }

    monkeypatch.setattr(
        "robotsix_chat.repo.direct.client."
        "DirectRepoClient.get_installation_token_diagnostics",
        _fake_diagnostics,
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "inspect_github_installation_token"][0]

    out = await fn("org/repo")
    assert "GitHub App installation token diagnostic for `org/repo`" in out
    assert "App id: `12345`" in out
    assert "Configured installation id: `67890`" in out
    assert "Resolved installation id (for `org/repo`): `67890`" in out
    assert "Token expires at: `2030-01-02T03:04:05+00:00` (UTC)" in out
    assert "Seconds remaining: 12345.6" in out
    assert "`pages`: `write`" in out
    assert "`contents`: `read`" in out
    assert "Mismatch" not in out


@pytest.mark.asyncio
async def test_inspect_github_installation_token_reports_id_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved/configured installation id mismatch is called out explicitly."""

    async def _fake_diagnostics(self: Any, repo_full_name: str) -> dict[str, Any]:
        return {
            "app_id": "12345",
            "configured_installation_id": "67890",
            "resolved_installation_id": "99999",
            "expires_at": "2030-01-02T03:04:05+00:00",
            "seconds_remaining": 12345.6,
            "permissions": {"pages": "write"},
        }

    monkeypatch.setattr(
        "robotsix_chat.repo.direct.client."
        "DirectRepoClient.get_installation_token_diagnostics",
        _fake_diagnostics,
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "inspect_github_installation_token"][0]

    out = await fn("org/repo")
    assert "Resolved installation id (for `org/repo`): `99999`" in out
    assert "Mismatch" in out


@pytest.mark.asyncio
async def test_inspect_github_installation_token_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostic failures are surfaced as an error message, not raised."""

    async def _raise(self: Any, repo_full_name: str) -> dict[str, Any]:
        raise RuntimeError("mint failed")

    monkeypatch.setattr(
        "robotsix_chat.repo.direct.client."
        "DirectRepoClient.get_installation_token_diagnostics",
        _raise,
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "inspect_github_installation_token"][0]

    out = await fn("org/repo")
    assert "Error inspecting installation token for org/repo: mint failed" in out


# ---------------------------------------------------------------------------
# Branch naming traceability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_branch_uses_ticket_id_in_commit(
    respx_mock: respx.MockRouter,
) -> None:
    """Commit message references the ticket id even when commit_message not given."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-4").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-4", "state": "blocked"})
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
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

    respx_mock.get("http://127.0.0.1:8077/tickets/t-cl").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-cl", "state": "blocked"})
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
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
    contents = {c["content"] for c in blob_calls}
    assert "Fixed a thing\n" in contents
    assert "print(1)" in contents  # unchanged


@pytest.mark.asyncio
async def test_push_branch_preserves_existing_trailing_newline_in_changelog(
    respx_mock: respx.MockRouter,
) -> None:
    r"""changelog.d/*.md files that already end with \n are not double-terminated."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-cl2").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-cl2", "state": "blocked"})
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
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

    respx_mock.get("http://127.0.0.1:8077/tickets/t-cl3").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-cl3", "state": "blocked"})
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
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
    contents = {c["content"] for c in blob_calls}
    assert "Instructions" in contents
    assert "Not in changelog.d/" in contents


# ---------------------------------------------------------------------------
# Dynamic scope resolution — coverage of the client method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_installation_repos_parses_response(
    respx_mock: respx.MockRouter,
) -> None:
    """list_installation_repos returns full_names from the API response."""
    settings = _settings()

    respx_mock.get(
        "https://api.github.com/installation/repositories?per_page=100&page=1"
    ).mock(
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


@pytest.mark.asyncio
async def test_list_installation_repos_paginates(
    respx_mock: respx.MockRouter,
) -> None:
    """list_installation_repos follows pages and returns all repos."""
    settings = _settings()

    # Simulate a full first page (100 repos, triggers another request)
    # and a partial second page (35 repos, which stops the loop).
    page_1_repos = [{"full_name": f"org/repo-{i}"} for i in range(100)]
    page_2_repos = [{"full_name": f"org/repo-{i}"} for i in range(100, 135)]

    respx_mock.get(
        "https://api.github.com/installation/repositories?per_page=100&page=1"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": page_1_repos}),
        )
    )
    respx_mock.get(
        "https://api.github.com/installation/repositories?per_page=100&page=2"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": page_2_repos}),
        )
    )

    client = DirectRepoClient(settings)
    repos = await client.list_installation_repos()

    expected = [f"org/repo-{i}" for i in range(135)]
    assert repos == expected


# ---------------------------------------------------------------------------
# Tool docstrings — verify no merge-related language
# ---------------------------------------------------------------------------


def test_tool_docstrings_forbid_merge() -> None:
    """Tool docstrings must not suggest merge capability.

    Only denial or descriptive checking of state is allowed.
    Descriptive uses of "merge" (e.g. "merge conflicts",
    "mergeable") are fine — they describe state, not a merge action.

    The merge_direct_repo_pr and arm_direct_repo_auto_merge tools are
    the exception — they are confirmation-gated merge tools that do not
    require BLOCKED state (they are follow-up operations on already-created
    PRs).
    """
    merge_tool_names = {
        "merge_direct_repo_pr",
        "arm_direct_repo_auto_merge",
        "close_direct_repo_pr",
    }
    # Read-only tools that don't require BLOCKED state
    readonly_tool_names = {
        "check_direct_repo_auto_merge",
        "list_open_prs",
        "inspect_github_installation_token",
    }

    tools = build_direct_repo_tools(_settings())
    for tool in tools:
        doc = (tool.__doc__ or "").lower()
        # Must not suggest force-push as a capability
        assert "force-push" not in doc, (
            f"Tool {tool.__name__} docstring mentions 'force-push'"
        )
        if tool.__name__ in merge_tool_names | readonly_tool_names:
            continue
        # Must mention the BLOCKED guardrail
        assert "blocked" in doc, (
            f"Tool {tool.__name__} docstring missing BLOCKED mention"
        )
        # If "merge" appears in other tools it must be descriptive only.
        performative = ("perform merge", "execute merge", "merge pr", "merge pull")
        if "merge" in doc:
            assert not any(p in doc for p in performative), (
                f"Tool {tool.__name__} docstring uses performative merge language"
            )


# ============================================================================
# reset_implement_spawn_counter
# ============================================================================


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_success(
    respx_mock: respx.MockRouter,
) -> None:
    """Successful POST resume-blocked → tool returns success message."""
    respx_mock.post("http://127.0.0.1:8077/tickets/t-reset/resume-blocked").mock(
        return_value=httpx.Response(200)
    )

    tools = build_direct_repo_tools(_settings())
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-reset")
    assert "reset" in out
    assert "t-reset" in out
    assert "Error" not in out


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_failure(
    respx_mock: respx.MockRouter,
) -> None:
    """Board API error → tool returns error message."""
    respx_mock.post("http://127.0.0.1:8077/tickets/t-bad/resume-blocked").mock(
        return_value=httpx.Response(500)
    )

    tools = build_direct_repo_tools(_settings())
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-bad")
    assert "Error" in out
    assert "t-bad" in out


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_roster_first_success() -> None:
    """When component_request is available, use roster path first."""

    async def _mock_cr(
        component: str, method: str, path: str, json_body: Any = None
    ) -> str:
        return "HTTP 200 OK"

    tools = build_direct_repo_tools(_settings(), component_request=_mock_cr)
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-roster-reset")
    assert "reset" in out
    assert "t-roster-reset" in out
    assert "roster path" in out


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_roster_fails_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """When roster path fails, fall back to direct board API."""

    async def _mock_cr(
        component: str, method: str, path: str, json_body: Any = None
    ) -> str:
        return "Error: connection refused"

    respx_mock.post("http://127.0.0.1:8077/tickets/t-fb/resume-blocked").mock(
        return_value=httpx.Response(200)
    )

    tools = build_direct_repo_tools(_settings(), component_request=_mock_cr)
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-fb")
    assert "reset" in out
    assert "t-fb" in out
    assert "Error" not in out


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_roster_non_2xx_falls_back(
    respx_mock: respx.MockRouter,
) -> None:
    """When roster returns non-2xx, fall back to direct."""

    async def _mock_cr(
        component: str, method: str, path: str, json_body: Any = None
    ) -> str:
        return "HTTP 502 Bad Gateway"

    respx_mock.post("http://127.0.0.1:8077/tickets/t-502/resume-blocked").mock(
        return_value=httpx.Response(200)
    )

    tools = build_direct_repo_tools(_settings(), component_request=_mock_cr)
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-502")
    assert "reset" in out
    assert "t-502" in out
    assert "Error" not in out


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_no_component_request_uses_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """Without component_request, uses direct path only (regression)."""
    respx_mock.post("http://127.0.0.1:8077/tickets/t-direct/resume-blocked").mock(
        return_value=httpx.Response(200)
    )

    tools = build_direct_repo_tools(_settings())  # component_request=None
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-direct")
    assert "reset" in out
    assert "t-direct" in out
    assert "Error" not in out


# ============================================================================
# direct_fix
# ============================================================================


def test_direct_fix_not_available_by_default() -> None:
    """direct_fix is not in the tool list when direct_fix_enabled is False."""
    tools = build_direct_repo_tools(_settings())
    names = [t.__name__ for t in tools]
    assert "direct_fix" not in names


def test_direct_fix_available_when_enabled() -> None:
    """direct_fix is in the tool list when direct_fix_enabled is True."""
    tools = build_direct_repo_tools(_settings(direct_fix_enabled=True))
    names = [t.__name__ for t in tools]
    assert "direct_fix" in names
    assert len(tools) == 29  # 27 base + direct_fix + patch_direct_repo_file


@pytest.mark.asyncio
async def test_direct_fix_rejects_non_blocked_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket not in BLOCKED → direct_fix is refused."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-df1").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-df1", "state": "draft"})
        )
    )

    tools = build_direct_repo_tools(_settings(direct_fix_enabled=True))
    df_fn = [t for t in tools if t.__name__ == "direct_fix"][0]

    out = await df_fn(
        ticket_id="t-df1",
        repo_full_name="org/repo",
        target_branch="main",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    assert "Refused" in out
    assert "BLOCKED" in out


@pytest.mark.asyncio
async def test_direct_fix_rejects_few_cycles(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket has <3 implement cycles → direct_fix is refused."""
    settings = _settings(direct_fix_enabled=True)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-df2").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-df2",
                    "state": "blocked",
                    "events": [
                        {"type": "implement_start", "timestamp": "..."},
                    ],
                }
            ),
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )

    tools = build_direct_repo_tools(settings)
    df_fn = [t for t in tools if t.__name__ == "direct_fix"][0]

    out = await df_fn(
        ticket_id="t-df2",
        repo_full_name="org/repo",
        target_branch="main",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    assert "Refused" in out
    assert "implement" in out.lower()
    assert "1" in out  # cycle count


@pytest.mark.asyncio
async def test_direct_fix_rejects_zero_cycles(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket has 0 completed implement cycles → direct_fix is refused.

    Regression test: the precondition lookup must correctly count 0 cycles
    (empty or non-implement events) and refuse with a distinct message from
    the API-unreachable error.
    """
    settings = _settings(direct_fix_enabled=True)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-df2z").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-df2z",
                    "state": "blocked",
                    "events": [
                        {"type": "ticket_created", "timestamp": "..."},
                    ],
                }
            ),
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )

    tools = build_direct_repo_tools(settings)
    df_fn = [t for t in tools if t.__name__ == "direct_fix"][0]

    out = await df_fn(
        ticket_id="t-df2z",
        repo_full_name="org/repo",
        target_branch="main",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    assert "Refused" in out
    assert "0" in out  # 0 cycles in the error message
    assert "implement" in out.lower()


@pytest.mark.asyncio
async def test_direct_fix_allows_enough_cycles(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket has ≥3 implement cycles → direct_fix proceeds."""
    settings = _settings(direct_fix_enabled=True)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-df3").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-df3",
                    "state": "blocked",
                    "events": [
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                    ],
                }
            ),
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
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
    respx_mock.patch(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )

    tools = build_direct_repo_tools(settings)
    df_fn = [t for t in tools if t.__name__ == "direct_fix"][0]

    out = await df_fn(
        ticket_id="t-df3",
        repo_full_name="org/repo",
        target_branch="main",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    # Should have attempted the push
    assert "Error pushing commit" in out or "pushed successfully" in out


@pytest.mark.asyncio
async def test_direct_fix_rejects_out_of_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → direct_fix is refused."""
    settings = _settings(direct_fix_enabled=True)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-df4").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-df4",
                    "state": "blocked",
                    "events": [
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                    ],
                }
            ),
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/other-repo"}]}),
        )
    )

    tools = build_direct_repo_tools(settings)
    df_fn = [t for t in tools if t.__name__ == "direct_fix"][0]

    out = await df_fn(
        ticket_id="t-df4",
        repo_full_name="org/repo",
        target_branch="main",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    assert "not installed" in out.lower()
    assert "install" in out.lower()


@pytest.mark.asyncio
async def test_direct_fix_uses_ticket_id_in_commit_message(
    respx_mock: respx.MockRouter,
) -> None:
    """Default commit message references the ticket id and cycle count."""
    settings = _settings(direct_fix_enabled=True)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-df5").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-df5",
                    "state": "blocked",
                    "events": [
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                    ],
                }
            ),
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
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
    respx_mock.patch("https://api.github.com/repos/org/repo/git/refs/heads/main").mock(
        return_value=httpx.Response(200, text=json.dumps({"ref": "refs/heads/main"}))
    )

    tools = build_direct_repo_tools(settings)
    df_fn = [t for t in tools if t.__name__ == "direct_fix"][0]

    await df_fn(
        ticket_id="t-df5",
        repo_full_name="org/repo",
        target_branch="main",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
        commit_message="",  # empty → default
    )

    assert commit_route.called
    commit_msg = json.loads(commit_route.calls.last.request.content.decode()).get(
        "message", ""
    )
    assert "t-df5" in commit_msg
    assert "direct fix" in commit_msg.lower()
    assert "implement" in commit_msg.lower()


@pytest.mark.asyncio
async def test_direct_fix_pushes_to_target_branch(
    respx_mock: respx.MockRouter,
) -> None:
    """direct_fix updates the existing branch ref, does not create a new one."""
    settings = _settings(direct_fix_enabled=True)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-df6").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-df6",
                    "state": "blocked",
                    "events": [
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                    ],
                }
            ),
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
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
    respx_mock.post("https://api.github.com/repos/org/repo/git/commits").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "commit-sha"}))
    )
    # Verify PATCH (update ref) is called, not POST (create ref)
    patch_route = respx_mock.patch(
        "https://api.github.com/repos/org/repo/git/refs/heads/main"
    ).mock(
        return_value=httpx.Response(200, text=json.dumps({"ref": "refs/heads/main"}))
    )
    # Should NOT call POST to create a new ref
    respx_mock.post("https://api.github.com/repos/org/repo/git/refs").mock(
        return_value=httpx.Response(200, text="{}")
    )

    tools = build_direct_repo_tools(settings)
    df_fn = [t for t in tools if t.__name__ == "direct_fix"][0]

    out = await df_fn(
        ticket_id="t-df6",
        repo_full_name="org/repo",
        target_branch="main",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )

    assert "pushed successfully" in out
    assert patch_route.called
    # Verify the PATCH payload includes force=False (not a force push)
    patch_body = json.loads(patch_route.calls.last.request.content.decode())
    assert patch_body.get("force") is False


# ---------------------------------------------------------------------------
# get_ticket_data / count_implement_cycles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticket_data_returns_full_json(
    respx_mock: respx.MockRouter,
) -> None:
    """get_ticket_data returns the full ticket JSON."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-full").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-full",
                    "state": "blocked",
                    "title": "Fix stuff",
                    "events": [{"type": "implement_start"}],
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    data = await client.get_ticket_data("t-full")
    assert data is not None
    assert data["id"] == "t-full"
    assert data["state"] == "blocked"


@pytest.mark.asyncio
async def test_get_ticket_data_returns_none_on_error(
    respx_mock: respx.MockRouter,
) -> None:
    """get_ticket_data returns None when the board API errors."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-err2").mock(
        return_value=httpx.Response(500, text="boom")
    )

    client = DirectRepoClient(settings)
    data = await client.get_ticket_data("t-err2")
    assert data is None


@pytest.mark.asyncio
async def test_count_implement_cycles_from_events(
    respx_mock: respx.MockRouter,
) -> None:
    """count_implement_cycles counts events with 'implement' in type/action."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-cycles").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-cycles",
                    "state": "blocked",
                    "events": [
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "review"},
                        {"type": "implement_start"},
                    ],
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    cycles = await client.count_implement_cycles("t-cycles")
    assert cycles == 5  # 3 starts + 2 completes


@pytest.mark.asyncio
async def test_count_implement_cycles_fallback_history(
    respx_mock: respx.MockRouter,
) -> None:
    """count_implement_cycles falls back to history when no events array."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-hist").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-hist",
                    "state": "blocked",
                    "history": [
                        {"state": "ready"},
                        {"state": "implement_complete"},
                        {"state": "in_progress"},
                        {"state": "implement_complete"},
                    ],
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    cycles = await client.count_implement_cycles("t-hist")
    assert cycles == 2


@pytest.mark.asyncio
async def test_count_implement_cycles_fallback_direct_field(
    respx_mock: respx.MockRouter,
) -> None:
    """count_implement_cycles falls back to cycle_count field."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-count").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-count",
                    "state": "blocked",
                    "cycle_count": 5,
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    cycles = await client.count_implement_cycles("t-count")
    assert cycles == 5


@pytest.mark.asyncio
async def test_count_implement_cycles_no_data_returns_zero(
    respx_mock: respx.MockRouter,
) -> None:
    """count_implement_cycles returns 0 when no events/history/cycle_count."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-nodata").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"id": "t-nodata", "state": "blocked"}),
        )
    )

    client = DirectRepoClient(settings)
    cycles = await client.count_implement_cycles("t-nodata")
    assert cycles == 0


def test_count_cycles_from_data_resume_unblock_events() -> None:
    """Resume/unblock events count as 3 cycles each (exhaustion heuristic)."""
    data = {
        "state": "blocked",
        "events": [
            {"type": "resume"},
            {"type": "implement_start"},
            {"type": "unblock"},
        ],
    }
    cycles = _count_cycles_from_data(data)
    # 1 resume event (+3) + 1 implement event (+1) + 1 unblock event (+3) = 7
    assert cycles == 7


def test_count_cycles_from_data_resume_event_alone_satisfies_gate() -> None:
    """A single resume event alone counts as 3 — enough to pass the ≥3 gate."""
    data = {"state": "blocked", "events": [{"type": "resume"}]}
    cycles = _count_cycles_from_data(data)
    assert cycles == 3


def test_count_cycles_from_data_unblock_event_alone_satisfies_gate() -> None:
    """A single unblock event alone counts as 3 — enough to pass the ≥3 gate."""
    data = {"state": "blocked", "events": [{"type": "unblock"}]}
    cycles = _count_cycles_from_data(data)
    assert cycles == 3


def test_count_cycles_from_data_mixed_resume_and_implement() -> None:
    """Resume events (+3 each) and implement events (+1 each) sum correctly."""
    data = {
        "state": "blocked",
        "events": [
            {"type": "implement_start"},
            {"type": "implement_complete"},
            {"type": "resume"},
            {"type": "implement_start"},
            {"type": "implement_complete"},
            {"type": "implement_start"},
            {"type": "implement_complete"},
        ],
    }
    cycles = _count_cycles_from_data(data)
    # 6 implement events (+6) + 1 resume event (+3) = 9
    assert cycles == 9


# ---------------------------------------------------------------------------
# 401 token-expiry retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_401_triggers_token_refresh_and_retry(
    respx_mock: respx.MockRouter,
) -> None:
    """When GitHub returns 401 the client refreshes the token and retries once."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    # The installation repos endpoint: first call → 401, second → 200
    repos_route = respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        side_effect=[
            httpx.Response(401, text=json.dumps({"message": "Bad credentials"})),
            httpx.Response(
                200,
                text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
            ),
        ]
    )

    # Token exchange endpoint: returns a fresh token
    respx_mock.post(
        "https://api.github.com/app/installations/67890/access_tokens"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"token": "ghs_fresh_token_after_401"}
            ),  # pragma: allowlist secret
        )
    )

    client = DirectRepoClient(settings)
    repos = await client.list_installation_repos()

    assert repos == ["org/repo"]
    assert repos_route.call_count == 2


@pytest.mark.asyncio
async def test_github_401_retry_fails_on_second_401(
    respx_mock: respx.MockRouter,
) -> None:
    """When GitHub returns 401 twice the client does not retry a third time."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    repos_route = respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            401, text=json.dumps({"message": "Bad credentials"})
        )
    )

    # Token exchange still works
    respx_mock.post(
        "https://api.github.com/app/installations/67890/access_tokens"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"token": "ghs_fresh_token_after_401"}
            ),  # pragma: allowlist secret
        )
    )

    client = DirectRepoClient(settings)
    with pytest.raises(RuntimeError, match="GitHub API GET"):
        await client.list_installation_repos()

    # Two calls: initial + one retry
    assert repos_route.call_count == 2


# ---------------------------------------------------------------------------
# push_commit_to_branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_commit_to_branch_updates_ref(
    respx_mock: respx.MockRouter,
) -> None:
    """push_commit_to_branch creates commit and updates existing ref via PATCH."""
    settings = _settings()

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
    respx_mock.post("https://api.github.com/repos/org/repo/git/commits").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "new-commit-sha"}))
    )
    patch_route = respx_mock.patch(
        "https://api.github.com/repos/org/repo/git/refs/heads/main"
    ).mock(
        return_value=httpx.Response(200, text=json.dumps({"ref": "refs/heads/main"}))
    )

    client = DirectRepoClient(settings)
    result = await client.push_commit_to_branch(
        repo_full_name="org/repo",
        branch_name="main",
        files=[{"path": "x.py", "content": "print(1)"}],
        commit_message="fix: direct fix",
        ticket_id="t-1",
    )

    assert "pushed successfully" in result
    assert "new-commit-sha" in result
    assert patch_route.called
    patch_body = json.loads(patch_route.calls.last.request.content.decode())
    assert patch_body["sha"] == "new-commit-sha"
    assert patch_body["force"] is False


# ---------------------------------------------------------------------------
# Scope-check bypass — mill pipeline credential (component_request) available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_branch_bypasses_scope_when_component_request_available(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo NOT in installation scope, but component_request is available.

    The scope check is skipped and the push proceeds.
    """
    settings = _settings()

    # Scope check would reject: only org/other-repo is installed
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/other-repo"}]}),
        )
    )
    # Catch-all for remaining GitHub API calls during push
    respx_mock.get(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )

    tools = build_direct_repo_tools(
        settings, component_request=_mock_component_request_blocked
    )
    push_fn = [t for t in tools if t.__name__ == "push_direct_repo_branch"][0]

    out = await push_fn(
        ticket_id="t-1",
        repo_full_name="org/repo",
        branch_name="fix/t-1",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    # Scope check must NOT have blocked the push — the result
    # should come from the GitHub push attempt, not from the scope guard.
    assert "not installed" not in out.lower()
    assert "Error pushing branch" in out or "pushed successfully" in out


@pytest.mark.asyncio
async def test_direct_fix_bypasses_scope_when_component_request_available(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo NOT in installation scope, but component_request is available.

    The scope check is skipped and direct_fix proceeds.
    """
    settings = _settings(direct_fix_enabled=True)

    # Scope check would reject: only org/other-repo is installed
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/other-repo"}]}),
        )
    )
    # Ticket with ≥3 implement cycles (required for direct_fix)
    respx_mock.get("http://127.0.0.1:8077/tickets/t-df").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-df",
                    "state": "blocked",
                    "events": [
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                    ],
                }
            ),
        )
    )
    # Catch-all for remaining GitHub API calls
    respx_mock.get(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.patch(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )

    tools = build_direct_repo_tools(
        settings, component_request=_mock_component_request_blocked
    )
    df_fn = [t for t in tools if t.__name__ == "direct_fix"][0]

    out = await df_fn(
        ticket_id="t-df",
        repo_full_name="org/repo",
        target_branch="main",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    # Scope check must NOT have blocked the push.
    assert "not installed" not in out.lower()
    # Should have attempted the push (we see an error because we returned
    # empty JSON for all GitHub API calls, but the guards passed).
    assert "Error pushing commit" in out or "pushed successfully" in out


# ---------------------------------------------------------------------------
# Roster-to-direct fallback — _check_blocked_exhausted with failing
# component_request exercises the fallback to board.get_ticket_data.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_fix_roster_fails_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """When the roster path returns 'Error:', direct_fix falls back to board API.

    The roster-based component_request returns a retryable 'Error:' string;
    _check_blocked_exhausted falls back to board.get_ticket_data.  The test
    verifies the fallback succeeds and direct_fix proceeds past the guard.
    """
    settings = _settings(direct_fix_enabled=True)

    # Scope check: repo IS in installation scope (fallback uses direct path)
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    # Board API direct path — the fallback target
    respx_mock.get("http://127.0.0.1:8077/tickets/t-df-fb").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-df-fb",
                    "state": "blocked",
                    "events": [
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                    ],
                }
            ),
        )
    )
    # Catch-all for remaining GitHub API calls
    respx_mock.get(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.patch(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )

    tools = build_direct_repo_tools(
        settings, component_request=_mock_component_request_error
    )
    df_fn = [t for t in tools if t.__name__ == "direct_fix"][0]

    out = await df_fn(
        ticket_id="t-df-fb",
        repo_full_name="org/repo",
        target_branch="main",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    # The guard must pass — fallback should have retrieved the blocked
    # ticket with ≥3 cycles from the board API.
    assert "Refused" not in out
    assert "Error pushing commit" in out or "pushed successfully" in out


# ---------------------------------------------------------------------------
# _retry_component_ticket_fetch — retry with exponential backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_component_request_retry_on_transient_error(
    respx_mock: respx.MockRouter,
) -> None:
    """component_request returning transient 'Error:' is retried, then succeeds.

    The first two calls return 'Error: connection refused'; the third call
    returns a valid HTTP 200 response.  The tool proceeds via the roster
    path — the board API is never called (no mock for it).  This verifies
    that _retry_component_ticket_fetch retries transient failures and that
    the response from a successful retry is used.
    """
    settings = _settings(direct_fix_enabled=True)

    # Scope check: repo IS in installation scope
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    # Catch-all for remaining GitHub API calls
    respx_mock.get(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.patch(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    # NOTE: no board API (http://127.0.0.1:8077) mock — if the roster path
    # fails and falls back to the board API, the test will fail because
    # respx would block the unmocked request.

    call_count = 0

    async def _retry_then_succeed(
        _component_id: str,
        _method: str,
        _path: str,
        **_kw: Any,
    ) -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return "Error: connection refused"
        return "HTTP 200\n" + json.dumps(
            {
                "state": "blocked",
                "events": [
                    {"type": "implement_start"},
                    {"type": "implement_complete"},
                    {"type": "implement_start"},
                    {"type": "implement_complete"},
                    {"type": "implement_start"},
                    {"type": "implement_complete"},
                ],
            }
        )

    tools = build_direct_repo_tools(settings, component_request=_retry_then_succeed)
    df_fn = [t for t in tools if t.__name__ == "direct_fix"][0]

    out = await df_fn(
        ticket_id="t-df-retry",
        repo_full_name="org/repo",
        target_branch="main",
        files_json=json.dumps([{"path": "x.py", "content": "print(1)"}]),
    )
    # The roster path must have succeeded after retries.
    assert "Refused" not in out
    assert call_count == 3, f"expected 3 calls, got {call_count}"
    assert "Error pushing commit" in out or "pushed successfully" in out

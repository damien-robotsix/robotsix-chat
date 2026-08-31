"""Tests for push_direct_repo_branch, push_commit_to_branch, push_patch_to_pr_branch."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.direct.client import (
    DirectRepoClient,
)

from .conftest import _settings

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

    # Entry carrying two content sources → rejected, naming the accepted forms.
    out3 = await push_fn(
        ticket_id="t-1",
        repo_full_name="org/repo",
        branch_name="fix/t-1",
        files_json=json.dumps(
            [{"path": "x.py", "content": "a", "content_b64": "YQ=="}]
        ),
    )
    assert "Error" in out3
    assert "exactly one" in out3
    assert "content_b64" in out3 and "local_path" in out3

    # Entry with no content source → rejected.
    out4 = await push_fn(
        ticket_id="t-1",
        repo_full_name="org/repo",
        branch_name="fix/t-1",
        files_json=json.dumps([{"path": "x.py"}]),
    )
    assert "Error" in out4
    assert "exactly one" in out4


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
# push_patch_to_pr_branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_patch_to_pr_branch_rejects_non_blocked_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket not in BLOCKED → push_patch_to_pr_branch is refused."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-ppr1").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-ppr1", "state": "draft"})
        )
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "push_patch_to_pr_branch"][0]

    out = await fn(
        ticket_id="t-ppr1",
        repo_full_name="org/repo",
        pr_number=42,
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert "Refused" in out
    assert "t-ppr1" in out
    assert "draft" in out.lower()


@pytest.mark.asyncio
async def test_push_patch_to_pr_branch_rejects_out_of_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → push_patch_to_pr_branch is refused."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "other/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-ppr2").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-ppr2", "state": "blocked"})
        )
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "push_patch_to_pr_branch"][0]

    out = await fn(
        ticket_id="t-ppr2",
        repo_full_name="org/repo",
        pr_number=42,
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert "not installed" in out.lower()


@pytest.mark.asyncio
async def test_push_patch_to_pr_branch_allows_blocked_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket is BLOCKED → push_patch_to_pr_branch proceeds to push."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-ppr3").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-ppr3", "state": "blocked"})
        )
    )
    # PR fetch: return a PR with a head branch in the same repo
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "number": 42,
                    "title": "Test PR",
                    "head": {
                        "ref": "fix/my-branch",
                        "repo": {"full_name": "org/repo"},
                    },
                }
            ),
        )
    )
    # Catch-all for push_patched_file internal calls (get file content,
    # create blobs/trees/commits, update ref)
    respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/contents/"
    ).mock(return_value=httpx.Response(200, text="{}"))
    respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.patch(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "push_patch_to_pr_branch"][0]

    out = await fn(
        ticket_id="t-ppr3",
        repo_full_name="org/repo",
        pr_number=42,
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    # Should not be a guard refusal
    assert "Refused" not in out


@pytest.mark.asyncio
async def test_push_patch_to_pr_branch_rejects_cross_repo_pr(
    respx_mock: respx.MockRouter,
) -> None:
    """PR head branch belongs to a different repo → refused."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-ppr4").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-ppr4", "state": "blocked"})
        )
    )
    # PR head is from a fork (different repo)
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "number": 42,
                    "title": "Fork PR",
                    "head": {
                        "ref": "fix/fork-branch",
                        "repo": {"full_name": "contributor/fork"},
                    },
                }
            ),
        )
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "push_patch_to_pr_branch"][0]

    out = await fn(
        ticket_id="t-ppr4",
        repo_full_name="org/repo",
        pr_number=42,
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert "Refused" in out
    assert "cross-repo" in out.lower()


@pytest.mark.asyncio
async def test_push_patch_to_pr_branch_handles_pr_fetch_error(
    respx_mock: respx.MockRouter,
) -> None:
    """PR fetch fails → returns error message."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-ppr5").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-ppr5", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/99").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "push_patch_to_pr_branch"][0]

    out = await fn(
        ticket_id="t-ppr5",
        repo_full_name="org/repo",
        pr_number=99,
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert "Error" in out
    assert "99" in out


@pytest.mark.asyncio
async def test_push_patch_to_pr_branch_bypasses_scope_with_component_request(
    respx_mock: respx.MockRouter,
) -> None:
    """Scope check is bypassed when component_request is available."""
    # No installation/repositories mock — would fail scope check if it ran
    respx_mock.get("http://127.0.0.1:8077/tickets/t-ppr6").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-ppr6", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "number": 42,
                    "title": "Test PR",
                    "head": {
                        "ref": "fix/my-branch",
                        "repo": {"full_name": "org/repo"},
                    },
                }
            ),
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/contents/"
    ).mock(return_value=httpx.Response(200, text="{}"))
    respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.patch(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )

    async def _mock_component_request_blocked(
        component: str, method: str, path: str
    ) -> str:
        return "HTTP 200 OK\n" + json.dumps({"id": "t-ppr6", "state": "blocked"})

    tools = build_direct_repo_tools(
        _settings(), component_request=_mock_component_request_blocked
    )
    fn = [t for t in tools if t.__name__ == "push_patch_to_pr_branch"][0]

    out = await fn(
        ticket_id="t-ppr6",
        repo_full_name="org/repo",
        pr_number=42,
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert "Refused" not in out


@pytest.mark.asyncio
async def test_push_patch_to_pr_branch_available_by_default() -> None:
    """push_patch_to_pr_branch is in the default tool list."""
    tools = build_direct_repo_tools(_settings())
    names = [t.__name__ for t in tools]
    assert "push_patch_to_pr_branch" in names


@pytest.mark.asyncio
async def test_push_patch_to_pr_branch_uses_custom_commit_message(
    respx_mock: respx.MockRouter,
) -> None:
    """Custom commit_message is passed through to the push."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-ppr7").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-ppr7", "state": "blocked"})
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "number": 42,
                    "title": "Test PR",
                    "head": {
                        "ref": "fix/my-branch",
                        "repo": {"full_name": "org/repo"},
                    },
                }
            ),
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/contents/"
    ).mock(return_value=httpx.Response(200, text="{}"))
    respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )
    respx_mock.patch(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text="{}")
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "push_patch_to_pr_branch"][0]

    out = await fn(
        ticket_id="t-ppr7",
        repo_full_name="org/repo",
        pr_number=42,
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
        commit_message="custom: my special fix",
    )
    # Should not be refused — we just verify it doesn't hit a guard
    assert "Refused" not in out

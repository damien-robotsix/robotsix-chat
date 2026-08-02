"""Tests for patch_direct_repo_file, apply_patch_to_file, and apply_patch."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from robotsix_chat.common.unified_diff import apply_patch
from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.direct.client import (
    DirectRepoClient,
)

from .conftest import _settings

# ---------------------------------------------------------------------------
# patch_direct_repo_file
# ---------------------------------------------------------------------------


def test_patch_direct_repo_file_not_available_by_default() -> None:
    """patch_direct_repo_file not in tool list when direct_fix_enabled is False."""
    tools = build_direct_repo_tools(_settings())
    names = [t.__name__ for t in tools]
    assert "patch_direct_repo_file" not in names


def test_patch_direct_repo_file_available_when_enabled() -> None:
    """patch_direct_repo_file is in the tool list when direct_fix_enabled is True."""
    tools = build_direct_repo_tools(_settings(direct_fix_enabled=True))
    names = [t.__name__ for t in tools]
    assert "patch_direct_repo_file" in names


@pytest.mark.asyncio
async def test_patch_direct_repo_file_rejects_non_blocked_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket not in BLOCKED → patch_direct_repo_file is refused."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-pf1").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-pf1", "state": "draft"})
        )
    )

    tools = build_direct_repo_tools(_settings(direct_fix_enabled=True))
    pf_fn = [t for t in tools if t.__name__ == "patch_direct_repo_file"][0]

    out = await pf_fn(
        ticket_id="t-pf1",
        repo_full_name="org/repo",
        target_branch="main",
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert "Refused" in out
    assert "BLOCKED" in out


@pytest.mark.asyncio
async def test_patch_direct_repo_file_rejects_few_cycles(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket has <3 implement cycles → patch_direct_repo_file is refused."""
    settings = _settings(direct_fix_enabled=True)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-pf2").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-pf2",
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
    pf_fn = [t for t in tools if t.__name__ == "patch_direct_repo_file"][0]

    out = await pf_fn(
        ticket_id="t-pf2",
        repo_full_name="org/repo",
        target_branch="main",
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert "Refused" in out
    assert "implement" in out.lower()
    assert "1" in out  # cycle count


@pytest.mark.asyncio
async def test_patch_direct_repo_file_rejects_zero_cycles(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket has 0 completed implement cycles → patch_direct_repo_file is refused.

    Regression test: the precondition lookup must correctly count 0 cycles
    (empty or non-implement events) and refuse with a distinct message from
    the API-unreachable error.
    """
    settings = _settings(direct_fix_enabled=True)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-pf2z").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-pf2z",
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
    pf_fn = [t for t in tools if t.__name__ == "patch_direct_repo_file"][0]

    out = await pf_fn(
        ticket_id="t-pf2z",
        repo_full_name="org/repo",
        target_branch="main",
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert "Refused" in out
    assert "0" in out  # 0 cycles in the error message
    assert "implement" in out.lower()


@pytest.mark.asyncio
async def test_patch_direct_repo_file_allows_enough_cycles(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket has ≥3 implement cycles → patch_direct_repo_file proceeds."""
    settings = _settings(direct_fix_enabled=True)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-pf3").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-pf3",
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
    # Catch-all for remaining GitHub API calls (get_file_content + push)
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
    pf_fn = [t for t in tools if t.__name__ == "patch_direct_repo_file"][0]

    out = await pf_fn(
        ticket_id="t-pf3",
        repo_full_name="org/repo",
        target_branch="main",
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    # Should have attempted the push (may fail on mock but should not be
    # a guard refusal)
    assert "Refused" not in out


@pytest.mark.asyncio
async def test_patch_direct_repo_file_rejects_out_of_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → patch_direct_repo_file is refused."""
    settings = _settings(direct_fix_enabled=True)

    respx_mock.get("http://127.0.0.1:8077/tickets/t-pf4").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-pf4",
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
    pf_fn = [t for t in tools if t.__name__ == "patch_direct_repo_file"][0]

    out = await pf_fn(
        ticket_id="t-pf4",
        repo_full_name="org/repo",
        target_branch="main",
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert "not installed" in out.lower()
    assert "install" in out.lower()


# ---------------------------------------------------------------------------
# apply_patch — unified diff patch application unit tests
# ---------------------------------------------------------------------------


def test_apply_patch_insert_at_beginning_of_empty_file() -> None:
    """@@ -0,0 +1,N @@ hunk against an empty file inserts at position 0."""
    patch = "@@ -0,0 +1,2 @@\n+line 1\n+line 2\n"
    result = apply_patch("", patch)
    assert result == "line 1\nline 2\n"


def test_apply_patch_insert_at_beginning_of_non_empty_file() -> None:
    """@@ -0,0 +1,N @@ hunk against a non-empty file inserts before line 1."""
    original = "existing line\n"
    patch = "@@ -0,0 +1,1 @@\n+new first line\n"
    result = apply_patch(original, patch)
    assert result == "new first line\nexisting line\n"


def test_apply_patch_normal_context_hunk() -> None:
    """A standard hunk with context, removal, and addition."""
    original = "line 1\nline 2\nline 3\nline 4\nline 5\n"
    patch = "@@ -2,3 +2,4 @@\n line 2\n-line 3\n+new line 3a\n+new line 3b\n line 4\n"
    result = apply_patch(original, patch)
    assert result == "line 1\nline 2\nnew line 3a\nnew line 3b\nline 4\nline 5\n"


def test_apply_patch_multiple_hunks() -> None:
    """Two hunks applied in order with cumulative offset tracking."""
    original = "a\nb\nc\nd\ne\nf\n"
    patch = "@@ -2,2 +2,3 @@\n b\n-c\n+cc\n+ccc\n d\n@@ -5,1 +6,0 @@\n-e\n"
    result = apply_patch(original, patch)
    assert result == "a\nb\ncc\nccc\nd\nf\n"


def test_apply_patch_removal_only() -> None:
    """Hunk that only removes lines."""
    original = "keep\nremove me\nalso keep\n"
    patch = "@@ -2,1 +1,0 @@\n-remove me\n"
    result = apply_patch(original, patch)
    assert result == "keep\nalso keep\n"


def test_apply_patch_addition_only() -> None:
    """Hunk that only adds lines (context-only with additions)."""
    original = "header\nfooter\n"
    patch = "@@ -2,1 +2,3 @@\n footer\n+middle 1\n+middle 2\n"
    result = apply_patch(original, patch)
    assert result == "header\nfooter\nmiddle 1\nmiddle 2\n"


def test_apply_patch_context_mismatch_raises_value_error() -> None:
    """Context line does not match original → ValueError."""
    original = "line 1\nline 2\n"
    patch = "@@ -1,1 +1,1 @@\n wrong\n+replacement\n"
    with pytest.raises(ValueError, match="context mismatch"):
        apply_patch(original, patch)


def test_apply_patch_no_newline_at_eof_marker() -> None:
    r"""The \\ No newline at end of file marker is skipped."""
    original = "line 1\nline 2"
    patch = (
        "@@ -1,2 +1,3 @@\n line 1\n-line 2\n+line 2\n+line 3\n"
        "\\ No newline at end of file\n"
    )
    result = apply_patch(original, patch)
    assert result == "line 1\nline 2\nline 3\n"


def test_apply_patch_insert_at_zero_with_prior_offset() -> None:
    """A hunk at @@ -0,0 after a prior hunk that shifted lines.

    The cumulative offset from the first hunk should not cause a negative
    orig_pos when the second hunk starts at old_start=0.
    """
    original = "a\nb\nc\n"
    patch = "@@ -3,1 +3,2 @@\n c\n+d\n@@ -0,0 +1,1 @@\n+preface\n"
    result = apply_patch(original, patch)
    assert result == "preface\na\nb\nc\nd\n"


def test_apply_patch_empty_hunk_at_zero() -> None:
    """@@ -0,0 +0,0 @@ (empty add at beginning) is a no-op."""
    original = "a\nb\n"
    patch = "@@ -0,0 +0,0 @@\n"
    result = apply_patch(original, patch)
    assert result == "a\nb\n"


def test_apply_patch_preserves_trailing_newline() -> None:
    """Files ending with newline keep it after patching."""
    original = "line 1\n"
    patch = "@@ -0,0 +1,1 @@\n+line 0\n"
    result = apply_patch(original, patch)
    assert result == "line 0\nline 1\n"


def test_apply_patch_preserves_no_trailing_newline() -> None:
    """Files without trailing newline keep that property after patching."""
    original = "only line"
    patch = "@@ -0,0 +1,1 @@\n+prefix\n"
    result = apply_patch(original, patch)
    # When original has no trailing newline, splitlines(keepends=True)
    # returns ["only line"] (no \n). The result should also lack a
    # trailing newline.
    assert result == "prefix\nonly line"


# ---------------------------------------------------------------------------
# apply_patch_to_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_patch_to_file_default_new_branch(
    respx_mock: respx.MockRouter,
) -> None:
    """apply_patch_to_file without target_branch pushes to a new branch."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-aptf1").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"id": "t-aptf1", "state": "blocked"}),
        )
    )
    # Repo info (called once by apply_patch_to_file and again inside
    # push_branch — both need default_branch).
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"default_branch": "main"}),
        )
    )
    # File content on the default branch (base64-encoded "old\n").
    respx_mock.get("https://api.github.com/repos/org/repo/contents/x.py?ref=main").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "encoding": "base64",
                    "content": "b2xkCg==",
                    "sha": "abc123",
                }
            ),
        )
    )
    # Catch-all GET for git refs and commits (push_branch internals).
    respx_mock.get(url__startswith="https://api.github.com/repos/org/repo/git").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "object": {"sha": "base-sha"},
                    "sha": "commit-sha",
                    "tree": {"sha": "tree-sha"},
                }
            ),
        )
    )
    # Catch-all POST for blobs, trees, commits, and ref creation.
    respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "new-sha"}))
    )
    # push_branch creates a new ref via POST, not PATCH — verify
    # the target_branch path (which uses PATCH) is NOT taken.
    patch_route = respx_mock.patch(
        url__startswith="https://api.github.com/repos/org/repo"
    ).mock(return_value=httpx.Response(200, text="{}"))

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "apply_patch_to_file"][0]

    out = await fn(
        ticket_id="t-aptf1",
        repo_full_name="org/repo",
        branch_name="fix/t-aptf1",
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert "Refused" not in out
    assert not patch_route.called


@pytest.mark.asyncio
async def test_apply_patch_to_file_target_branch(
    respx_mock: respx.MockRouter,
) -> None:
    """apply_patch_to_file with target_branch pushes directly to that branch."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-aptf2").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"id": "t-aptf2", "state": "blocked"}),
        )
    )
    # File content on the target branch (base64-encoded "old\n").
    respx_mock.get(
        "https://api.github.com/repos/org/repo/contents/x.py?ref=existing-branch"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "encoding": "base64",
                    "content": "b2xkCg==",
                    "sha": "abc123",
                }
            ),
        )
    )
    # Catch-all GET for git refs and commits (push_commit_to_branch).
    respx_mock.get(url__startswith="https://api.github.com/repos/org/repo/git").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "object": {"sha": "base-sha"},
                    "sha": "commit-sha",
                    "tree": {"sha": "tree-sha"},
                }
            ),
        )
    )
    # Catch-all POST for blobs, trees, and commits.
    respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"sha": "new-sha"}))
    )
    # push_patched_file → push_commit_to_branch uses PATCH to update
    # the existing branch ref, not POST (which would create a new ref).
    patch_route = respx_mock.patch(
        url__startswith="https://api.github.com/repos/org/repo"
    ).mock(return_value=httpx.Response(200, text="{}"))

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "apply_patch_to_file"][0]

    out = await fn(
        ticket_id="t-aptf2",
        repo_full_name="org/repo",
        branch_name="fix/t-aptf2",
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
        target_branch="existing-branch",
    )
    assert "Refused" not in out
    # The target_branch path calls push_patched_file which updates
    # the existing ref via PATCH — verify it was taken.
    assert patch_route.called


@pytest.mark.asyncio
async def test_apply_patch_to_file_rejects_non_blocked(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-BLOCKED ticket is refused even when target_branch is supplied."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-aptf3").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"id": "t-aptf3", "state": "draft"}),
        )
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "apply_patch_to_file"][0]

    out = await fn(
        ticket_id="t-aptf3",
        repo_full_name="org/repo",
        branch_name="fix/t-aptf3",
        file_path="x.py",
        patch_content="@@ -1,1 +1,1 @@\n-old\n+new\n",
        target_branch="existing-branch",
    )
    assert "Refused" in out
    assert "t-aptf3" in out
    assert "draft" in out.lower()

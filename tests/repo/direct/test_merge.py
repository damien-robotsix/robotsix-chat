"""Tests for merge/conflict/auto-merge tools and related client methods."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.direct.client import (
    DirectRepoClient,
)

from .conftest import _prepopulate_installation_token, _settings

# ---------------------------------------------------------------------------
# check_pr_merge_conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_pr_merge_conflict_clean(
    respx_mock: respx.MockRouter,
) -> None:
    """mergeable=True → no-conflict message."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-clean").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-clean", "state": "blocked"})
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
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/7").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Fix the thing",
                    "html_url": "https://github.com/org/repo/pull/7",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "check_pr_merge_conflict"][0]

    out = await fn(
        ticket_id="t-clean",
        repo_full_name="org/repo",
        pr_number=7,
    )
    assert "No merge conflicts" in out
    assert "clean" in out


@pytest.mark.asyncio
async def test_check_pr_merge_conflict_dirty(
    respx_mock: respx.MockRouter,
) -> None:
    """mergeable=False → conflict message."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-dirty").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-dirty", "state": "blocked"})
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
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/8").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Breaks the thing",
                    "html_url": "https://github.com/org/repo/pull/8",
                    "mergeable": False,
                    "mergeable_state": "dirty",
                    "merged": False,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "check_pr_merge_conflict"][0]

    out = await fn(
        ticket_id="t-dirty",
        repo_full_name="org/repo",
        pr_number=8,
    )
    assert "Merge conflicts detected" in out
    assert "dirty" in out


@pytest.mark.asyncio
async def test_check_pr_merge_conflict_unknown(
    respx_mock: respx.MockRouter,
) -> None:
    """mergeable=None → still-computing message."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-unk").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-unk", "state": "blocked"})
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
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/9").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Maybe works",
                    "html_url": "https://github.com/org/repo/pull/9",
                    "mergeable": None,
                    "mergeable_state": "unknown",
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "check_pr_merge_conflict"][0]

    out = await fn(
        ticket_id="t-unk",
        repo_full_name="org/repo",
        pr_number=9,
    )
    assert "still being computed" in out.lower()


@pytest.mark.asyncio
async def test_check_pr_merge_conflict_rejects_non_blocked(
    respx_mock: respx.MockRouter,
) -> None:
    """BLOCKED guard applies to check_pr_merge_conflict."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-nb2").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-nb2", "state": "ready"})
        )
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "check_pr_merge_conflict"][0]

    out = await fn(
        ticket_id="t-nb2",
        repo_full_name="org/repo",
        pr_number=1,
    )
    assert "Refused" in out
    assert "BLOCKED" in out


# ---------------------------------------------------------------------------
# merge_direct_repo_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_pr_success(
    respx_mock: respx.MockRouter,
) -> None:
    """mergeable=True, not draft → merge succeeds."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/10").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Fix the thing",
                    "html_url": "https://github.com/org/repo/pull/10",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/10/merge").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "sha": "abc123def456",  # pragma: allowlist secret
                    "merged": True,
                    "message": "Pull Request successfully merged",
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=10,
        pr_title="Fix the thing",
        head_base_branches="fix/t-10 → main",
        merge_method="squash",
    )
    assert "merged successfully" in out.lower()
    assert "abc123def456" in out  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_merge_pr_draft_refused(
    respx_mock: respx.MockRouter,
) -> None:
    """PR is draft → merge refused with diagnostic."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/11").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "WIP thing",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": True,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=11,
        pr_title="WIP thing",
        head_base_branches="fix/t-11 → main",
    )
    assert "draft" in out.lower()
    assert "11" in out


@pytest.mark.asyncio
async def test_merge_pr_conflict_refused(
    respx_mock: respx.MockRouter,
) -> None:
    """PR has merge conflicts → merge refused."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/12").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Conflicting PR",
                    "mergeable": False,
                    "mergeable_state": "dirty",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=12,
        pr_title="Conflicting PR",
        head_base_branches="fix/t-12 → main",
    )
    assert "merge conflicts" in out.lower()
    assert "12" in out


@pytest.mark.asyncio
async def test_merge_pr_unknown_mergeability_refused(
    respx_mock: respx.MockRouter,
) -> None:
    """mergeable=None → merge refused (still computing)."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/13").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Pending PR",
                    "mergeable": None,
                    "mergeable_state": "unknown",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=13,
        pr_title="Pending PR",
        head_base_branches="fix/t-13 → main",
    )
    assert "still being computed" in out.lower()


@pytest.mark.asyncio
async def test_merge_pr_already_merged(
    respx_mock: respx.MockRouter,
) -> None:
    """PR already merged → informative message."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/14").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Already merged",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": True,
                    "merge_commit_sha": "sha123",
                    "draft": False,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=14,
        pr_title="Already merged",
        head_base_branches="fix/t-14 → main",
    )
    assert "already merged" in out.lower()


@pytest.mark.asyncio
async def test_merge_pr_out_of_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → merge refused."""
    settings = _settings()

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/other"}]}),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=1,
        pr_title="Any",
        head_base_branches="fix → main",
    )
    assert "not installed" in out.lower()
    assert "install" in out.lower()


@pytest.mark.asyncio
async def test_merge_pr_github_405(
    respx_mock: respx.MockRouter,
) -> None:
    """GitHub returns 405 (not mergeable) → diagnostic message."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/15").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "CI failing",
                    "mergeable": True,
                    "mergeable_state": "blocked",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/15/merge").mock(
        return_value=httpx.Response(
            405,
            text=json.dumps({"message": "Pull Request is not mergeable"}),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=15,
        pr_title="CI failing",
        head_base_branches="fix/t-15 → main",
    )
    assert "not in a mergeable state" in out.lower()
    assert "status checks" in out.lower()


# ---------------------------------------------------------------------------
# arm_direct_repo_auto_merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arm_auto_merge_success(
    respx_mock: respx.MockRouter,
) -> None:
    """Not draft, not merged → auto-merge enabled."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/20").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Ready PR",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/20/auto-merge").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"message": "Auto-merge enabled", "merge_method": "squash"}
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "arm_direct_repo_auto_merge"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=20,
        pr_title="Ready PR",
        head_base_branches="fix/t-20 → main",
        merge_method="squash",
    )
    assert "auto-merge enabled" in out.lower()
    assert "20" in out


@pytest.mark.asyncio
async def test_arm_auto_merge_draft_refused(
    respx_mock: respx.MockRouter,
) -> None:
    """PR is draft → auto-merge refused."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/21").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Draft PR",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": True,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "arm_direct_repo_auto_merge"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=21,
        pr_title="Draft PR",
        head_base_branches="fix/t-21 → main",
    )
    assert "draft" in out.lower()


@pytest.mark.asyncio
async def test_arm_auto_merge_already_merged(
    respx_mock: respx.MockRouter,
) -> None:
    """PR already merged → auto-merge refused."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/22").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Merged PR",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": True,
                    "draft": False,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "arm_direct_repo_auto_merge"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=22,
        pr_title="Merged PR",
        head_base_branches="fix/t-22 → main",
    )
    assert "already merged" in out.lower()


@pytest.mark.asyncio
async def test_arm_auto_merge_out_of_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in scope → auto-merge refused."""
    settings = _settings()

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/other"}]}),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "arm_direct_repo_auto_merge"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=1,
        pr_title="Any",
        head_base_branches="fix → main",
    )
    assert "not installed" in out.lower()
    assert "install" in out.lower()


@pytest.mark.asyncio
async def test_arm_auto_merge_github_error(
    respx_mock: respx.MockRouter,
) -> None:
    """GitHub returns 403/404 (auto-merge not available) → diagnostic."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/23").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "No auto-merge repo",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/23/auto-merge").mock(
        return_value=httpx.Response(
            403,
            text=json.dumps({"message": "Auto-merge is not allowed"}),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "arm_direct_repo_auto_merge"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=23,
        pr_title="No auto-merge repo",
        head_base_branches="fix/t-23 → main",
    )
    assert "auto-merge" in out.lower()
    assert "23" in out


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
    merge_tool_names = {"merge_direct_repo_pr", "arm_direct_repo_auto_merge"}
    # Read-only tools that don't require BLOCKED state
    readonly_tool_names = {"check_direct_repo_auto_merge"}

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
            text=json.dumps({"token": "ghs_fresh_token_after_401"}),
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
            text=json.dumps({"token": "ghs_fresh_token_after_401"}),
        )
    )

    client = DirectRepoClient(settings)
    with pytest.raises(RuntimeError, match="GitHub API GET"):
        await client.list_installation_repos()

    # Two calls: initial + one retry
    assert repos_route.call_count == 2

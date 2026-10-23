"""Pull-request tests for the direct-repo integration.

Covers opening, merging, updating, conflict-checking, listing and diffing
pull requests through :func:`build_direct_repo_tools` and
:class:`DirectRepoClient`, with ``respx`` mocked so there are no real network calls.
``robotsix_github_auth`` is mocked by the autouse fixture in ``conftest.py``.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.direct.client import DirectRepoClient
from tests.repo.direct.conftest import (
    _mock_installation_repos,
    _prepopulate_installation_token,
    _settings,
)

# ---------------------------------------------------------------------------
# BLOCKED-state precondition — open_direct_repo_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_pr_rejects_non_blocked_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket not in BLOCKED → PR open is refused."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
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

    respx_mock.get("http://127.0.0.1:8077/tickets/t-2").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-2", "state": "blocked"})
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
# Merge and auto-merge capability
# ---------------------------------------------------------------------------


def test_merge_methods_exist_on_client() -> None:
    """DirectRepoClient exposes merge_pr, arm_auto_merge, resolve_pr_conflict."""
    client = DirectRepoClient(_settings())
    assert hasattr(client, "merge_pr")
    assert hasattr(client, "arm_auto_merge")
    assert hasattr(client, "resolve_pr_conflict")
    assert callable(client.merge_pr)
    assert callable(client.arm_auto_merge)
    assert callable(client.resolve_pr_conflict)


def test_merge_tools_returned() -> None:
    """Verify that build_direct_repo_tools returns merge and auto-merge tools."""
    tools = build_direct_repo_tools(_settings())
    names = [t.__name__ for t in tools]
    assert "merge_direct_repo_pr" in names
    assert "arm_direct_repo_auto_merge" in names
    # Expected set: push, open_pr, update_branch, check_merge_conflict,
    # merge, auto-merge, close, reset, apply_patch, token inspection
    assert sorted(names) == [
        "apply_patch_to_file",
        "arm_direct_repo_auto_merge",
        "check_ci_health",
        "check_direct_repo_auto_merge",
        "check_pr_merge_conflict",
        "close_direct_repo_pr",
        "enable_repo_pages",
        "fetch_ci_job_logs",
        "fetch_trivy_findings",
        "file_ci_stabilization_ticket",
        "get_repo_settings",
        "inspect_github_installation_token",
        "inspect_pr_diff",
        "list_open_prs",
        "merge_direct_repo_pr",
        "open_direct_repo_pr",
        "open_simple_repo_pr",
        "push_direct_repo_branch",
        "push_patch_to_pr_branch",
        "recover_auto_merge",
        "rerun_ci_workflow",
        "reset_implement_spawn_counter",
        "resolve_pr_conflict",
        "set_repo_delete_branch_on_merge",
        "update_pr_branch",
        "update_simple_repo_pr",
        "verify_pr_ci_status",
    ]


# ---------------------------------------------------------------------------
# merge_pr — client method HTTP response branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_pr_client_200_success(
    respx_mock: respx.MockRouter,
) -> None:
    """merge_pr returns success message on 200 with merged=True."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Test PR",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/42/merge").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "sha": "abc123def",  # pragma: allowlist secret
                    "merged": True,
                    "message": "Pull Request successfully merged",
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    result = await client.merge_pr(
        repo_full_name="org/repo",
        pr_number=42,
        merge_method="squash",
        commit_title="Squash all the things",
    )
    assert "merged successfully" in result
    assert "42" in result
    assert "org/repo" in result


@pytest.mark.asyncio
async def test_merge_pr_client_200_not_merged(
    respx_mock: respx.MockRouter,
) -> None:
    """merge_pr returns warning when 200 response has merged=False."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Test PR",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/42/merge").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "merged": False,
                    "message": "Merge already in progress",
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    result = await client.merge_pr(
        repo_full_name="org/repo",
        pr_number=42,
    )
    assert "was not merged" in result
    assert "Merge already in progress" in result


@pytest.mark.asyncio
async def test_merge_pr_client_405_not_mergeable(
    respx_mock: respx.MockRouter,
) -> None:
    """merge_pr returns not-mergeable message on 405."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/99").mock(
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
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/99/merge").mock(
        return_value=httpx.Response(
            405,
            text=json.dumps(
                {
                    "message": "Pull Request is not mergeable",
                    "documentation_url": "https://docs.github.com/...",
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    result = await client.merge_pr(
        repo_full_name="org/repo",
        pr_number=99,
    )
    assert "not in a mergeable state" in result.lower()
    assert "99" in result
    assert "status checks" in result.lower()
    assert "Pull Request is not mergeable" in result


@pytest.mark.asyncio
async def test_merge_pr_client_409_conflict(
    respx_mock: respx.MockRouter,
) -> None:
    """merge_pr returns conflict message on 409."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/pulls/77").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Test PR",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/77/merge").mock(
        return_value=httpx.Response(
            409,
            text=json.dumps(
                {
                    "message": "Merge conflict",
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    result = await client.merge_pr(
        repo_full_name="org/repo",
        pr_number=77,
    )
    assert "merge conflict" in result.lower()
    assert "77" in result


# ---------------------------------------------------------------------------
# PR human-review gate — verify no auto-merge requested
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pr_does_not_enable_auto_merge(
    respx_mock: respx.MockRouter,
) -> None:
    """create_pr must NOT set auto_merge or merge-related fields in the payload."""
    settings = _settings()

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
# PR body defaults when body not provided
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_pr_default_body_links_ticket_id(
    respx_mock: respx.MockRouter,
) -> None:
    """When no body is provided, the tool generates one referencing the ticket."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-3").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-3", "state": "blocked"})
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
# update_pr_branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_pr_branch_success(
    respx_mock: respx.MockRouter,
) -> None:
    """202 response → success message."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-up").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-up", "state": "blocked"})
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
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/42/update-branch").mock(
        return_value=httpx.Response(202, text=json.dumps({"message": "queued"}))
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "update_pr_branch"][0]

    out = await fn(
        ticket_id="t-up",
        repo_full_name="org/repo",
        pr_number=42,
    )
    assert "queued" in out.lower()
    assert "42" in out


@pytest.mark.asyncio
async def test_update_pr_branch_conflict(
    respx_mock: respx.MockRouter,
) -> None:
    """422 response → conflict message returned."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-conflict").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-conflict", "state": "blocked"})
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
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/99/update-branch").mock(
        return_value=httpx.Response(
            422,
            text=json.dumps(
                {"message": "Update is not possible. Pull request is not mergeable."}
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "update_pr_branch"][0]

    out = await fn(
        ticket_id="t-conflict",
        repo_full_name="org/repo",
        pr_number=99,
    )
    assert "conflict" in out.lower()
    assert "99" in out
    assert "not mergeable" in out.lower()


@pytest.mark.asyncio
async def test_update_pr_branch_rejects_non_blocked(
    respx_mock: respx.MockRouter,
) -> None:
    """BLOCKED guard applies to update_pr_branch."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-nb").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-nb", "state": "draft"})
        )
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "update_pr_branch"][0]

    out = await fn(
        ticket_id="t-nb",
        repo_full_name="org/repo",
        pr_number=1,
    )
    assert "Refused" in out
    assert "BLOCKED" in out


@pytest.mark.asyncio
async def test_update_pr_branch_rejects_out_of_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Scope guard applies to update_pr_branch."""
    _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-scope").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-scope", "state": "blocked"})
        )
    )
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/other"}]}),
        )
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "update_pr_branch"][0]

    out = await fn(
        ticket_id="t-scope",
        repo_full_name="org/repo",
        pr_number=1,
    )
    assert "not installed" in out.lower()
    assert "install" in out.lower()


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
# list_open_prs
# ---------------------------------------------------------------------------


_SEARCH_ITEMS = [
    {
        "number": 12,
        "title": "Add batch PR listing",
        "html_url": "https://github.com/damien-robotsix/repo-a/pull/12",
        "user": {"login": "octocat"},
        "draft": False,
        "state": "open",
        "repository_url": "https://api.github.com/repos/damien-robotsix/repo-a",
    },
    {
        "number": 821,
        "title": "Add chat-scoped volume file write endpoint",
        "html_url": "https://github.com/damien-robotsix/robotsix-central-deploy/pull/821",
        "user": {"login": "robotsix-mill[bot]"},
        "draft": False,
        "state": "closed",
        "pull_request": {"merged_at": "2026-08-30T14:11:00Z"},
        "repository_url": (
            "https://api.github.com/repos/damien-robotsix/robotsix-central-deploy"
        ),
    },
]


@pytest.mark.asyncio
async def test_list_open_prs_defaults_to_installation_account_and_shows_merged(
    respx_mock: respx.MockRouter,
) -> None:
    """No org guess: the owner comes from the installation; merged PRs are listed."""
    settings = _settings()
    _prepopulate_installation_token(settings)
    _mock_installation_repos(respx_mock)

    search = respx_mock.get(
        url__startswith="https://api.github.com/search/issues"
    ).mock(return_value=httpx.Response(200, text=json.dumps({"items": _SEARCH_ITEMS})))

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "list_open_prs"][0]

    out = await fn()
    query = search.calls[0].request.url.params["q"]
    assert "user:damien-robotsix" in query
    assert "state:" not in query  # default state=all
    assert "updated:>=" in query
    assert (
        "PRs (all updated in the last 30 days) for account 'damien-robotsix' — 2 total:"
        in out
    )
    assert "damien-robotsix/repo-a" in out
    assert "#12 Add batch PR listing [open] (by octocat)" in out
    assert "#821 Add chat-scoped volume file write endpoint [merged]" in out


@pytest.mark.asyncio
async def test_list_open_prs_resolves_mill_repo_id(
    respx_mock: respx.MockRouter,
) -> None:
    """A mill repo_id is resolved through GET /repos into repo:<owner>/<name>."""
    settings = _settings()
    _prepopulate_installation_token(settings)
    respx_mock.get("http://127.0.0.1:8077/repos").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                [
                    {
                        "repo_id": "robotsix-central-deploy",
                        "board_id": "robotsix-central-deploy",
                        "forge_remote_url": "https://github.com/damien-robotsix/robotsix-central-deploy.git",
                    }
                ]
            ),
        )
    )
    search = respx_mock.get(
        url__startswith="https://api.github.com/search/issues"
    ).mock(
        return_value=httpx.Response(200, text=json.dumps({"items": [_SEARCH_ITEMS[1]]}))
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "list_open_prs"][0]

    out = await fn(repo="robotsix-central-deploy", state="closed", since_days=0)
    query = search.calls[0].request.url.params["q"]
    assert "repo:damien-robotsix/robotsix-central-deploy" in query
    assert "state:closed" in query
    assert "updated:" not in query
    assert "for damien-robotsix/robotsix-central-deploy — 1 total" in out
    assert "#821" in out and "[merged]" in out


@pytest.mark.asyncio
async def test_list_open_prs_unknown_repo_id_refuses_to_guess(
    respx_mock: respx.MockRouter,
) -> None:
    """An unknown bare name is an error pointing at resolve_repo — no search happens."""
    settings = _settings()
    _prepopulate_installation_token(settings)
    respx_mock.get("http://127.0.0.1:8077/repos").mock(
        return_value=httpx.Response(200, text=json.dumps([]))
    )
    search = respx_mock.get(
        url__startswith="https://api.github.com/search/issues"
    ).mock(return_value=httpx.Response(200, text=json.dumps({"items": []})))

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "list_open_prs"][0]

    out = await fn(repo="robotsix")
    assert out.startswith("Error: 'robotsix' is neither a registered mill repo_id")
    assert "resolve_repo" in out
    assert not search.called


@pytest.mark.asyncio
async def test_list_open_prs_no_results(
    respx_mock: respx.MockRouter,
) -> None:
    """Empty search results produce a clear no-PRs message naming the scope."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(url__startswith="https://api.github.com/search/issues").mock(
        return_value=httpx.Response(200, text=json.dumps({"items": []}))
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "list_open_prs"][0]

    out = await fn(repo="damien-robotsix/repo-a", state="open")
    assert "No open PRs found for damien-robotsix/repo-a" in out


@pytest.mark.asyncio
async def test_list_open_prs_api_error(
    respx_mock: respx.MockRouter,
) -> None:
    """Search API failures are surfaced as an error message, not raised."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(url__startswith="https://api.github.com/search/issues").mock(
        return_value=httpx.Response(422, text="Validation Failed")
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "list_open_prs"][0]

    out = await fn(owner="damien-robotsix")
    assert "Error listing PRs for account 'damien-robotsix'" in out


@pytest.mark.asyncio
async def test_list_open_prs_rejects_bad_state() -> None:
    """An invalid state is rejected before any network call."""
    settings = _settings()
    _prepopulate_installation_token(settings)
    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "list_open_prs"][0]
    assert (await fn(owner="x", state="merged")).startswith("Error: state must be")


# ============================================================================
# inspect_pr_diff
# ============================================================================


@pytest.mark.asyncio
async def test_inspect_pr_diff_success(
    respx_mock: respx.MockRouter,
) -> None:
    """inspect_pr_diff returns the diff prefixed with a line count."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    diff = "diff --git a/x.py b/x.py\n+print('hi')\n"
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/7").mock(
        return_value=httpx.Response(200, text=diff)
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "inspect_pr_diff"][0]

    out = await fn(repo_full_name="org/repo", pr_number=7)
    assert "PR #7 diff" in out
    assert "diff --git a/x.py b/x.py" in out
    assert "Error" not in out


@pytest.mark.asyncio
async def test_inspect_pr_diff_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """inspect_pr_diff reports an empty diff clearly."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/8").mock(
        return_value=httpx.Response(200, text="")
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "inspect_pr_diff"][0]

    out = await fn(repo_full_name="org/repo", pr_number=8)
    assert "diff is empty" in out
    assert "PR #8" in out


@pytest.mark.asyncio
async def test_inspect_pr_diff_truncated(
    respx_mock: respx.MockRouter,
) -> None:
    """inspect_pr_diff truncates large diffs and notes the remainder."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    long_diff = "diff --git a/x.py b/x.py\n" + ("+added line of content\n" * 400)
    assert len(long_diff) > 8000
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/9").mock(
        return_value=httpx.Response(200, text=long_diff)
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "inspect_pr_diff"][0]

    out = await fn(repo_full_name="org/repo", pr_number=9)
    assert "PR #9 diff" in out
    assert "[truncated:" in out
    assert "more chars" in out
    assert "more lines" in out


@pytest.mark.asyncio
async def test_inspect_pr_diff_error(
    respx_mock: respx.MockRouter,
) -> None:
    """inspect_pr_diff surfaces a GitHub API error gracefully."""
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/10").mock(
        return_value=httpx.Response(404, text=json.dumps({"message": "Not Found"}))
    )

    tools = build_direct_repo_tools(_settings())
    fn = [t for t in tools if t.__name__ == "inspect_pr_diff"][0]

    out = await fn(repo_full_name="org/repo", pr_number=10)
    assert "Error fetching diff for PR #10" in out


@pytest.mark.asyncio
async def test_get_pr_diff_preserves_accept_header_on_401_retry(
    respx_mock: respx.MockRouter,
) -> None:
    """401 retry keeps the caller-supplied diff Accept header (regression)."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    route = respx_mock.get("https://api.github.com/repos/org/repo/pulls/42").mock(
        side_effect=[
            httpx.Response(401, text="token expired"),
            httpx.Response(200, text="diff --git a/x.py b/x.py\n"),
        ]
    )

    client = DirectRepoClient(settings)
    result = await client.get_pr_diff(repo_full_name="org/repo", pr_number=42)
    assert "diff --git" in result

    assert len(route.calls) == 2
    assert route.calls[1].request.headers["Accept"] == "application/vnd.github.v3.diff"

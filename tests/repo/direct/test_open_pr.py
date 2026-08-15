"""Tests for open_direct_repo_pr tool and merge_pr client method."""

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
    """Verify that DirectRepoClient exposes merge_pr and arm_auto_merge."""
    client = DirectRepoClient(_settings())
    assert hasattr(client, "merge_pr")
    assert hasattr(client, "arm_auto_merge")
    assert callable(client.merge_pr)
    assert callable(client.arm_auto_merge)


def test_merge_tools_returned() -> None:
    """Verify that build_direct_repo_tools returns merge and auto-merge tools."""
    tools = build_direct_repo_tools(_settings())
    names = [t.__name__ for t in tools]
    assert "merge_direct_repo_pr" in names
    assert "arm_direct_repo_auto_merge" in names
    # Expected set: push, open_pr, update_branch, check_merge_conflict,
    # merge, auto-merge, reset, apply_patch
    assert sorted(names) == [
        "apply_patch_to_file",
        "arm_direct_repo_auto_merge",
        "check_direct_repo_auto_merge",
        "check_pr_merge_conflict",
        "list_open_prs",
        "merge_direct_repo_pr",
        "open_direct_repo_pr",
        "push_direct_repo_branch",
        "push_patch_to_pr_branch",
        "recover_auto_merge",
        "reset_implement_spawn_counter",
        "update_pr_branch",
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

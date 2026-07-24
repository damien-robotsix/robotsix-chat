"""Tests for the direct-repo integration.

:func:`build_direct_repo_tools` and :class:`DirectRepoClient`, with ``respx``
mocked so there are no real network calls.
``robotsix_github_auth.mint_installation_token`` is mocked so the shared
library is never imported.
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
    _INSTALLATION_TOKEN_CACHE,
    DirectRepoClient,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _prepopulate_installation_token(settings: DirectRepoSettings) -> None:
    """Seed the installation token cache so tests bypass the token exchange."""
    _INSTALLATION_TOKEN_CACHE[settings.github_app_installation_id] = (
        "ghs_prepopulated_token"
    )


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


@pytest.fixture(autouse=True)
def _mock_github_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock mint_installation_token so the shared library is never imported."""
    import sys
    from types import SimpleNamespace

    def _fake_mint(**kw: object) -> object:
        return SimpleNamespace(token="ghs_test_installation_token")

    fake = SimpleNamespace()
    fake.mint_installation_token = _fake_mint
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)


# ---------------------------------------------------------------------------
# build_direct_repo_tools
# ---------------------------------------------------------------------------


def test_build_direct_repo_tools_disabled() -> None:
    """Verify that disabled direct_repo returns no tools."""
    assert build_direct_repo_tools(DirectRepoSettings(enabled=False)) == []


def test_build_direct_repo_tools_returns_four_tools() -> None:
    """Verify that enabled direct_repo returns the four expected tools."""
    tools = build_direct_repo_tools(_settings())
    assert len(tools) == 4
    names = [t.__name__ for t in tools]
    assert "push_direct_repo_branch" in names
    assert "open_direct_repo_pr" in names
    assert "update_pr_branch" in names
    assert "check_pr_merge_conflict" in names


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
    _settings()

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
    """Verify that build_direct_repo_tools returns no merge-performing tools.

    Tools may reference "merge" in the context of *checking* mergeability
    (e.g. ``check_pr_merge_conflict``), but never to perform an actual merge.
    """
    tools = build_direct_repo_tools(_settings())
    names = [t.__name__ for t in tools]
    # The only tool with "merge" in the name is the *check* tool — verify it
    # is not a merge-performing tool.
    merge_named = [n for n in names if "merge" in n.lower()]
    assert merge_named == ["check_pr_merge_conflict"], (
        f"Unexpected merge-named tools: {merge_named}"
    )
    # Expected set: push, open_pr, update_branch, check_merge_conflict
    assert sorted(names) == [
        "check_pr_merge_conflict",
        "open_direct_repo_pr",
        "push_direct_repo_branch",
        "update_pr_branch",
    ]


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
    respx_mock.get("https://api.github.com/installation/repositories").mock(
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
    respx_mock.get("https://api.github.com/installation/repositories").mock(
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
    respx_mock.get("https://api.github.com/installation/repositories").mock(
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
    assert "Refused" in out
    assert "scope" in out.lower()


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
    respx_mock.get("https://api.github.com/installation/repositories").mock(
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
    respx_mock.get("https://api.github.com/installation/repositories").mock(
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
    respx_mock.get("https://api.github.com/installation/repositories").mock(
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
    """
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
        # If "merge" appears it must be descriptive (conflicts, state) OR denial.
        # Performative merge language is forbidden.
        performative = ("perform merge", "execute merge", "merge pr", "merge pull")
        if "merge" in doc:
            assert not any(p in doc for p in performative), (
                f"Tool {tool.__name__} docstring uses performative merge language"
            )


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
    assert len(tools) == 5  # 4 base + direct_fix


@pytest.mark.asyncio
async def test_direct_fix_rejects_non_blocked_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Ticket not in BLOCKED → direct_fix is refused."""
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
    respx_mock.get("https://api.github.com/installation/repositories").mock(
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
    respx_mock.get("https://api.github.com/installation/repositories").mock(
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
    assert "Refused" in out
    assert "scope" in out.lower()


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
    respx_mock.get("https://api.github.com/installation/repositories").mock(
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
    respx_mock.get("https://api.github.com/installation/repositories").mock(
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
    repos_url = "https://api.github.com/installation/repositories"
    repos_route = respx_mock.get(repos_url).mock(
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

    repos_url = "https://api.github.com/installation/repositories"
    repos_route = respx_mock.get(repos_url).mock(
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

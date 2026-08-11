"""Tests for direct_fix tool and scope-check bypass."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.repo.direct import build_direct_repo_tools

from .conftest import _settings

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
assert len(tools) == 21  # 19 base + direct_fix + patch_direct_repo_file


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
# Scope-check bypass — mill pipeline credential (component_request) available
# ---------------------------------------------------------------------------


async def _mock_component_request_blocked(
    _component_id: str,
    _method: str,
    _path: str,
    **_kw: Any,
) -> str:
    """Mock ``component_request`` that returns BLOCKED state with ≥3 cycles."""
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

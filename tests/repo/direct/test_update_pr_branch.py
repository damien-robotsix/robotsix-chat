"""Tests for update_pr_branch tool."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from robotsix_chat.repo.direct import build_direct_repo_tools

from .conftest import _settings

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

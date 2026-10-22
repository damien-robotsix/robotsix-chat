"""Tests for ``DirectRepoClient.get_repo_settings`` / ``set_delete_branch_on_merge``.

Uses ``respx`` for HTTP mocking — no real network calls.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from robotsix_chat.repo.direct.client import DirectRepoClient

from .conftest import _prepopulate_installation_token, _settings

BASE = "https://api.github.com"
REPO_URL = f"{BASE}/repos/org/repo"


def _client() -> DirectRepoClient:
    s = _settings()
    _prepopulate_installation_token(s)
    return DirectRepoClient(s)


@pytest.mark.asyncio
async def test_get_repo_settings_reports_hygiene_fields(
    respx_mock: respx.MockRouter,
) -> None:
    """The read reports delete_branch_on_merge, default_branch, allow_auto_merge."""
    respx_mock.get(REPO_URL).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "default_branch": "main",
                    "delete_branch_on_merge": False,
                    "allow_auto_merge": True,
                    "archived": False,
                }
            ),
        )
    )

    result = await _client().get_repo_settings("org/repo")

    assert "Repository settings for org/repo" in result
    assert "- delete_branch_on_merge: false" in result
    assert '- default_branch: "main"' in result
    assert "- allow_auto_merge: true" in result


@pytest.mark.asyncio
async def test_get_repo_settings_error(respx_mock: respx.MockRouter) -> None:
    """A failing GET is returned as an error string, never raised."""
    respx_mock.get(REPO_URL).mock(return_value=httpx.Response(404, text="nope"))

    result = await _client().get_repo_settings("org/repo")

    assert result.startswith("Error fetching repository settings for org/repo")


@pytest.mark.asyncio
async def test_set_delete_branch_on_merge_sends_only_that_field(
    respx_mock: respx.MockRouter,
) -> None:
    """The PATCH body carries exactly one key: delete_branch_on_merge."""
    patch = respx_mock.patch(REPO_URL).mock(
        return_value=httpx.Response(
            200, text=json.dumps({"delete_branch_on_merge": True})
        )
    )

    result = await _client().set_delete_branch_on_merge("org/repo", True)

    assert result == "delete_branch_on_merge set to true on org/repo."
    assert json.loads(patch.calls.last.request.content) == {
        "delete_branch_on_merge": True
    }


@pytest.mark.asyncio
async def test_set_delete_branch_on_merge_disable(
    respx_mock: respx.MockRouter,
) -> None:
    patch = respx_mock.patch(REPO_URL).mock(
        return_value=httpx.Response(
            200, text=json.dumps({"delete_branch_on_merge": False})
        )
    )

    result = await _client().set_delete_branch_on_merge("org/repo", enabled=False)

    assert "set to false" in result
    assert json.loads(patch.calls.last.request.content) == {
        "delete_branch_on_merge": False
    }


@pytest.mark.asyncio
async def test_set_delete_branch_on_merge_permission_denied(
    respx_mock: respx.MockRouter,
) -> None:
    """A 403 points at the missing Administration: write permission."""
    respx_mock.patch(REPO_URL).mock(
        return_value=httpx.Response(
            403, text=json.dumps({"message": "Resource not accessible"})
        )
    )

    result = await _client().set_delete_branch_on_merge("org/repo")

    assert "permission denied" in result
    assert "administration: write" in result
    assert "inspect_github_installation_token" in result


@pytest.mark.asyncio
async def test_set_delete_branch_on_merge_readback_mismatch(
    respx_mock: respx.MockRouter,
) -> None:
    """A response reporting a different value is surfaced as an error."""
    respx_mock.patch(REPO_URL).mock(
        return_value=httpx.Response(
            200, text=json.dumps({"delete_branch_on_merge": False})
        )
    )

    result = await _client().set_delete_branch_on_merge("org/repo", True)

    assert result.startswith("Error updating delete_branch_on_merge")


@pytest.mark.asyncio
async def test_set_delete_branch_on_merge_rejects_non_bool() -> None:
    result = await _client().set_delete_branch_on_merge(
        "org/repo",
        "yes",  # type: ignore[arg-type]
    )
    assert result.startswith("Error: enabled must be a boolean")

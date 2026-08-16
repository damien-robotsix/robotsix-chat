"""Tests for ``DirectRepoClient.enable_pages``.

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


def _client() -> DirectRepoClient:
    s = _settings()
    _prepopulate_installation_token(s)
    return DirectRepoClient(s)


def _site(status: str = "built", build_type: str = "workflow") -> str:
    return json.dumps(
        {
            "html_url": "https://org.github.io/repo",
            "status": status,
            "build_type": build_type,
        }
    )


@pytest.mark.asyncio
async def test_enable_pages_creates_and_reports_status(
    respx_mock: respx.MockRouter,
) -> None:
    """A fresh repo has Pages created with build_type workflow + status read back."""
    client = _client()

    post = respx_mock.post(f"{BASE}/repos/org/repo/pages").mock(
        return_value=httpx.Response(201, text=_site(status="building"))
    )
    respx_mock.get(f"{BASE}/repos/org/repo/pages").mock(
        return_value=httpx.Response(200, text=_site(status="built"))
    )

    result = await client.enable_pages("org/repo")

    assert "GitHub Pages enabled on org/repo" in result
    assert "Site status: built" in result
    assert "build_type: workflow" in result
    assert "Site URL: https://org.github.io/repo" in result
    # The create request must carry build_type: workflow.
    assert json.loads(post.calls.last.request.content) == {"build_type": "workflow"}


@pytest.mark.asyncio
async def test_enable_pages_already_enabled_is_idempotent(
    respx_mock: respx.MockRouter,
) -> None:
    """A 409 (already enabled) is reported as success, not an error."""
    client = _client()

    respx_mock.post(f"{BASE}/repos/org/repo/pages").mock(
        return_value=httpx.Response(409, text="Pages already exists")
    )
    respx_mock.get(f"{BASE}/repos/org/repo/pages").mock(
        return_value=httpx.Response(200, text=_site(status="built"))
    )

    result = await client.enable_pages("org/repo")

    assert "already enabled" in result
    assert "Site status: built" in result
    assert "Error" not in result


@pytest.mark.asyncio
async def test_enable_pages_switches_build_type_on_conflict(
    respx_mock: respx.MockRouter,
) -> None:
    """A 409 with a differing build_type switches via PUT."""
    client = _client()

    respx_mock.post(f"{BASE}/repos/org/repo/pages").mock(
        return_value=httpx.Response(409, text="Pages already exists")
    )
    respx_mock.get(f"{BASE}/repos/org/repo/pages").mock(
        side_effect=[
            httpx.Response(200, text=_site(status="built", build_type="legacy")),
            httpx.Response(200, text=_site(status="built")),
        ]
    )
    put = respx_mock.put(f"{BASE}/repos/org/repo/pages").mock(
        return_value=httpx.Response(204)
    )

    result = await client.enable_pages("org/repo")

    assert "GitHub Pages updated on org/repo" in result
    assert json.loads(put.calls.last.request.content) == {"build_type": "workflow"}


@pytest.mark.asyncio
async def test_enable_pages_permission_denied(
    respx_mock: respx.MockRouter,
) -> None:
    """A 403 missing pages:write permission is reported gracefully."""
    client = _client()

    respx_mock.post(f"{BASE}/repos/org/repo/pages").mock(
        return_value=httpx.Response(
            403, text=json.dumps({"message": "lacks pages: write"})
        )
    )

    result = await client.enable_pages("org/repo")

    assert "permission denied" in result
    assert "inspect_github_installation_token" in result


@pytest.mark.asyncio
async def test_enable_pages_invalid_build_type() -> None:
    client = _client()

    result = await client.enable_pages("org/repo", build_type="bogus")
    assert result.startswith("Error: build_type must be")

"""Tests for the reference-docs integration.

:func:`build_refdocs_tools` and :class:`RefDocsClient`, with ``respx`` mocked
so there are no real network calls.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings, RefDocsSettings
from robotsix_chat.refdocs import build_refdocs_tools
from robotsix_chat.refdocs.client import RefDocsClient


def _settings(**kw: Any) -> RefDocsSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "repos": ["org/allowed-repo"],
    }
    base.update(kw)
    return RefDocsSettings(**base)


def _direct_repo(**kw: Any) -> DirectRepoSettings:
    """Minimal DirectRepoSettings for tests — App not configured by default."""
    return DirectRepoSettings(**kw)


# ---------------------------------------------------------------------------
# build_refdocs_tools
# ---------------------------------------------------------------------------


def test_build_refdocs_tools_disabled() -> None:
    """Verify that disabled refdocs returns no tools."""
    assert build_refdocs_tools(RefDocsSettings(enabled=False), _direct_repo()) == []


def test_build_refdocs_tools_returns_read_and_list_tools() -> None:
    """Verify that enabled refdocs returns the read and list tool callables."""
    tools = build_refdocs_tools(_settings(), _direct_repo())
    names = {t.__name__ for t in tools}
    assert names == {"read_reference_doc", "list_reference_docs"}


# ---------------------------------------------------------------------------
# RefDocsClient — allowlist enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_rejects_repo_not_in_allowlist() -> None:
    """The read tool refuses repos outside the allowlist without a request."""
    client = RefDocsClient(_settings(repos=["org/x"]), _direct_repo())
    out = await client.read_file("org/y", "README.md")
    assert "Access denied" in out
    assert "org/y" in out


@pytest.mark.asyncio
async def test_list_rejects_repo_not_in_allowlist() -> None:
    """The list tool refuses repos outside the allowlist without a request."""
    client = RefDocsClient(_settings(repos=["org/x"]), _direct_repo())
    out = await client.list_files("org/y", "")
    assert "Access denied" in out
    assert "org/y" in out


# ---------------------------------------------------------------------------
# RefDocsClient — successful fetch (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_returns_decoded_content(
    respx_mock: respx.MockRouter,
) -> None:
    """The read tool decodes base64 content from a successful GitHub response."""
    text = "## Manual-action states\n\n- state A\n- state B\n"
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    respx_mock.get("https://api.github.com/repos/org/r/contents/docs/states.md").mock(
        return_value=httpx.Response(
            200, json={"content": encoded, "encoding": "base64"}
        )
    )

    client = RefDocsClient(_settings(repos=["org/r"]), _direct_repo())
    out = await client.read_file("org/r", "docs/states.md")
    assert out == text


@pytest.mark.asyncio
async def test_read_file_uses_app_token_when_configured(
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read tool sends an Authorization header when App creds are set."""
    route = respx_mock.get("https://api.github.com/repos/org/r/contents/f.txt").mock(
        return_value=httpx.Response(200, json={"content": "YQ==", "encoding": "base64"})
    )

    import sys
    from types import SimpleNamespace

    async def _fake_mint(**kw: object) -> str:
        return "app-token-123"

    fake = SimpleNamespace()
    fake.mint_installation_token = _fake_mint
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)

    dr = _direct_repo(
        github_app_id="12345",
        github_app_private_key="fake-key",
        github_app_installation_id="67890",
    )
    client = RefDocsClient(_settings(repos=["org/r"]), dr)
    await client.read_file("org/r", "f.txt")

    assert route.calls.last.request.headers["authorization"] == "Bearer app-token-123"


@pytest.mark.asyncio
async def test_read_file_no_token_header_when_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """The read tool does NOT send an Authorization header when no token."""
    route = respx_mock.get("https://api.github.com/repos/org/r/contents/f.txt").mock(
        return_value=httpx.Response(200, json={"content": "YQ==", "encoding": "base64"})
    )

    client = RefDocsClient(_settings(repos=["org/r"]), _direct_repo())
    await client.read_file("org/r", "f.txt")
    assert "authorization" not in route.calls.last.request.headers


@pytest.mark.asyncio
async def test_read_file_directory_response_returns_guidance(
    respx_mock: respx.MockRouter,
) -> None:
    """When the GitHub response is a list (directory), guide to list tool."""
    respx_mock.get("https://api.github.com/repos/org/allowed-repo/contents/docs").mock(
        return_value=httpx.Response(200, json=[{"name": "a.md", "type": "file"}])
    )

    client = RefDocsClient(_settings(), _direct_repo())
    out = await client.read_file("org/allowed-repo", "docs")
    assert "directory" in out.lower()
    assert "list_reference_docs" in out


@pytest.mark.asyncio
async def test_read_file_missing_content_returns_error(
    respx_mock: respx.MockRouter,
) -> None:
    """When the response lacks base64 content, return an error string."""
    respx_mock.get("https://api.github.com/repos/org/allowed-repo/contents/f.txt").mock(
        return_value=httpx.Response(200, json={"no_content_here": True})
    )

    client = RefDocsClient(_settings(), _direct_repo())
    out = await client.read_file("org/allowed-repo", "f.txt")
    assert "Unable to decode" in out


# ---------------------------------------------------------------------------
# RefDocsClient — error handling (no raise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_http_error_returns_string(
    respx_mock: respx.MockRouter,
) -> None:
    """An HTTP error is returned as a concise string, never raised."""
    respx_mock.get(
        "https://api.github.com/repos/org/allowed-repo/contents/missing.md"
    ).mock(return_value=httpx.Response(404, json={}))

    client = RefDocsClient(_settings(), _direct_repo())
    out = await client.read_file("org/allowed-repo", "missing.md")
    assert "Failed to fetch" in out


@pytest.mark.asyncio
async def test_read_file_network_error_returns_string(
    respx_mock: respx.MockRouter,
) -> None:
    """A network/connection error is returned as a concise string, never raised."""
    respx_mock.get("https://api.github.com/repos/org/allowed-repo/contents/f.txt").mock(
        side_effect=ConnectionError("connection refused")
    )

    client = RefDocsClient(_settings(), _direct_repo())
    out = await client.read_file("org/allowed-repo", "f.txt")
    assert "Failed to fetch" in out
    assert "connection refused" in out.lower()


@pytest.mark.asyncio
async def test_list_files_returns_formatted_listing(
    respx_mock: respx.MockRouter,
) -> None:
    """The list tool formats a directory listing from a GitHub array response."""
    respx_mock.get(
        "https://api.github.com/repos/org/allowed-repo/contents?ref=main"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "README.md", "type": "file"},
                {"name": "docs", "type": "dir"},
                {"name": "states.md", "type": "file"},
            ],
        )
    )

    client = RefDocsClient(_settings(), _direct_repo())
    out = await client.list_files("org/allowed-repo", "")
    assert "README.md" in out
    assert "docs/" in out
    assert "states.md" in out
    assert "org/allowed-repo/" in out


@pytest.mark.asyncio
async def test_list_files_file_response_returns_guidance(
    respx_mock: respx.MockRouter,
) -> None:
    """When the GitHub response is a file (not a list), guide to read tool."""
    respx_mock.get(
        "https://api.github.com/repos/org/allowed-repo/contents/README.md?ref=main"
    ).mock(
        return_value=httpx.Response(200, json={"content": "YQ==", "encoding": "base64"})
    )

    client = RefDocsClient(_settings(), _direct_repo())
    out = await client.list_files("org/allowed-repo", "README.md")
    assert "file" in out.lower()
    assert "read_reference_doc" in out


@pytest.mark.asyncio
async def test_list_files_network_error_returns_string(
    respx_mock: respx.MockRouter,
) -> None:
    """A network error in list_files is returned as a string, never raised."""
    respx_mock.get(
        "https://api.github.com/repos/org/allowed-repo/contents?ref=main"
    ).mock(side_effect=OSError("timeout"))

    client = RefDocsClient(_settings(), _direct_repo())
    out = await client.list_files("org/allowed-repo", "")
    assert "Failed to list" in out
    assert "timeout" in out.lower()


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_truncates_large_content(
    respx_mock: respx.MockRouter,
) -> None:
    """Content exceeding the cap is truncated with a marker."""
    big = "x" * 40_000
    encoded = base64.b64encode(big.encode("utf-8")).decode("ascii")
    respx_mock.get(
        "https://api.github.com/repos/org/allowed-repo/contents/big.txt?ref=main"
    ).mock(
        return_value=httpx.Response(
            200, json={"content": encoded, "encoding": "base64"}
        )
    )

    client = RefDocsClient(_settings(), _direct_repo())
    out = await client.read_file("org/allowed-repo", "big.txt")
    assert "truncated" in out.lower()
    assert len(out) < 40_000  # definitely shorter than original

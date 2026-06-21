"""Tests for the reference-docs integration.

:func:`build_refdocs_tools` and :class:`RefDocsClient`, with ``httpx`` mocked
so there are no real network calls.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from robotsix_chat.config import RefDocsSettings
from robotsix_chat.refdocs import build_refdocs_tools
from robotsix_chat.refdocs.client import RefDocsClient


def _settings(**kw: Any) -> RefDocsSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "repos": ["org/allowed-repo"],
    }
    base.update(kw)
    return RefDocsSettings(**base)


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------


class _MockResponse:
    """Minimal httpx.Response stand-in for testing."""

    def __init__(self, json_data: Any, status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=object(),  # type: ignore[arg-type]
                response=self,  # type: ignore[arg-type]
            )


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    response: _MockResponse,
) -> dict[str, Any]:
    """Replace ``httpx.AsyncClient`` with a factory returning *response*.

    Returns a ``captured`` dict that receives ``url`` and ``headers`` from
    each ``get`` call for later inspection.
    """
    captured: dict[str, Any] = {}

    class _BoundClient:
        def __init__(self, **kwargs: Any) -> None:
            self._resp = response

        async def __aenter__(self) -> _BoundClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> _MockResponse:
            captured["url"] = url
            captured["headers"] = headers
            return self._resp

    monkeypatch.setattr(httpx, "AsyncClient", _BoundClient)
    return captured


# ---------------------------------------------------------------------------
# build_refdocs_tools
# ---------------------------------------------------------------------------


def test_build_refdocs_tools_disabled() -> None:
    """Verify that disabled refdocs returns no tools."""
    assert build_refdocs_tools(RefDocsSettings(enabled=False)) == []


def test_build_refdocs_tools_returns_read_and_list_tools() -> None:
    """Verify that enabled refdocs returns the read and list tool callables."""
    tools = build_refdocs_tools(_settings())
    names = {t.__name__ for t in tools}
    assert names == {"read_reference_doc", "list_reference_docs"}


# ---------------------------------------------------------------------------
# RefDocsClient — allowlist enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_rejects_repo_not_in_allowlist() -> None:
    """The read tool refuses repos outside the allowlist without a request."""
    client = RefDocsClient(_settings(repos=["org/x"]))
    out = await client.read_file("org/y", "README.md")
    assert "Access denied" in out
    assert "org/y" in out


@pytest.mark.asyncio
async def test_list_rejects_repo_not_in_allowlist() -> None:
    """The list tool refuses repos outside the allowlist without a request."""
    client = RefDocsClient(_settings(repos=["org/x"]))
    out = await client.list_files("org/y", "")
    assert "Access denied" in out
    assert "org/y" in out


# ---------------------------------------------------------------------------
# RefDocsClient — successful fetch (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_returns_decoded_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read tool decodes base64 content from a successful GitHub response."""
    text = "## Manual-action states\n\n- state A\n- state B\n"
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    resp = _MockResponse({"content": encoded, "encoding": "base64"})

    _install_mock_client(monkeypatch, resp)

    client = RefDocsClient(_settings(repos=["org/r"]))
    out = await client.read_file("org/r", "docs/states.md")
    assert out == text


@pytest.mark.asyncio
async def test_read_file_uses_token_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read tool sends an Authorization header when a token is set."""
    resp = _MockResponse({"content": "YQ==", "encoding": "base64"})

    captured = _install_mock_client(monkeypatch, resp)

    client = RefDocsClient(
        _settings(repos=["org/r"], github_token="ghp_test")  # pragma: allowlist secret
    )
    await client.read_file("org/r", "f.txt")

    assert captured.get("headers", {}).get("Authorization") == (
        "Bearer ghp_test"  # pragma: allowlist secret
    )


@pytest.mark.asyncio
async def test_read_file_no_token_header_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read tool does NOT send an Authorization header when no token."""
    resp = _MockResponse({"content": "YQ==", "encoding": "base64"})

    captured = _install_mock_client(monkeypatch, resp)

    client = RefDocsClient(_settings(repos=["org/r"]))
    await client.read_file("org/r", "f.txt")
    assert "Authorization" not in captured.get("headers", {})


@pytest.mark.asyncio
async def test_read_file_directory_response_returns_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the GitHub response is a list (directory), guide to list tool."""
    resp = _MockResponse([{"name": "a.md", "type": "file"}])

    _install_mock_client(monkeypatch, resp)

    client = RefDocsClient(_settings())
    out = await client.read_file("org/allowed-repo", "docs")
    assert "directory" in out.lower()
    assert "list_reference_docs" in out


@pytest.mark.asyncio
async def test_read_file_missing_content_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the response lacks base64 content, return an error string."""
    resp = _MockResponse({"no_content_here": True})

    _install_mock_client(monkeypatch, resp)

    client = RefDocsClient(_settings())
    out = await client.read_file("org/allowed-repo", "f.txt")
    assert "Unable to decode" in out


# ---------------------------------------------------------------------------
# RefDocsClient — error handling (no raise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_http_error_returns_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP error is returned as a concise string, never raised."""
    resp = _MockResponse({}, status_code=404)

    _install_mock_client(monkeypatch, resp)

    client = RefDocsClient(_settings())
    out = await client.read_file("org/allowed-repo", "missing.md")
    assert "Failed to fetch" in out


@pytest.mark.asyncio
async def test_read_file_network_error_returns_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network/connection error is returned as a concise string, never raised."""

    class _FailingClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> None:
            raise ConnectionError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _FailingClient)

    client = RefDocsClient(_settings())
    out = await client.read_file("org/allowed-repo", "f.txt")
    assert "Failed to fetch" in out
    assert "connection refused" in out.lower()


@pytest.mark.asyncio
async def test_list_files_returns_formatted_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list tool formats a directory listing from a GitHub array response."""
    resp = _MockResponse(
        [
            {"name": "README.md", "type": "file"},
            {"name": "docs", "type": "dir"},
            {"name": "states.md", "type": "file"},
        ]
    )

    _install_mock_client(monkeypatch, resp)

    client = RefDocsClient(_settings())
    out = await client.list_files("org/allowed-repo", "")
    assert "README.md" in out
    assert "docs/" in out
    assert "states.md" in out
    assert "org/allowed-repo/" in out


@pytest.mark.asyncio
async def test_list_files_file_response_returns_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the GitHub response is a file (not a list), guide to read tool."""
    resp = _MockResponse({"content": "YQ==", "encoding": "base64"})

    _install_mock_client(monkeypatch, resp)

    client = RefDocsClient(_settings())
    out = await client.list_files("org/allowed-repo", "README.md")
    assert "file" in out.lower()
    assert "read_reference_doc" in out


@pytest.mark.asyncio
async def test_list_files_network_error_returns_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network error in list_files is returned as a string, never raised."""

    class _FailingClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> None:
            raise OSError("timeout")

    monkeypatch.setattr(httpx, "AsyncClient", _FailingClient)

    client = RefDocsClient(_settings())
    out = await client.list_files("org/allowed-repo", "")
    assert "Failed to list" in out
    assert "timeout" in out.lower()


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_truncates_large_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content exceeding the cap is truncated with a marker."""
    big = "x" * 40_000
    encoded = base64.b64encode(big.encode("utf-8")).decode("ascii")
    resp = _MockResponse({"content": encoded, "encoding": "base64"})

    _install_mock_client(monkeypatch, resp)

    client = RefDocsClient(_settings())
    out = await client.read_file("org/allowed-repo", "big.txt")
    assert "truncated" in out.lower()
    assert len(out) < 40_000  # definitely shorter than original

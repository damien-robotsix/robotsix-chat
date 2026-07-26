"""Tests for the public URL fetch tool.

:func:`build_public_fetch_tools` with ``respx`` mocked so there are
no real network calls.  DNS resolution is mocked via ``socket.getaddrinfo``
so the SSRF guard can validate public-IP results without real DNS lookups.
"""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from robotsix_chat.config import PublicFetchSettings
from robotsix_chat.public_fetch import build_public_fetch_tools


def _settings(**kw: Any) -> PublicFetchSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "timeout": 30.0,
        "max_body_bytes": 1_048_576,
        "max_redirects": 5,
    }
    base.update(kw)
    return PublicFetchSettings(**base)


# ---------------------------------------------------------------------------
# DNS mock helpers
# ---------------------------------------------------------------------------


# Save the real getaddrinfo before any test patches it.
_real_getaddrinfo = socket.getaddrinfo


def _public_addrinfo(
    host: str, port: Any = None, *args: Any, **kwargs: Any
) -> list[tuple[Any, ...]]:
    """Return a synthetic ``getaddrinfo`` result resolving to a public IP.

    Falls through to the real ``socket.getaddrinfo`` for localhost so
    loopback / DNS SSRF tests still block correctly.
    """
    if host in ("localhost", "localhost.localdomain"):
        return _real_getaddrinfo(host, port)
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("93.184.216.34", 0),
        )
    ]


def _dns_passthrough(hostname: str) -> list[tuple[Any, ...]]:
    """Call the real ``socket.getaddrinfo`` — used for localhost / loopback."""
    return socket.getaddrinfo(hostname, None)


# ---------------------------------------------------------------------------
# build_public_fetch_tools
# ---------------------------------------------------------------------------


def test_build_public_fetch_tools_disabled() -> None:
    """Disabled public_fetch returns no tools."""
    assert build_public_fetch_tools(PublicFetchSettings(enabled=False)) == []


def test_build_public_fetch_tools_returns_one_tool() -> None:
    """Enabled public_fetch returns exactly one tool: fetch_public_url."""
    tools = build_public_fetch_tools(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "fetch_public_url"


# ---------------------------------------------------------------------------
# fetch_public_url — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_basic_success(respx_mock: respx.MockRouter) -> None:
    """A successful fetch returns body text with status summary."""
    url = "https://gitlab.univ-nantes.fr/some/repo/raw/main/README.md"
    respx_mock.get(url).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/markdown; charset=utf-8"},
            text="# My Project\n\nDescription here.",
        )
    )

    tools = build_public_fetch_tools(_settings())
    with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
        result = await tools[0](url)

    assert "Status: 200" in result
    assert "Content-Type: text/markdown" in result
    assert "# My Project" in result


@pytest.mark.asyncio
async def test_fetch_truncates_large_body(respx_mock: respx.MockRouter) -> None:
    """Body larger than max_body_bytes is truncated with a note."""
    settings = _settings(max_body_bytes=10)
    body = "x" * 100
    url = "https://example.com/large.txt"
    respx_mock.get(url).mock(return_value=httpx.Response(200, text=body))

    tools = build_public_fetch_tools(settings)
    with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
        result = await tools[0](url)

    assert "Body size: 100 bytes" in result
    assert "truncated to 10 bytes" in result


# ---------------------------------------------------------------------------
# Redirect handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_follows_redirects(respx_mock: respx.MockRouter) -> None:
    """Redirects are followed and the final URL is reported."""
    respx_mock.get("https://gitlab.com/org/repo/-/raw/main/file.txt").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://cdn.gitlab.com/org/repo/file.txt"}
        )
    )
    respx_mock.get("https://cdn.gitlab.com/org/repo/file.txt").mock(
        return_value=httpx.Response(200, text="hello world")
    )

    tools = build_public_fetch_tools(_settings())
    with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
        result = await tools[0]("https://gitlab.com/org/repo/-/raw/main/file.txt")

    assert "Status: 200" in result
    assert "hello world" in result


# ---------------------------------------------------------------------------
# URL scheme validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_rejects_non_http_scheme() -> None:
    """ftp:// and other schemes are blocked."""
    tools = build_public_fetch_tools(_settings())
    result = await tools[0]("ftp://example.com/file")

    assert "SSRF check failed" in result
    assert "scheme" in result.lower()


@pytest.mark.asyncio
async def test_fetch_rejects_file_scheme() -> None:
    """file:// scheme is blocked."""
    tools = build_public_fetch_tools(_settings())
    result = await tools[0]("file:///etc/passwd")

    assert "SSRF check failed" in result


# ---------------------------------------------------------------------------
# SSRF protection — bare IP addresses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_rejects_bare_ipv4() -> None:
    """Bare IPv4 addresses are blocked."""
    tools = build_public_fetch_tools(_settings())
    result = await tools[0]("http://127.0.0.1/admin")

    assert "SSRF check failed" in result
    assert "Bare IP" in result


@pytest.mark.asyncio
async def test_fetch_rejects_bare_ipv6_bracketed() -> None:
    """Bare IPv6 addresses in brackets are blocked."""
    tools = build_public_fetch_tools(_settings())
    result = await tools[0]("http://[::1]/admin")

    assert "SSRF check failed" in result
    assert "Bare IP" in result


# ---------------------------------------------------------------------------
# SSRF protection — DNS resolution (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_rejects_localhost(respx_mock: respx.MockRouter) -> None:
    """Localhost resolves to 127.0.0.1 and is blocked by DNS check."""
    # localhost always resolves to 127.0.0.1 (loopback) in real DNS, so
    # the DNS check blocks it before any HTTP call.
    tools = build_public_fetch_tools(_settings())
    result = await tools[0]("http://localhost:8080/debug")

    assert "SSRF check failed" in result
    assert "non-public IP" in result


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_timeout(respx_mock: respx.MockRouter) -> None:
    """Timeout → clear error message."""
    url = "https://example.com/timeout"
    respx_mock.get(url).mock(side_effect=httpx.TimeoutException("timed out"))

    tools = build_public_fetch_tools(_settings())
    with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
        result = await tools[0](url)

    assert "timed out" in result


@pytest.mark.asyncio
async def test_fetch_too_many_redirects(respx_mock: respx.MockRouter) -> None:
    """Exceeding max_redirects returns an error."""
    settings = _settings(max_redirects=1)
    respx_mock.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/b"})
    )
    respx_mock.get("https://example.com/b").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/c"})
    )

    tools = build_public_fetch_tools(settings)
    with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
        result = await tools[0]("https://example.com/a")

    assert "Too many redirects" in result


# ---------------------------------------------------------------------------
# SSRF protection — redirect to internal host is blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_rejects_redirect_to_localhost(
    respx_mock: respx.MockRouter,
) -> None:
    """A redirect to localhost is blocked before the request is made."""
    respx_mock.get("https://evil.com/redirect").mock(
        return_value=httpx.Response(
            302, headers={"Location": "http://localhost:8080/secret"}
        )
    )

    tools = build_public_fetch_tools(_settings())
    with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
        result = await tools[0]("https://evil.com/redirect")

    assert "Redirect target rejected" in result


# ---------------------------------------------------------------------------
# HTTP errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_404(respx_mock: respx.MockRouter) -> None:
    """A 404 response returns an error with the body snippet."""
    url = "https://example.com/missing"
    respx_mock.get(url).mock(return_value=httpx.Response(404, text="Not Found"))

    tools = build_public_fetch_tools(_settings())
    with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
        result = await tools[0](url)

    assert "HTTP 404" in result
    assert "Not Found" in result


@pytest.mark.asyncio
async def test_fetch_500(respx_mock: respx.MockRouter) -> None:
    """A 500 response returns an error with the body snippet."""
    url = "https://example.com/broken"
    respx_mock.get(url).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    tools = build_public_fetch_tools(_settings())
    with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
        result = await tools[0](url)

    assert "HTTP 500" in result
    assert "Internal Server Error" in result

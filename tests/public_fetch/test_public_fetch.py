"""Tests for the public-fetch tool.

:func:`build_public_fetch_tools` with ``respx`` mocked so there are
no real network calls.  SSRF checks are tested via monkeypatched DNS
resolution.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from unittest import mock

import httpx
import pytest
import respx

from robotsix_chat.config import PublicFetchSettings
from robotsix_chat.public_fetch import build_public_fetch_tools, load_public_fetch_skill


def _settings(**kw: Any) -> PublicFetchSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "timeout": 10.0,
        "max_body_bytes": 1_048_576,
        "max_redirects": 5,
        "domain_allowlist": [],
        "rate_limit_requests": 100,
        "rate_limit_window_seconds": 60.0,
    }
    base.update(kw)
    return PublicFetchSettings(**base)


# ---------------------------------------------------------------------------
# build_public_fetch_tools — enable/disable
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
# load_public_fetch_skill
# ---------------------------------------------------------------------------


def test_load_public_fetch_skill_returns_non_empty_markdown() -> None:
    """The shipped skill.md is loadable and describes the tool."""
    skill = load_public_fetch_skill()
    assert len(skill) > 100
    assert "fetch_public_url" in skill
    assert "read-only" in skill.lower()


# ---------------------------------------------------------------------------
# fetch_public_url — success paths
# ---------------------------------------------------------------------------

_MOCK_SOCKET_RETURN = [
    (
        socket.AF_INET,
        socket.SOCK_STREAM,
        6,
        "",
        ("93.184.216.34", 0),  # example.com public IP
    )
]


@pytest.mark.asyncio
async def test_fetch_basic_success(respx_mock: respx.MockRouter) -> None:
    """A successful fetch returns the raw text with metadata."""
    respx_mock.get("https://example.com/README.md").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            text="# Hello World\n\nThis is a README.",
        )
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("https://example.com/README.md"))

    assert result["error"] == ""
    assert result["status_code"] == 200
    assert result["final_url"] == "https://example.com/README.md"
    assert "text/plain" in result["content_type"]
    assert result["body_size_bytes"] > 0
    assert "# Hello World" in result["text"]
    assert result["truncated"] is False
    assert result["fetched_at"].endswith("Z")


@pytest.mark.asyncio
async def test_fetch_with_cookies(respx_mock: respx.MockRouter) -> None:
    """Cookies are injected into the request headers."""
    respx_mock.get("https://example.com/api").mock(
        return_value=httpx.Response(200, text="authenticated content")
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(
            await tools[0](
                "https://example.com/api",
                cookies={"session_id": "abc123", "user_token": "xyz789"},
            )
        )

    assert result["error"] == ""
    assert result["status_code"] == 200
    assert "authenticated content" in result["text"]
    # Verify cookies were sent in the request
    assert "Cookie" in respx_mock.calls.last.request.headers
    cookie_header = respx_mock.calls.last.request.headers["Cookie"]
    assert "session_id=abc123" in cookie_header
    assert "user_token=xyz789" in cookie_header


@pytest.mark.asyncio
async def test_fetch_with_cookies_redirect(respx_mock: respx.MockRouter) -> None:
    """Cookies are forwarded through redirects."""
    respx_mock.get("https://example.com/old").mock(
        return_value=httpx.Response(
            301, headers={"Location": "https://example.com/new"}
        )
    )
    respx_mock.get("https://example.com/new").mock(
        return_value=httpx.Response(200, text="redirected content")
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(
            await tools[0](
                "https://example.com/old",
                cookies={"session_id": "abc123"},
            )
        )

    assert result["error"] == ""
    assert result["status_code"] == 200
    assert result["final_url"] == "https://example.com/new"
    assert "redirected content" in result["text"]
    # Verify cookies were sent in the redirect request
    assert "Cookie" in respx_mock.calls.last.request.headers
    cookie_header = respx_mock.calls.last.request.headers["Cookie"]
    assert "session_id=abc123" in cookie_header


@pytest.mark.asyncio
async def test_fetch_without_cookies(respx_mock: respx.MockRouter) -> None:
    """When no cookies are provided, no Cookie header is sent."""
    respx_mock.get("https://example.com/api").mock(
        return_value=httpx.Response(200, text="public content")
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("https://example.com/api", cookies=None))

    assert result["error"] == ""
    assert result["status_code"] == 200
    assert "public content" in result["text"]
    # Verify no Cookie header was sent
    assert "Cookie" not in respx_mock.calls.last.request.headers


@pytest.mark.asyncio
async def test_fetch_github_raw(respx_mock: respx.MockRouter) -> None:
    """GitHub raw URL shape works correctly."""
    respx_mock.get(
        "https://raw.githubusercontent.com/damien-robotsix/robotsix-standards/main/README.md"
    ).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            text="# robotsix-standards",
        )
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(
            await tools[0](
                "https://raw.githubusercontent.com/damien-robotsix/"
                "robotsix-standards/main/README.md"
            )
        )

    assert result["error"] == ""
    assert result["status_code"] == 200
    assert "# robotsix-standards" in result["text"]


@pytest.mark.asyncio
async def test_fetch_gitlab_raw(respx_mock: respx.MockRouter) -> None:
    """GitLab raw URL shape works correctly."""
    respx_mock.get(
        "https://gitlab.univ-nantes.fr/ls2n-drones/ls2n_drone_armada/-/raw/main/README.md"
    ).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            text="# ls2n_drone_armada",
        )
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(
            await tools[0](
                "https://gitlab.univ-nantes.fr/ls2n-drones/"
                "ls2n_drone_armada/-/raw/main/README.md"
            )
        )

    assert result["error"] == ""
    assert result["status_code"] == 200
    assert "# ls2n_drone_armada" in result["text"]


@pytest.mark.asyncio
async def test_fetch_redirect_followed(respx_mock: respx.MockRouter) -> None:
    """Redirects are followed and final_url reflects the target."""
    respx_mock.get("https://example.com/old").mock(
        return_value=httpx.Response(
            301, headers={"Location": "https://example.com/new"}
        )
    )
    respx_mock.get("https://example.com/new").mock(
        return_value=httpx.Response(200, text="redirected content")
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("https://example.com/old"))

    assert result["error"] == ""
    assert result["status_code"] == 200
    assert result["final_url"] == "https://example.com/new"
    assert "redirected content" in result["text"]


@pytest.mark.asyncio
async def test_fetch_truncation(respx_mock: respx.MockRouter) -> None:
    """When body exceeds max_body_bytes, reading stops at the cap."""
    big_body = "x" * 5000
    respx_mock.get("https://example.com/large").mock(
        return_value=httpx.Response(200, text=big_body)
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings(max_body_bytes=1000))
        result = json.loads(await tools[0]("https://example.com/large"))

    assert result["error"] == ""
    # Streaming reads stop at the cap — body_size_bytes reports bytes actually
    # read (≤ max_body_bytes + chunk_size), not the full remote size.
    assert 1000 <= result["body_size_bytes"] <= 1000 + 65536
    assert len(result["text"]) == 1000
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# URL scheme validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_rejects_non_http_scheme() -> None:
    """ftp:// and other schemes are blocked."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("ftp://example.com/file"))

    assert "scheme" in result["error"].lower()
    assert result["text"] == ""


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

_PRIVATE_IP_SOCKADDR = [
    (
        socket.AF_INET,
        socket.SOCK_STREAM,
        6,
        "",
        ("127.0.0.1", 0),
    )
]


@pytest.mark.asyncio
async def test_fetch_blocks_loopback() -> None:
    """127.0.0.1 is blocked by SSRF protection."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_PRIVATE_IP_SOCKADDR,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("http://127.0.0.1/admin"))

    assert "SSRF" in result["error"]
    assert result["text"] == ""


@pytest.mark.asyncio
async def test_fetch_blocks_private_10() -> None:
    """10.x.x.x is blocked by SSRF protection."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.1", 0),
            )
        ],
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("http://10.0.0.1/"))

    assert "SSRF" in result["error"]


@pytest.mark.asyncio
async def test_fetch_blocks_private_192_168() -> None:
    """192.168.x.x is blocked by SSRF protection."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("192.168.1.1", 0),
            )
        ],
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("http://192.168.1.1/"))

    assert "SSRF" in result["error"]


@pytest.mark.asyncio
async def test_fetch_blocks_private_172_16() -> None:
    """172.16.x.x is blocked by SSRF protection."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("172.16.0.1", 0),
            )
        ],
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("http://172.16.0.1/"))

    assert "SSRF" in result["error"]


@pytest.mark.asyncio
async def test_fetch_blocks_link_local() -> None:
    """169.254.x.x is blocked by SSRF protection."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("169.254.169.254", 0),
            )
        ],
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("http://169.254.169.254/latest/meta-data/"))

    assert "SSRF" in result["error"]


@pytest.mark.asyncio
async def test_fetch_blocks_ipv4_mapped_ipv6_loopback() -> None:
    """IPv4-mapped IPv6 loopback (::ffff:127.0.0.1) is blocked."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("::ffff:127.0.0.1", 0),
            )
        ],
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("http://example.com/"))

    assert "SSRF" in result["error"]


@pytest.mark.asyncio
async def test_fetch_blocks_ipv4_mapped_ipv6_private_10() -> None:
    """IPv4-mapped IPv6 10.x (::ffff:10.0.0.1) is blocked."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("::ffff:10.0.0.1", 0),
            )
        ],
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("http://example.com/"))

    assert "SSRF" in result["error"]


@pytest.mark.asyncio
async def test_fetch_blocks_zero_address() -> None:
    """0.0.0.0 is blocked by SSRF protection."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("0.0.0.0", 0),
            )
        ],
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("http://0.0.0.0/"))

    assert "SSRF" in result["error"]


@pytest.mark.asyncio
async def test_fetch_blocks_unresolvable_host() -> None:
    """A hostname that fails DNS resolution is treated as unsafe."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        side_effect=socket.gaierror("Name or service not known"),
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("http://nonexistent.invalid/"))

    assert "SSRF" in result["error"]


@pytest.mark.asyncio
async def test_fetch_ssrf_redirect_check(respx_mock: respx.MockRouter) -> None:
    """SSRF check runs on the redirect target before following it."""
    respx_mock.get("https://example.com/goto").mock(
        return_value=httpx.Response(301, headers={"Location": "http://127.0.0.1/admin"})
    )

    def _getaddrinfo(host, port):
        if host == "127.0.0.1":
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("127.0.0.1", 0),
                )
            ]
        # public IP for everything else
        return _MOCK_SOCKET_RETURN

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        side_effect=_getaddrinfo,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("https://example.com/goto"))

    assert "SSRF" in result["error"]
    assert result["final_url"] == "http://127.0.0.1/admin"


# ---------------------------------------------------------------------------
# Domain allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_allowlist_permits_listed_host(
    respx_mock: respx.MockRouter,
) -> None:
    """A host in the allowlist is permitted through."""
    respx_mock.get("https://allowed.example.com/file").mock(
        return_value=httpx.Response(200, text="allowed")
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(
            _settings(domain_allowlist=["allowed.example.com"])
        )
        result = json.loads(await tools[0]("https://allowed.example.com/file"))

    assert result["error"] == ""
    assert result["status_code"] == 200


@pytest.mark.asyncio
async def test_fetch_allowlist_blocks_unlisted_host() -> None:
    """A host not in the allowlist is blocked."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(
            _settings(domain_allowlist=["allowed.example.com"])
        )
        result = json.loads(await tools[0]("https://evil.example.com/file"))

    assert "allowlist" in result["error"].lower()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_rate_limit_blocks_excess(
    respx_mock: respx.MockRouter,
) -> None:
    """Exceeding the rate limit returns a clear error."""
    respx_mock.get("https://example.com/").mock(
        return_value=httpx.Response(200, text="ok")
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(
            _settings(rate_limit_requests=1, rate_limit_window_seconds=60.0)
        )
        # First request should succeed
        r1 = json.loads(await tools[0]("https://example.com/"))
        assert r1["error"] == ""

        # Second request should be rate-limited
        r2 = json.loads(await tools[0]("https://example.com/"))
        assert "rate limit" in r2["error"].lower()


# ---------------------------------------------------------------------------
# Auth-required (401/403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_401_reports_auth_required(
    respx_mock: respx.MockRouter,
) -> None:
    """401 Unauthorized → clear auth-required error."""
    respx_mock.get("https://example.com/private").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("https://example.com/private"))

    assert "authentication" in result["error"].lower()
    assert result["status_code"] == 401


@pytest.mark.asyncio
async def test_fetch_403_reports_auth_required(
    respx_mock: respx.MockRouter,
) -> None:
    """403 Forbidden → clear auth-required error."""
    respx_mock.get("https://example.com/private").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("https://example.com/private"))

    assert "authentication" in result["error"].lower()
    assert result["status_code"] == 403


# ---------------------------------------------------------------------------
# Fleet auth (Basic-Auth injection, SSRF bypass, allowlist bypass)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_timeout(respx_mock: respx.MockRouter) -> None:
    """Timeout → error message."""
    respx_mock.get("https://example.com/").mock(
        side_effect=httpx.TimeoutException("timed out")
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("https://example.com/"))

    assert "timed out" in result["error"].lower()


@pytest.mark.asyncio
async def test_fetch_too_many_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    """TooManyRedirects → error message."""
    respx_mock.get("https://example.com/").mock(
        side_effect=httpx.TooManyRedirects("too many")
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("https://example.com/"))

    assert "redirect" in result["error"].lower()


# ---------------------------------------------------------------------------
# 404 — still returns structured data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_404_returns_error(
    respx_mock: respx.MockRouter,
) -> None:
    """A 404 response returns status info and an error message."""
    respx_mock.get("https://example.com/missing").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        tools = build_public_fetch_tools(_settings())
        result = json.loads(await tools[0]("https://example.com/missing"))

    assert result["status_code"] == 404
    assert result["error"] != ""
    assert "404" in result["error"]


# ---------------------------------------------------------------------------
# No hostname
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_no_hostname() -> None:
    """A URL without a hostname is rejected."""
    tools = build_public_fetch_tools(_settings())
    result = json.loads(await tools[0]("http:///path"))

    assert "no hostname" in result["error"].lower()


# ---------------------------------------------------------------------------
# Fleet components — resolved from the central-deploy roster
# ---------------------------------------------------------------------------


def _roster(*base_urls: str):
    """Patch the central-deploy roster to advertise *base_urls*."""

    async def _fake_fetch_roster(_settings):
        return [{"id": f"c{i}", "base_url": u} for i, u in enumerate(base_urls)]

    return mock.patch(
        "robotsix_chat.component_access.roster.fetch_roster", _fake_fetch_roster
    )


def _central_deploy():
    from robotsix_chat.config.models import CentralDeploySettings

    return CentralDeploySettings(url="http://central-deploy:8100")


@pytest.mark.asyncio
async def test_roster_host_bypasses_domain_allowlist(
    respx_mock: respx.MockRouter,
) -> None:
    """A component in the roster is fetchable though absent from the allowlist."""
    respx_mock.get("http://mail:8080/api").mock(
        return_value=httpx.Response(200, text="ok")
    )

    settings = _settings(domain_allowlist=["public.example.com"])
    with (
        mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_MOCK_SOCKET_RETURN,
        ),
        _roster("http://mail:8080"),
    ):
        tools = build_public_fetch_tools(settings, _central_deploy())
        result = json.loads(await tools[0]("http://mail:8080/api"))

    assert result["status_code"] == 200
    assert "allowlist" not in result["error"].lower()
    # Internal network — no credential is attached.
    assert "Authorization" not in respx_mock.calls.last.request.headers


@pytest.mark.asyncio
async def test_roster_host_bypasses_ssrf_on_initial_host(
    respx_mock: respx.MockRouter,
) -> None:
    """A component address resolving to a private IP is still fetchable."""
    respx_mock.get("http://mail:8080/api").mock(
        return_value=httpx.Response(200, text="ok")
    )

    with (
        mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_PRIVATE_IP_SOCKADDR,
        ),
        _roster("http://mail:8080"),
    ):
        tools = build_public_fetch_tools(_settings(), _central_deploy())
        result = json.loads(await tools[0]("http://mail:8080/api"))

    assert result["status_code"] == 200
    assert "SSRF" not in result["error"]


@pytest.mark.asyncio
async def test_roster_host_bypasses_ssrf_on_redirect_hop(
    respx_mock: respx.MockRouter,
) -> None:
    """The per-hop SSRF check also exempts component addresses."""
    respx_mock.get("http://mill:8077/goto").mock(
        return_value=httpx.Response(301, headers={"Location": "http://mail:8080/api"})
    )
    respx_mock.get("http://mail:8080/api").mock(
        return_value=httpx.Response(200, text="ok")
    )

    def _getaddrinfo(host, port):
        if host == "mail":
            return _PRIVATE_IP_SOCKADDR
        return _MOCK_SOCKET_RETURN

    with (
        mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            side_effect=_getaddrinfo,
        ),
        _roster("http://mill:8077", "http://mail:8080"),
    ):
        tools = build_public_fetch_tools(_settings(), _central_deploy())
        result = json.loads(await tools[0]("http://mill:8077/goto"))

    assert result["status_code"] == 200
    assert "SSRF" not in result["error"]


@pytest.mark.asyncio
async def test_non_roster_private_host_still_blocked(
    respx_mock: respx.MockRouter,
) -> None:
    """A private host that is not a component is still refused."""
    with (
        mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_PRIVATE_IP_SOCKADDR,
        ),
        _roster("http://mail:8080"),
    ):
        tools = build_public_fetch_tools(_settings(), _central_deploy())
        result = json.loads(await tools[0]("http://internal.example.com/secrets"))

    assert "SSRF" in result["error"]

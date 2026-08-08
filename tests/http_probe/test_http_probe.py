"""Tests for the HTTP probe tool.

:func:`build_http_probe_tools` with ``respx`` mocked so there are
no real network calls.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from unittest import mock

import httpx
import pytest
import respx

from robotsix_chat.config import HttpProbeSettings
from robotsix_chat.config.models import FleetAuthSettings
from robotsix_chat.http_probe import build_http_probe_tools, load_http_probe_skill

# ---------------------------------------------------------------------------
# DNS mock — return a public IP so SSRF checks pass in sandbox
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


@pytest.fixture(autouse=True)
def _mock_dns():
    """Mock DNS resolution to return a public IP for all tests.

    ``_host_is_private`` from ``common.http_fetch`` calls
    ``socket.getaddrinfo`` directly — without this mock, tests in the
    sandbox (no DNS) would always treat every hostname as private and
    block every SSRF-gated request.
    """
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_MOCK_SOCKET_RETURN,
    ):
        yield


def _settings(**kw: Any) -> HttpProbeSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "timeout": 10.0,
        "allowlist": ["www.robotsix.net", "robotsix.net"],
        "max_body_bytes": 2048,
        "max_redirects": 5,
    }
    base.update(kw)
    return HttpProbeSettings(**base)


def _tools(fleet_auth: FleetAuthSettings | None = None, **kw: Any) -> list[Any]:
    """Build the tool. fleet_auth is a top-level setting, not a per-tool one."""
    return build_http_probe_tools(_settings(**kw), fleet_auth)


# ---------------------------------------------------------------------------
# build_http_probe_tools
# ---------------------------------------------------------------------------


def test_build_http_probe_tools_disabled() -> None:
    """Disabled http_probe returns no tools."""
    assert build_http_probe_tools(HttpProbeSettings(enabled=False)) == []


def test_build_http_probe_tools_returns_one_tool() -> None:
    """Enabled http_probe returns exactly one tool: http_probe."""
    tools = build_http_probe_tools(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "http_probe"


# ---------------------------------------------------------------------------
# load_http_probe_skill
# ---------------------------------------------------------------------------


def test_load_http_probe_skill_returns_non_empty_markdown() -> None:
    """The shipped skill.md is loadable and describes the tool."""
    skill = load_http_probe_skill()
    assert len(skill) > 100
    assert "http_probe" in skill
    assert "read-only" in skill.lower()


# ---------------------------------------------------------------------------
# http_probe tool — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_probe_basic_success(respx_mock: respx.MockRouter) -> None:
    """A successful probe returns healthy=True with correct fields."""
    route = respx_mock.get("https://www.robotsix.net/").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text="<html><head><title>Robotsix</title></head><body>hello</body></html>",
        )
    )

    tools = build_http_probe_tools(_settings())
    result = json.loads(await tools[0]("https://www.robotsix.net/"))

    assert route.called
    assert result["healthy"] is True
    assert result["status_code"] == 200
    assert result["final_url"] == "https://www.robotsix.net/"
    assert result["response_time_ms"] is not None
    assert result["response_time_ms"] > 0
    assert "text/html" in result["content_type"]
    assert result["body_size_bytes"] > 0
    assert "Robotsix" in result["body_snippet"]
    assert result["error"] == ""


@pytest.mark.asyncio
async def test_http_probe_with_assertions_pass(
    respx_mock: respx.MockRouter,
) -> None:
    """All assertions pass — healthy=True."""
    respx_mock.get("https://www.robotsix.net/").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><title>Robotsix — AI Automation</title></html>",
        )
    )

    tools = build_http_probe_tools(_settings())
    result = json.loads(
        await tools[0](
            "https://www.robotsix.net/",
            expect_status=200,
            expect_contains=["Robotsix"],
            expect_absent=["Index of /", "Not Found"],
        )
    )

    assert result["healthy"] is True
    assert len(result["checks"]) == 4  # status + 1 expect_contains + 2 expect_absent
    assert all(c["passed"] for c in result["checks"])


@pytest.mark.asyncio
async def test_http_probe_redirect_followed(
    respx_mock: respx.MockRouter,
) -> None:
    """Redirects are followed and final_url reflects the target."""
    respx_mock.get("https://robotsix.net/").mock(
        return_value=httpx.Response(
            301, headers={"Location": "https://www.robotsix.net/"}
        )
    )
    respx_mock.get("https://www.robotsix.net/").mock(
        return_value=httpx.Response(200, text="landing page")
    )

    tools = build_http_probe_tools(_settings())
    result = json.loads(await tools[0]("https://robotsix.net/"))

    assert result["healthy"] is True
    assert result["status_code"] == 200
    assert result["final_url"] == "https://www.robotsix.net/"


# ---------------------------------------------------------------------------
# Assertion failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_probe_status_mismatch_unhealthy(
    respx_mock: respx.MockRouter,
) -> None:
    """When status doesn't match expect_status, healthy=False."""
    respx_mock.get("https://www.robotsix.net/").mock(
        return_value=httpx.Response(404, text="not found")
    )

    tools = build_http_probe_tools(_settings())
    result = json.loads(await tools[0]("https://www.robotsix.net/", expect_status=200))

    assert result["healthy"] is False
    assert result["status_code"] == 404
    assert len(result["checks"]) == 1
    assert result["checks"][0]["check"] == "status"
    assert result["checks"][0]["passed"] is False


@pytest.mark.asyncio
async def test_http_probe_expect_contains_fails(
    respx_mock: respx.MockRouter,
) -> None:
    """Missing expected substring → unhealthy."""
    respx_mock.get("https://www.robotsix.net/").mock(
        return_value=httpx.Response(200, text="some unrelated content")
    )

    tools = build_http_probe_tools(_settings())
    result = json.loads(
        await tools[0](
            "https://www.robotsix.net/",
            expect_contains=["Robotsix"],
        )
    )

    assert result["healthy"] is False
    contains_check = [c for c in result["checks"] if "expect_contains" in c["check"]]
    assert len(contains_check) == 1
    assert contains_check[0]["passed"] is False


@pytest.mark.asyncio
async def test_http_probe_expect_absent_fails(
    respx_mock: respx.MockRouter,
) -> None:
    """Forbidden substring present → unhealthy."""
    respx_mock.get("https://www.robotsix.net/").mock(
        return_value=httpx.Response(200, text="Index of /")
    )

    tools = build_http_probe_tools(_settings())
    result = json.loads(
        await tools[0](
            "https://www.robotsix.net/",
            expect_absent=["Index of /"],
        )
    )

    assert result["healthy"] is False
    absent_check = [c for c in result["checks"] if "expect_absent" in c["check"]]
    assert len(absent_check) == 1
    assert absent_check[0]["passed"] is False


# ---------------------------------------------------------------------------
# Hostname allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_probe_hostname_not_in_allowlist() -> None:
    """Probing an unlisted host returns healthy=False with a clear error."""
    tools = build_http_probe_tools(_settings())
    result = json.loads(await tools[0]("https://evil.example.com/"))

    assert result["healthy"] is False
    assert "allowlist" in result["error"].lower()


@pytest.mark.asyncio
async def test_http_probe_empty_allowlist_allows_any_host(
    respx_mock: respx.MockRouter,
) -> None:
    """An empty allowlist permits any hostname."""
    respx_mock.get("https://any.example.com/").mock(
        return_value=httpx.Response(200, text="ok")
    )

    tools = build_http_probe_tools(_settings(allowlist=[]))
    result = json.loads(await tools[0]("https://any.example.com/"))

    assert result["healthy"] is True
    assert result["status_code"] == 200


# ---------------------------------------------------------------------------
# URL scheme validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_probe_rejects_non_http_scheme() -> None:
    """ftp:// and other schemes are blocked."""
    tools = build_http_probe_tools(_settings())
    result = json.loads(await tools[0]("ftp://www.robotsix.net/file"))

    assert result["healthy"] is False
    assert "scheme" in result["error"].lower()


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_probe_blocks_private_ip_loopback() -> None:
    """127.0.0.1 is blocked by SSRF protection."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 0),
            )
        ],
    ):
        tools = build_http_probe_tools(_settings(allowlist=["127.0.0.1"]))
        result = json.loads(await tools[0]("http://127.0.0.1/"))

    assert result["healthy"] is False
    assert "SSRF" in result["error"]


@pytest.mark.asyncio
async def test_http_probe_blocks_private_ip_10() -> None:
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
        tools = build_http_probe_tools(_settings(allowlist=["10.0.0.1"]))
        result = json.loads(await tools[0]("http://10.0.0.1/"))

    assert result["healthy"] is False
    assert "SSRF" in result["error"]


@pytest.mark.asyncio
async def test_http_probe_fleet_auth_host_skips_ssrf(
    respx_mock: respx.MockRouter,
) -> None:
    """Fleet-auth hosts skip the SSRF check (operator-trusted)."""
    respx_mock.get("https://deploy.robotsix.net/internal").mock(
        return_value=httpx.Response(200, text="ok")
    )

    # deploy.robotsix.net is in fleet_auth_hosts → SSRF check skipped.
    # Mock DNS to return a private IP — the probe should still succeed
    # because fleet-auth hosts are trusted.
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.5", 0),
            )
        ],
    ):
        tools = _tools(
            allowlist=["deploy.robotsix.net"],
            fleet_auth=FleetAuthSettings(
                basic_auth_username="op",
                basic_auth_password="pw",  # pragma: allowlist secret
                auth_hosts=["deploy.robotsix.net"],
            ),
        )
        result = json.loads(await tools[0]("https://deploy.robotsix.net/internal"))

    assert result["healthy"] is True
    assert result["body_snippet"] == "ok"


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_probe_timeout(respx_mock: respx.MockRouter) -> None:
    """Timeout → healthy=False with timeout error message."""
    respx_mock.get("https://www.robotsix.net/").mock(
        side_effect=httpx.TimeoutException("timed out")
    )

    tools = build_http_probe_tools(_settings())
    result = json.loads(await tools[0]("https://www.robotsix.net/"))

    assert result["healthy"] is False
    assert "timed out" in result["error"].lower()


@pytest.mark.asyncio
async def test_http_probe_too_many_redirects(
    respx_mock: respx.MockRouter,
) -> None:
    """TooManyRedirects → healthy=False."""
    respx_mock.get("https://www.robotsix.net/").mock(
        side_effect=httpx.TooManyRedirects("too many")
    )

    tools = build_http_probe_tools(_settings())
    result = json.loads(await tools[0]("https://www.robotsix.net/"))

    assert result["healthy"] is False
    assert "redirect" in result["error"].lower()


# ---------------------------------------------------------------------------
# HTTP errors (4xx/5xx) — still return structured data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_probe_500_still_returns_body(
    respx_mock: respx.MockRouter,
) -> None:
    """A 500 response still returns status/body/healthy=False."""
    respx_mock.get("https://www.robotsix.net/").mock(
        return_value=httpx.Response(
            500,
            headers={"Content-Type": "text/plain"},
            text="Internal Server Error",
        )
    )

    tools = build_http_probe_tools(_settings())
    result = json.loads(await tools[0]("https://www.robotsix.net/"))

    assert result["healthy"] is False  # status mismatch (expect 200)
    assert result["status_code"] == 500
    assert result["body_snippet"] == "Internal Server Error"
    assert "text/plain" in result["content_type"]
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# Fleet auth — basic-auth header injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_probe_fleet_auth_sends_header(
    respx_mock: respx.MockRouter,
) -> None:
    """When fleet_auth is configured and host matches, basic auth header is sent."""
    respx_mock.get("https://deploy.robotsix.net/docs").mock(
        return_value=httpx.Response(200, text="authenticated")
    )

    tools = _tools(
        allowlist=["deploy.robotsix.net"],
        fleet_auth=FleetAuthSettings(
            basic_auth_username="operator",
            basic_auth_password="s3cret",  # pragma: allowlist secret
            auth_hosts=["deploy.robotsix.net"],
        ),
    )
    result = json.loads(await tools[0]("https://deploy.robotsix.net/docs"))

    assert result["healthy"] is True
    assert result["body_snippet"] == "authenticated"

    # Verify the Authorization header was sent.
    last_request = respx_mock.calls.last.request
    auth = last_request.headers.get("Authorization", "")
    assert auth.startswith("Basic ")
    import base64 as _b64

    decoded = _b64.b64decode(auth.removeprefix("Basic ")).decode()
    assert decoded == "operator:s3cret"  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_http_probe_fleet_auth_host_not_matching(
    respx_mock: respx.MockRouter,
) -> None:
    """Fleet auth is NOT sent when the host is not in auth_hosts."""
    respx_mock.get("https://www.robotsix.net/").mock(
        return_value=httpx.Response(200, text="public")
    )

    tools = _tools(
        fleet_auth=FleetAuthSettings(
            basic_auth_username="operator",
            basic_auth_password="s3cret",  # pragma: allowlist secret
            auth_hosts=["deploy.robotsix.net"],
        ),
    )
    result = json.loads(await tools[0]("https://www.robotsix.net/"))

    assert result["healthy"] is True
    assert result["body_snippet"] == "public"

    last_request = respx_mock.calls.last.request
    assert "Authorization" not in last_request.headers


@pytest.mark.asyncio
async def test_http_probe_fleet_auth_none(
    respx_mock: respx.MockRouter,
) -> None:
    """No fleet_auth configured → no auth header sent."""
    respx_mock.get("https://www.robotsix.net/").mock(
        return_value=httpx.Response(200, text="public")
    )

    tools = _tools(fleet_auth=None)
    result = json.loads(await tools[0]("https://www.robotsix.net/"))

    assert result["healthy"] is True
    last_request = respx_mock.calls.last.request
    assert "Authorization" not in last_request.headers


@pytest.mark.asyncio
async def test_http_probe_fleet_auth_host_allowed_without_main_allowlist(
    respx_mock: respx.MockRouter,
) -> None:
    """Fleet-auth hosts are implicitly allowed even when not in the main allowlist."""
    respx_mock.get("https://deploy.robotsix.net/docs").mock(
        return_value=httpx.Response(200, text="authenticated")
    )

    # The main allowlist does NOT include deploy.robotsix.net.
    tools = _tools(
        allowlist=["www.robotsix.net", "robotsix.net"],
        fleet_auth=FleetAuthSettings(
            basic_auth_username="operator",
            basic_auth_password="s3cret",  # pragma: allowlist secret
            auth_hosts=["deploy.robotsix.net"],
        ),
    )
    result = json.loads(await tools[0]("https://deploy.robotsix.net/docs"))

    assert result["healthy"] is True
    assert result["body_snippet"] == "authenticated"

    # Verify the Authorization header was sent.
    last_request = respx_mock.calls.last.request
    auth = last_request.headers.get("Authorization", "")
    assert auth.startswith("Basic ")


@pytest.mark.asyncio
async def test_http_probe_fleet_auth_empty_credentials_no_header(
    respx_mock: respx.MockRouter,
) -> None:
    """When username or password is empty, no auth header is sent."""
    respx_mock.get("https://deploy.robotsix.net/docs").mock(
        return_value=httpx.Response(200, text="ok")
    )

    tools = _tools(
        allowlist=["deploy.robotsix.net"],
        fleet_auth=FleetAuthSettings(
            basic_auth_username="",
            basic_auth_password="",
            auth_hosts=["deploy.robotsix.net"],
        ),
    )
    result = json.loads(await tools[0]("https://deploy.robotsix.net/docs"))

    assert result["healthy"] is True
    last_request = respx_mock.calls.last.request
    assert "Authorization" not in last_request.headers

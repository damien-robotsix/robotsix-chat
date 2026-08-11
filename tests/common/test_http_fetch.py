"""Dedicated unit tests for the shared HTTP-fetch helpers.

Covers the three internal helpers in :mod:`robotsix_chat.common.http_fetch`:

- :func:`_host_is_private` — SSRF protection against private/internal IPs
- :func:`_check_hostname_allowlist` — hostname allowlist enforcement
- :func:`_validate_url_scheme` — restricts to http/https only
"""

from __future__ import annotations

import socket
from unittest import mock

import pytest

from robotsix_chat.common.http_fetch import (
    _check_hostname_allowlist,
    _host_is_private,
    _validate_url_scheme,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _addrinfo(ip: str) -> list[tuple]:
    """Return a single-entry ``getaddrinfo``-shaped list for *ip*."""
    return [
        (
            socket.AF_INET if ":" not in ip else socket.AF_INET6,
            socket.SOCK_STREAM,
            6,
            "",
            (ip, 0),
        )
    ]


# ---------------------------------------------------------------------------
# _host_is_private — private IPv4 ranges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip_address,description",
    [
        ("127.0.0.1", "loopback (127.0.0.0/8)"),
        ("10.1.2.3", "class-A private (10.0.0.0/8)"),
        ("172.16.0.1", "class-B private low (172.16.0.0/12)"),
        ("172.31.255.255", "class-B private high (172.16.0.0/12)"),
        ("192.168.1.1", "class-C private (192.168.0.0/16)"),
        ("169.254.1.1", "link-local (169.254.0.0/16)"),
        ("0.0.0.1", "this-network (0.0.0.0/8)"),
    ],
)
def test_host_is_private_ipv4_blocked(ip_address: str, description: str) -> None:
    """Private IPv4 addresses are detected as private."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_addrinfo(ip_address),
    ):
        assert _host_is_private(ip_address) is True, description


# ---------------------------------------------------------------------------
# _host_is_private — private IPv6 ranges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip_address,description",
    [
        ("::1", "loopback (::1/128)"),
        ("fc00::1", "unique-local low (fc00::/7)"),
        ("fdff::1", "unique-local high (fc00::/7)"),
        ("fe80::1", "link-local (fe80::/10)"),
    ],
)
def test_host_is_private_ipv6_blocked(ip_address: str, description: str) -> None:
    """Private IPv6 addresses are detected as private."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_addrinfo(ip_address),
    ):
        assert _host_is_private(ip_address) is True, description


# ---------------------------------------------------------------------------
# _host_is_private — public IPs pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip_address,description",
    [
        ("8.8.8.8", "Google DNS (public IPv4)"),
        ("93.184.216.34", "example.com (public IPv4)"),
        ("2001:4860:4860::8888", "Google DNS (public IPv6)"),
    ],
)
def test_host_is_private_public_passes(ip_address: str, description: str) -> None:
    """Public IP addresses are not detected as private."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_addrinfo(ip_address),
    ):
        assert _host_is_private(ip_address) is False, description


# ---------------------------------------------------------------------------
# _host_is_private — IPv4-mapped IPv6 (defence in depth)
# ---------------------------------------------------------------------------


def test_host_is_private_ipv4_mapped_private_embedded() -> None:
    """IPv4-mapped address with private embedded IPv4 is blocked."""
    # ::ffff:10.0.0.1 → embedded IPv4 10.0.0.1 is private
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_addrinfo("::ffff:10.0.0.1"),
    ):
        assert _host_is_private("::ffff:10.0.0.1") is True


def test_host_is_private_ipv4_mapped_public_embedded() -> None:
    """IPv4-mapped address with public embedded IPv4 is still blocked.

    Even when the embedded IPv4 is public, the mapped address itself falls
    within ``::ffff:0:0/96`` which is listed in ``_PRIVATE_NETWORKS``.
    """
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        return_value=_addrinfo("::ffff:8.8.8.8"),
    ):
        assert _host_is_private("::ffff:8.8.8.8") is True


# ---------------------------------------------------------------------------
# _host_is_private — unresolvable host
# ---------------------------------------------------------------------------


def test_host_is_private_gaierror_treated_as_unsafe() -> None:
    """A host that cannot be resolved is treated as private (unsafe)."""
    with mock.patch(
        "robotsix_chat.common.http_fetch.socket.getaddrinfo",
        side_effect=socket.gaierror("Name or service not known"),
    ):
        assert _host_is_private("does-not-exist.invalid") is True


# ---------------------------------------------------------------------------
# _check_hostname_allowlist
# ---------------------------------------------------------------------------


def test_check_hostname_allowlist_allowed_host_passes() -> None:
    """An allowlisted host returns ``None`` (no error)."""
    result = _check_hostname_allowlist(
        hostname="example.com",
        allowed_hosts={"example.com", "example.org"},
        fleet_hosts=set(),
        tool_name="test-tool",
    )
    assert result is None


def test_check_hostname_allowlist_non_allowed_host_fails() -> None:
    """A non-allowlisted host returns an error message."""
    result = _check_hostname_allowlist(
        hostname="evil.com",
        allowed_hosts={"example.com"},
        fleet_hosts=set(),
        tool_name="test-tool",
    )
    assert result is not None
    assert "evil.com" in result
    assert "test-tool" in result
    assert "example.com" in result


def test_check_hostname_allowlist_fleet_host_implicit_pass() -> None:
    """Fleet component hosts pass the allowlist check implicitly."""
    result = _check_hostname_allowlist(
        hostname="internal-fleet.local",
        allowed_hosts={"example.com"},
        fleet_hosts={"internal-fleet.local"},
        tool_name="test-tool",
    )
    assert result is None


def test_check_hostname_allowlist_empty_allowlist_passes_all() -> None:
    """An empty allowlist means any hostname is permitted."""
    result = _check_hostname_allowlist(
        hostname="anything.example",
        allowed_hosts=set(),
        fleet_hosts=set(),
        tool_name="test-tool",
    )
    assert result is None


def test_check_hostname_allowlist_fleet_host_in_error_message() -> None:
    """Error message includes fleet hosts in sorted allowed list."""
    result = _check_hostname_allowlist(
        hostname="evil.com",
        allowed_hosts={"example.com"},
        fleet_hosts={"fleet.local"},
        tool_name="test-tool",
    )
    assert result is not None
    assert "fleet.local" in result
    assert "example.com" in result


# ---------------------------------------------------------------------------
# _validate_url_scheme
# ---------------------------------------------------------------------------


def test_validate_url_scheme_http_passes() -> None:
    """``http`` scheme is allowed."""
    assert _validate_url_scheme("http") is None


def test_validate_url_scheme_https_passes() -> None:
    """``https`` scheme is allowed."""
    assert _validate_url_scheme("https") is None


@pytest.mark.parametrize(
    "scheme",
    ["ftp", "file", "gopher", "ws", "wss", "data", "javascript"],
)
def test_validate_url_scheme_disallowed_schemes_fail(scheme: str) -> None:
    """Non-http/https schemes are rejected."""
    result = _validate_url_scheme(scheme)
    assert result is not None
    assert scheme in result
    assert "only http and https" in result


def test_validate_url_scheme_empty_string_fails() -> None:
    """An empty scheme string is rejected."""
    result = _validate_url_scheme("")
    assert result is not None

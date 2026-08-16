"""Unit tests for :mod:`robotsix_chat.common.http_fetch`.

Covers the three SSRF / allowlist / URL-scheme helpers directly,
without going through http_probe or public_fetch tool wrappers.
"""

from __future__ import annotations

import socket
from unittest import mock

from robotsix_chat.common.http_fetch import (
    _check_hostname_allowlist,
    _host_is_private,
    _validate_url_scheme,
)

# ---------------------------------------------------------------------------
# _host_is_private
# ---------------------------------------------------------------------------


def _mock_addrinfo(*ip_strings: str):
    """Build a ``socket.getaddrinfo`` return for one or more IP strings."""
    results = []
    for ip_str in ip_strings:
        results.append(
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (ip_str, 0),
            )
        )
    return results


class TestHostIsPrivate:
    """Unit tests for :func:`_host_is_private` SSRF guard."""

    def test_public_ip_is_not_private(self):
        """A public IP (93.184.216.34) is not private."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("93.184.216.34"),
        ):
            assert _host_is_private("example.com") is False

    def test_loopback_v4_is_private(self):
        """127.0.0.1 is loopback and treated as private."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("127.0.0.1"),
        ):
            assert _host_is_private("localhost") is True

    def test_private_10x_is_private(self):
        """10.x.x.x is RFC 1918 private."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("10.1.2.3"),
        ):
            assert _host_is_private("internal") is True

    def test_private_172_16_x_is_private(self):
        """172.16.x.x (lower bound of RFC 1918 /12) is private."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("172.16.99.99"),
        ):
            assert _host_is_private("internal") is True

    def test_private_172_31_x_is_private(self):
        """172.31.x.x (upper bound of RFC 1918 /12) is private."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("172.31.255.255"),
        ):
            assert _host_is_private("internal") is True

    def test_private_192_168_x_is_private(self):
        """192.168.x.x is RFC 1918 private."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("192.168.0.1"),
        ):
            assert _host_is_private("internal") is True

    def test_link_local_v4_is_private(self):
        """169.254.x.x link-local is treated as private."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("169.254.1.1"),
        ):
            assert _host_is_private("linklocal") is True

    def test_loopback_v6_is_private(self):
        """::1 is the IPv6 loopback address."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("::1"),
        ):
            assert _host_is_private("localhost6") is True

    def test_unique_local_v6_is_private(self):
        """fc00::/7 is the IPv6 unique-local range."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("fc00::1"),
        ):
            assert _host_is_private("ula") is True

    def test_link_local_v6_is_private(self):
        """fe80::/10 is the IPv6 link-local range."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("fe80::1"),
        ):
            assert _host_is_private("linklocal6") is True

    def test_ipv4_mapped_loopback_is_private(self):
        """::ffff:127.0.0.1 is a mapped loopback — private."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("::ffff:127.0.0.1"),
        ):
            assert _host_is_private("mapped-loopback") is True

    def test_ipv4_mapped_arbitrary_v4_in_private_range(self):
        """Defence-in-depth: mapped address embedding a private IPv4."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("::ffff:10.0.0.1"),
        ):
            assert _host_is_private("mapped-private") is True

    def test_ipv4_mapped_public_v4_is_private(self):
        """All mapped addresses are private (::ffff:0:0/96 range check)."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("::ffff:93.184.216.34"),
        ):
            assert _host_is_private("mapped-public") is True

    def test_unresolvable_host_is_private(self):
        """A host that fails DNS resolution is treated as private (safe default)."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            side_effect=socket.gaierror("Name or service not known"),
        ):
            assert _host_is_private("no-such-host.invalid") is True

    def test_dual_stack_public_first(self):
        """First IP public, second private → host is private (any match wins)."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("93.184.216.34", "10.0.0.1"),
        ):
            assert _host_is_private("dual") is True

    def test_dual_stack_private_first(self):
        """First resolved IP is private → host is private."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("10.0.0.1", "93.184.216.34"),
        ):
            assert _host_is_private("dual") is True

    def test_all_zero_ip_is_private(self):
        """0.0.0.0 is in the private networks."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=_mock_addrinfo("0.0.0.0"),
        ):
            assert _host_is_private("zero") is True

    def test_invalid_ip_string_skipped(self):
        """A malformed IP in getaddrinfo result is silently skipped."""
        with mock.patch(
            "robotsix_chat.common.http_fetch.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            ],
        ):
            assert _host_is_private("broken-dns") is False


# ---------------------------------------------------------------------------
# _check_hostname_allowlist
# ---------------------------------------------------------------------------


class TestCheckHostnameAllowlist:
    """Unit tests for :func:`_check_hostname_allowlist`."""

    def test_empty_allowed_hosts_passes(self):
        """An empty allowlist permits any hostname."""
        assert _check_hostname_allowlist("any.host", set(), set(), "test_tool") is None

    def test_exact_host_in_allowlist_passes(self):
        """Exact hostname match in allowed_hosts passes."""
        assert (
            _check_hostname_allowlist(
                "example.com", {"example.com"}, set(), "test_tool"
            )
            is None
        )

    def test_host_not_in_allowlist_returns_error(self):
        """A hostname not in the allowlist returns a descriptive error."""
        err = _check_hostname_allowlist("evil.com", {"example.com"}, set(), "test_tool")
        assert err is not None
        assert "Hostname 'evil.com'" in err
        assert "not in the test_tool allowlist" in err
        assert "['example.com']" in err

    def test_fleet_auth_host_implicitly_allowed(self):
        """A fleet_auth host passes even when not in main allowed_hosts."""
        assert (
            _check_hostname_allowlist(
                "deploy.robotsix.net",
                {"example.com"},
                {"deploy.robotsix.net"},
                "test_tool",
            )
            is None
        )

    def test_block_message_includes_both_lists_sorted(self):
        """Block message lists sorted union of allowed_hosts and fleet_auth_hosts."""
        err = _check_hostname_allowlist(
            "bad.host",
            {"z.example", "a.example"},
            {"b.internal"},
            "probe_tool",
        )
        assert err is not None
        # sorted union: ['a.example', 'b.internal', 'z.example']
        assert "['a.example', 'b.internal', 'z.example']" in err
        assert "Hostname 'bad.host'" in err
        assert "probe_tool allowlist" in err

    def test_host_in_both_lists_passes(self):
        """A host present in both allowed_hosts and fleet_auth_hosts passes."""
        assert (
            _check_hostname_allowlist(
                "shared.host", {"shared.host"}, {"shared.host"}, "tool"
            )
            is None
        )

    def test_fleet_auth_implicit_bypass_even_when_allowlist_has_other_hosts(self):
        """Fleet auth hosts pass even though not in the main allowed_hosts."""
        assert (
            _check_hostname_allowlist(
                "auth-only.host",
                {"other.host"},
                {"auth-only.host"},
                "tool",
            )
            is None
        )


# ---------------------------------------------------------------------------
# _validate_url_scheme
# ---------------------------------------------------------------------------


class TestValidateUrlScheme:
    """Unit tests for :func:`_validate_url_scheme`."""

    def test_http_passes(self):
        """The ``http`` scheme is allowed."""
        assert _validate_url_scheme("http") is None

    def test_https_passes(self):
        """The ``https`` scheme is allowed."""
        assert _validate_url_scheme("https") is None

    def test_ftp_rejected(self):
        """The ``ftp`` scheme is rejected with an error message."""
        err = _validate_url_scheme("ftp")
        assert err is not None
        assert "ftp" in err

    def test_file_rejected(self):
        """The ``file`` scheme is rejected with an error message."""
        err = _validate_url_scheme("file")
        assert err is not None
        assert "file" in err

    def test_arbitrary_scheme_rejected(self):
        """An arbitrary scheme (gopher) is rejected."""
        err = _validate_url_scheme("gopher")
        assert err is not None
        assert "gopher" in err
        assert "http and https" in err

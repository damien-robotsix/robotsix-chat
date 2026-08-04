"""Shared HTTP-fetch helpers for http_probe and public_fetch.

Consolidates the duplicated logic that previously lived in both
``robotsix_chat.http_probe`` and ``robotsix_chat.public_fetch``:

- SSRF protection (private/internal IP network ranges + DNS-level check)
- Fleet-auth Basic-Auth header pre-computation
- Hostname allowlist check (with fleet_auth_hosts implicit-pass)
- URL scheme validation (http / https only)

These are internal helpers — not part of either tool's public API.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robotsix_chat.config import FleetAuthSettings

# ---------------------------------------------------------------------------
# SSRF protection — private / internal IP ranges
# ---------------------------------------------------------------------------

_PRIVATE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.IPv4Network("0.0.0.0/8"),  # "this host on this network"
    ipaddress.IPv4Network("127.0.0.0/8"),  # loopback
    ipaddress.IPv4Network("10.0.0.0/8"),  # private
    ipaddress.IPv4Network("172.16.0.0/12"),  # private
    ipaddress.IPv4Network("192.168.0.0/16"),  # private
    ipaddress.IPv4Network("169.254.0.0/16"),  # link-local
    ipaddress.IPv6Network("::1/128"),  # loopback
    ipaddress.IPv6Network("fc00::/7"),  # unique local
    ipaddress.IPv6Network("fe80::/10"),  # link-local
    ipaddress.IPv6Network("::ffff:0:0/96"),  # IPv4-mapped IPv6
)

# Sentinel network for explicit IPv4-mapped extraction (defence in depth —
# the ::ffff:0:0/96 entry above catches mapped addresses directly, but we
# also unpack the embedded IPv4 and check it against private IPv4 ranges).
_IPV4_MAPPED = ipaddress.IPv6Network("::ffff:0:0/96")

# Private IPv4 networks for IPv4-mapped extraction check.
_PRIVATE_V4_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
)


def _host_is_private(host: str) -> bool:
    """Return ``True`` when *host* resolves to any private/internal IP.

    A host that cannot be resolved at all is treated as unsafe (returns
    ``True``) so the tool rejects it rather than making a blind request.

    IPv4-mapped IPv6 addresses (``::ffff:x.x.x.x``) are handled in two
    layers: the mapped address itself is checked against the
    ``::ffff:0:0/96`` entry in ``_PRIVATE_NETWORKS``, **and** the
    embedded IPv4 is extracted and checked against the private IPv4
    ranges — defence in depth so a mapped address cannot slip through.
    """
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # can't resolve — treat as unsafe
    for _, _, _, _, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        # Defence in depth: extract embedded IPv4 from mapped addresses
        # and check against private IPv4 ranges.
        if isinstance(ip, ipaddress.IPv6Address) and ip in _IPV4_MAPPED:
            ipv4 = ip.ipv4_mapped
            if ipv4 is not None:
                for v4net in _PRIVATE_V4_NETWORKS:
                    if ipv4 in v4net:
                        return True
        for net in _PRIVATE_NETWORKS:
            if ip in net:
                return True
    return False


# ---------------------------------------------------------------------------
# Fleet-auth Basic-Auth header pre-computation
# ---------------------------------------------------------------------------


def _build_fleet_auth_header(
    fleet_auth: FleetAuthSettings | None,
) -> tuple[str | None, set[str]]:
    """Return ``(header_value, auth_hosts)`` from fleet-auth config.

    When *fleet_auth* is ``None`` or missing credentials, returns
    ``(None, set())`` — no header injection.
    """
    if fleet_auth is None:
        return None, set()
    username = fleet_auth.basic_auth_username
    password = fleet_auth.basic_auth_password.get_secret_value()
    if not username or not password:
        return None, set()
    import base64 as _base64

    encoded = _base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {encoded}", set(fleet_auth.auth_hosts)


# ---------------------------------------------------------------------------
# Hostname allowlist check
# ---------------------------------------------------------------------------


def _check_hostname_allowlist(
    hostname: str,
    allowed_hosts: set[str],
    fleet_auth_hosts: set[str],
    tool_name: str,
) -> str | None:
    """Return an error message when *hostname* is not allowed, or ``None``.

    Fleet-auth hosts are implicitly allowed (the operator explicitly listed
    them in ``auth_hosts``), so the agent can reach authenticated fleet UIs
    without duplicating every hostname in the main allowlist.

    An empty *allowed_hosts* set means any hostname is permitted.
    """
    if not allowed_hosts:
        return None
    if hostname in allowed_hosts or hostname in fleet_auth_hosts:
        return None
    all_allowed = sorted(allowed_hosts | fleet_auth_hosts)
    return (
        f"Hostname {hostname!r} is not in the {tool_name} allowlist. "
        f"Allowed hosts: {all_allowed}"
    )


# ---------------------------------------------------------------------------
# URL scheme validation
# ---------------------------------------------------------------------------


def _validate_url_scheme(scheme: str) -> str | None:
    """Return an error when *scheme* is not ``http`` or ``https``, else ``None``."""
    if scheme in ("http", "https"):
        return None
    return f"Unsupported URL scheme {scheme!r} — only http and https are allowed."

"""Shared HTTP-fetch helpers for http_probe and public_fetch.

Consolidates the duplicated logic that previously lived in both
``robotsix_chat.http_probe`` and ``robotsix_chat.public_fetch``:

- SSRF protection (private/internal IP network ranges + DNS-level check)
- Fleet-auth Basic-Auth header pre-computation
- Hostname allowlist check (with fleet-component implicit-pass)
- URL scheme validation (http / https only)

These are internal helpers — not part of either tool's public API.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import TYPE_CHECKING

import httpcore
import httpx

if TYPE_CHECKING:
    from robotsix_chat.config import CentralDeploySettings

logger = logging.getLogger(__name__)

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


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return ``True`` when a single resolved *ip* falls in a blocked range.

    Blocks private / loopback / link-local / unique-local / multicast /
    unspecified addresses, plus IPv4-mapped IPv6 addresses (both the mapped
    address itself via the ``::ffff:0:0/96`` entry and, defence in depth, the
    embedded IPv4 checked against the private IPv4 ranges).
    """
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
    # Additional non-routable categories (multicast / unspecified) that the
    # explicit tables above do not enumerate.
    return bool(ip.is_multicast or ip.is_unspecified)


def _host_is_private(host: str) -> bool:
    """Return ``True`` when *host* resolves to any private/internal IP.

    A host that cannot be resolved at all is treated as unsafe (returns
    ``True``) so the tool rejects it rather than making a blind request.

    IPv4-mapped IPv6 addresses (``::ffff:x.x.x.x``) are handled in two
    layers: the mapped address itself is checked against the
    ``::ffff:0:0/96`` entry in ``_PRIVATE_NETWORKS``, **and** the
    embedded IPv4 is extracted and checked against the private IPv4
    ranges — defence in depth so a mapped address cannot slip through.

    This is a *pre-flight* check only; httpx re-resolves the hostname when it
    opens the connection, so it does not by itself close the TOCTOU /
    DNS-rebinding gap. Route requests through :func:`build_ssrf_guarded_client`
    for connection-time validation.
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
        if _ip_is_blocked(ip):
            return True
    return False


# ---------------------------------------------------------------------------
# Connection-layer SSRF guard — validates the resolved IP at connect time
# ---------------------------------------------------------------------------


class _SSRFGuardBackend(httpcore.AsyncNetworkBackend):
    """Resolve and validate the target IP at connect time, then connect to it.

    This closes the TOCTOU / DNS-rebinding gap that a pre-flight
    ``getaddrinfo`` check leaves open: the IP validated here is the IP the
    socket connects to, because the resolved literal is passed straight to
    the inner backend (``getaddrinfo`` on an IP literal returns that literal,
    so no second, unchecked resolution can occur).

    Fleet-component hosts are exempt — their internal container addresses are
    private by design, and the operator granted access by enabling chat access
    on the component.
    """

    def __init__(
        self,
        fleet_hosts: set[str],
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._fleet_hosts = fleet_hosts
        self._inner: httpcore.AsyncNetworkBackend = (
            inner if inner is not None else httpcore.AnyIOBackend()
        )

    def _resolve_and_validate(self, host: str) -> str:
        """Return a validated IP literal to connect to, or raise ``ConnectError``.

        Fleet-component hosts pass through unresolved (connected by name).
        Any other host is resolved and the first non-blocked IP is returned;
        if every resolved address is blocked (or resolution fails), the
        connection is refused.
        """
        if host in self._fleet_hosts:
            return host
        try:
            addrinfo = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise httpcore.ConnectError(
                f"Cannot resolve host {host!r} — SSRF protection refused "
                "the connection."
            ) from exc
        for _, _, _, _, sockaddr in addrinfo:
            ip_str = str(sockaddr[0])
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if not _ip_is_blocked(ip):
                return ip_str
        raise httpcore.ConnectError(
            f"Host {host!r} resolves only to private/internal addresses — "
            "SSRF protection blocked the connection."
        )

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        target = self._resolve_and_validate(host)
        return await self._inner.connect_tcp(
            target,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,  # type: ignore[arg-type]
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._inner.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,  # type: ignore[arg-type]
        )

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class SSRFGuardPool(httpcore.AsyncConnectionPool):
    """Connection pool that opens every connection through the SSRF guard.

    Each connection — including each redirect hop — is opened through
    :class:`_SSRFGuardBackend`, so the resolved IP is validated at connect
    time.
    """

    def __init__(self, fleet_hosts: set[str], **kwargs: object) -> None:
        """Wire an :class:`_SSRFGuardBackend` for *fleet_hosts* into the pool."""
        super().__init__(
            network_backend=_SSRFGuardBackend(fleet_hosts),
            **kwargs,  # type: ignore[arg-type]
        )


def build_ssrf_guarded_client(
    *,
    timeout: float,
    fleet_hosts: set[str],
    follow_redirects: bool = False,
    max_redirects: int = 20,
) -> httpx.AsyncClient:
    """Return an SSRF-guarded :class:`httpx.AsyncClient`.

    The client validates the resolved IP of every connection against the SSRF
    blocklist at connect time. Because each connection — including every
    redirect hop httpx follows —
    passes through :class:`SSRFGuardPool`, redirect targets are re-validated
    automatically and the validated IP is the connected IP (TOCTOU closed).
    Fleet-component *fleet_hosts* keep their exemption inside the pool.
    """
    transport = httpx.AsyncHTTPTransport()
    # Replace the default pool with the SSRF-guarded one. The discarded pool
    # was never opened, so nothing leaks; ssl_context defaults to httpcore's
    # certifi-backed context.
    transport._pool = SSRFGuardPool(fleet_hosts)
    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
    )


# ---------------------------------------------------------------------------
# Fleet component hosts, resolved from the central-deploy roster
# ---------------------------------------------------------------------------


async def fleet_component_hosts(
    central_deploy: CentralDeploySettings | None,
) -> set[str]:
    """Return the hostnames of chat-accessible fleet components.

    Derived from the central-deploy roster (``GET /chat/components``), whose
    ``base_url`` for each component is an address on the internal container
    network — ``http://mill:8077``, not a public URL. The roster is the fleet's
    single source of truth for which components the agent may reach; the
    per-component chat-access toggle is what populates it.

    These hosts get two exemptions in the fetching tools: they satisfy the host
    allowlist, and they skip the private-address (SSRF) check, because an
    internal address is exactly what a fleet component's URL looks like.

    Reaching them needs no credential. Requests never leave the container
    network, so they never meet the fleet's edge or its SSO gate.

    Returns an empty set when central-deploy is not configured or the roster
    cannot be fetched — the tools then fall back to their own allowlists rather
    than failing, matching the roster module's no-queues/no-retry-loop stance.
    """
    if central_deploy is None:
        return set()
    from urllib.parse import urlsplit

    from robotsix_chat.component_access.roster import fetch_roster

    try:
        entries = await fetch_roster(central_deploy)
    except Exception:
        logger.warning("Could not fetch the component roster", exc_info=True)
        return set()

    hosts: set[str] = set()
    for entry in entries:
        base_url = entry.get("base_url") or ""
        host = urlsplit(base_url).hostname
        if host:
            hosts.add(host)
    return hosts


# ---------------------------------------------------------------------------
# Hostname allowlist check
# ---------------------------------------------------------------------------


def _check_hostname_allowlist(
    hostname: str,
    allowed_hosts: set[str],
    fleet_hosts: set[str],
    tool_name: str,
) -> str | None:
    """Return an error message when *hostname* is not allowed, or ``None``.

    Fleet component hosts are implicitly allowed: the operator granted the
    agent access by enabling chat access on the component, and requiring the
    hostname to be repeated in this tool's allowlist would be a second place to
    configure the same decision.

    An empty *allowed_hosts* set means any hostname is permitted.
    """
    if not allowed_hosts:
        return None
    if hostname in allowed_hosts or hostname in fleet_hosts:
        return None
    all_allowed = sorted(allowed_hosts | fleet_hosts)
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

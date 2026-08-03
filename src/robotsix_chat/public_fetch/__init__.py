"""Scoped public-URL fetch tool for the chat agent.

Performs a plain HTTP(S) GET to a user-provided public URL, returns the
raw text/file contents plus metadata (final URL, HTTP status, Content-Type,
byte length, truncated flag, fetch timestamp), and writes an audit-log entry
per fetch.

Safe by construction: only GET, SSRF protection blocks internal/private IP
ranges, body read is size-capped, configurable domain allowlist, rate
limiting, one request per call, short timeout.  Fleet-auth hosts (configured
via ``fleet_auth.auth_hosts``) carry server-side Basic-Auth headers injected
transparently — the agent never sees the credential.

Exposes :func:`build_public_fetch_tools` — a factory returning the LLM tool.
Returns no tools when disabled.  Also exposes :func:`load_public_fetch_skill`
which returns the component skill markdown.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from robotsix_chat.config import PublicFetchSettings

__all__ = ["build_public_fetch_tools", "load_public_fetch_skill"]

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
# Rate limiter — simple in-process sliding window
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Simple sliding-window rate limiter for a single tool.

    Not thread-safe — the chat server runs async single-threaded, so a
    plain list of timestamps is sufficient.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max: int = max_requests
        self._window: float = window_seconds
        self._timestamps: list[float] = []

    def allow(self, now: float | None = None) -> bool:
        """Return ``True`` when another request is allowed right now."""
        if self._max <= 0:
            return False
        ts = now or time.monotonic()
        cutoff = ts - self._window
        # Prune expired entries
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(ts)
        return True


def load_public_fetch_skill() -> str:
    """Return the public-fetch component skill markdown."""
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_public_fetch_tools(
    settings: PublicFetchSettings,
) -> list[Callable[..., Any]]:
    """Return the ``fetch_public_url`` tool, or an empty list when disabled.

    Args:
        settings: PublicFetch configuration.

    Returns:
        A single-element list containing the ``fetch_public_url`` async
        callable, or ``[]`` when *settings.enabled* is ``False``.

    """
    if not settings.enabled:
        return []

    allowed_hosts: set[str] = set(settings.domain_allowlist)
    rate_limiter = _RateLimiter(
        settings.rate_limit_requests, settings.rate_limit_window_seconds
    )

    # Pre-compute the basic-auth header value when fleet-auth is
    # configured — the agent never sees the credential; it is injected
    # server-side for matching hosts only.
    fleet_auth_header: str | None = None
    fleet_auth_hosts: set[str] = set()
    if settings.fleet_auth is not None:
        username = settings.fleet_auth.basic_auth_username
        password = settings.fleet_auth.basic_auth_password.get_secret_value()
        if username and password:
            import base64 as _base64

            encoded = _base64.b64encode(f"{username}:{password}".encode()).decode(
                "ascii"
            )
            fleet_auth_header = f"Basic {encoded}"
            fleet_auth_hosts = set(settings.fleet_auth.auth_hosts)

    async def fetch_public_url(url: str) -> str:
        """Fetch a public URL and return raw text contents with metadata.

        Performs a single HTTP(S) GET to *url*, following redirects, and
        returns the final HTTP status code, the final URL after any
        redirects, ``Content-Type`` header, response body size (bytes),
        the raw body text (truncated at the configured cap), a truncated
        flag, and a fetch timestamp.

        Safety: only public URLs on the open internet are allowed — SSRF
        protection blocks internal/private IP ranges.  Only GET.  Fleet-
        auth hosts (configured by the operator) carry Basic-Auth headers
        injected server-side — the credential is never exposed to the
        agent.  Every fetch is audited at WARNING log level.

        Args:
            url: The fully-qualified http(s):// URL to fetch.

        Returns:
            A JSON string with ``url``, ``final_url``, ``status_code``,
            ``content_type``, ``body_size_bytes``, ``text`` (the raw body,
            possibly truncated), ``truncated`` (bool), ``fetched_at``
            (ISO-8601 UTC), and ``error`` (empty on success).

        """
        result: dict[str, Any] = {
            "url": url,
            "final_url": url,
            "status_code": None,
            "content_type": None,
            "body_size_bytes": 0,
            "text": "",
            "truncated": False,
            "fetched_at": _utcnow_iso(),
            "error": "",
        }

        # --- URL scheme validation ---
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            result["error"] = (
                f"Unsupported URL scheme {parsed.scheme!r} — "
                "only http and https are allowed."
            )
            _audit(result, "blocked:scheme")
            return json.dumps(result, ensure_ascii=False)

        hostname = parsed.hostname or ""
        if not hostname:
            result["error"] = "URL has no hostname — cannot fetch."
            _audit(result, "blocked:no-hostname")
            return json.dumps(result, ensure_ascii=False)

        # --- Hostname allowlist check ---
        # Fleet-auth hosts are implicitly allowed (the operator
        # explicitly listed them in auth_hosts), so the agent can
        # reach authenticated fleet UIs without duplicating every
        # hostname in the main allowlist.
        if (
            allowed_hosts
            and hostname not in allowed_hosts
            and hostname not in fleet_auth_hosts
        ):
            all_allowed = sorted(allowed_hosts | fleet_auth_hosts)
            result["error"] = (
                f"Hostname {hostname!r} is not in the public_fetch domain "
                f"allowlist. Allowed hosts: {all_allowed}"
            )
            _audit(result, "blocked:allowlist")
            return json.dumps(result, ensure_ascii=False)

        # --- SSRF check (initial hostname) ---
        # Fleet-auth hosts are trusted by the operator — skip SSRF check.
        if hostname not in fleet_auth_hosts and _host_is_private(hostname):
            result["error"] = (
                f"Hostname {hostname!r} resolves to a private/internal IP "
                "address — SSRF protection blocked the request."
            )
            _audit(result, "blocked:ssrf")
            return json.dumps(result, ensure_ascii=False)

        # --- Rate limiting ---
        if not rate_limiter.allow():
            result["error"] = (
                f"Rate limit exceeded — "
                f"{settings.rate_limit_requests} requests per "
                f"{settings.rate_limit_window_seconds:.0f}s window."
            )
            _audit(result, "blocked:rate-limit")
            return json.dumps(result, ensure_ascii=False)

        # --- HTTP GET with manual redirect following (SSRF check on each hop) ---
        # Uses httpx streaming so the body cap is enforced at the network-
        # read level — a malicious server cannot exhaust memory by sending
        # gigabytes before the cap is applied.
        try:
            async with httpx.AsyncClient(
                timeout=settings.timeout,
                follow_redirects=False,
            ) as client:
                current_url = url
                redirects_followed = 0

                while True:
                    parsed_current = urlparse(current_url)
                    current_host = parsed_current.hostname or ""

                    # SSRF check on every hop (redirect targets included).
                    # Fleet-auth hosts are trusted by the operator — skip.
                    if current_host not in fleet_auth_hosts and _host_is_private(
                        current_host
                    ):
                        result["error"] = (
                            f"Hostname {current_host!r} resolves to a "
                            "private/internal IP address — SSRF protection "
                            "blocked the request."
                        )
                        result["final_url"] = current_url
                        _audit(result, "blocked:ssrf-redirect")
                        return json.dumps(result, ensure_ascii=False)

                    # Build request headers — inject fleet-auth when
                    # the target host is in the fleet_auth_hosts set.
                    request_headers: dict[str, str] = {}
                    if (
                        current_host in fleet_auth_hosts
                        and fleet_auth_header is not None
                    ):
                        request_headers["Authorization"] = fleet_auth_header

                    async with client.stream(
                        "GET", current_url, headers=request_headers
                    ) as response:
                        result["final_url"] = str(response.url)

                        # Follow redirect?
                        if response.status_code in (301, 302, 303, 307, 308):
                            redirects_followed += 1
                            if redirects_followed > settings.max_redirects:
                                result["error"] = (
                                    f"Too many redirects "
                                    f"(max {settings.max_redirects}) for {url}"
                                )
                                _audit(result, "error:too-many-redirects")
                                return json.dumps(result, ensure_ascii=False)
                            next_url = response.headers.get("Location", "")
                            if not next_url:
                                result["error"] = (
                                    f"Redirect ({response.status_code}) "
                                    "with no Location header"
                                )
                                _audit(result, "error:redirect-no-location")
                                return json.dumps(result, ensure_ascii=False)
                            # Drain the redirect body to free the connection
                            async for _ in response.aiter_bytes(8192):
                                pass
                            current_url = str(
                                httpx.URL(current_url).join(httpx.URL(next_url))
                            )
                            continue

                        # Final response — stream body with cap
                        result["status_code"] = response.status_code
                        result["content_type"] = response.headers.get(
                            "content-type", ""
                        )

                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes(65536):
                            total += len(chunk)
                            if total <= settings.max_body_bytes:
                                chunks.append(chunk)
                            else:
                                # Partial chunk fills the remainder, then stop
                                already = sum(len(c) for c in chunks)
                                remaining = settings.max_body_bytes - already
                                if remaining > 0:
                                    chunks.append(chunk[:remaining])
                                result["truncated"] = True
                                break  # enforce cap at network-read level

                        body_bytes = b"".join(chunks)
                        result["body_size_bytes"] = (
                            total if not result["truncated"] else len(body_bytes)
                        )
                        result["text"] = body_bytes.decode("utf-8", errors="replace")

                        # Handle error status codes
                        if response.status_code >= 400:
                            if response.status_code in (401, 403):
                                if current_host in fleet_auth_hosts:
                                    result["error"] = (
                                        f"Server returned "
                                        f"{response.status_code} — fleet-auth "
                                        "credentials may need updating. Check "
                                        "the fleet_auth configuration."
                                    )
                                else:
                                    result["error"] = (
                                        f"Server returned "
                                        f"{response.status_code} — URL requires "
                                        "authentication. Only public, "
                                        "unauthenticated URLs are supported."
                                    )
                            else:
                                result["error"] = (
                                    f"HTTP {response.status_code}: "
                                    f"{response.reason_phrase}"
                                )
                            _audit(
                                result,
                                f"error:http-{response.status_code}",
                            )
                            return json.dumps(result, ensure_ascii=False)

                        # Success
                        _audit(result, "success")
                        return json.dumps(result, ensure_ascii=False)

        except httpx.TimeoutException:
            result["error"] = f"Request timed out after {settings.timeout}s: {url}"
            _audit(result, "error:timeout")
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.exception("fetch_public_url failed for %s", url)
            result["error"] = f"{type(exc).__name__}: {exc}"
            _audit(result, "error:exception")
            return json.dumps(result, ensure_ascii=False)

    return [fetch_public_url]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Return current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _audit(result: dict[str, Any], disposition: str) -> None:
    """Write an audit-log entry at WARNING level.

    The log line carries the URL, final URL, status code, body size, a
    SHA-256 hash of the response text (empty on error), and the disposition
    tag so operators can trace every fetch.
    """
    text = result.get("text") or ""
    sha = (
        hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        if text
        else ""
    )
    logger.warning(
        "public_fetch: disposition=%s url=%s final_url=%s status=%s size=%s sha256=%s",
        disposition,
        result.get("url", ""),
        result.get("final_url", ""),
        result.get("status_code"),
        result.get("body_size_bytes", 0),
        sha,
    )

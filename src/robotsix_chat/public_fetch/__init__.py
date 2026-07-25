"""Scoped public-URL fetch tool for the chat agent.

Performs a plain HTTP(S) GET to a user-provided public URL, returns the
raw text/file contents plus metadata (final URL, HTTP status, Content-Type,
byte length, truncated flag, fetch timestamp), and writes an audit-log entry
per fetch.

Safe by construction: only GET, SSRF protection blocks internal/private IP
ranges, body read is size-capped, configurable domain allowlist, rate
limiting, one request per call, short timeout.  Only public, unauthenticated
URLs are allowed — 401/403 responses are reported clearly.

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
    ipaddress.IPv4Network("127.0.0.0/8"),  # loopback
    ipaddress.IPv4Network("10.0.0.0/8"),  # private
    ipaddress.IPv4Network("172.16.0.0/12"),  # private
    ipaddress.IPv4Network("192.168.0.0/16"),  # private
    ipaddress.IPv4Network("169.254.0.0/16"),  # link-local
    ipaddress.IPv6Network("::1/128"),  # loopback
    ipaddress.IPv6Network("fc00::/7"),  # unique local
    ipaddress.IPv6Network("fe80::/10"),  # link-local
)


def _host_is_private(host: str) -> bool:
    """Return ``True`` when *host* resolves to any private/internal IP.

    A host that cannot be resolved at all is treated as unsafe (returns
    ``True``) so the tool rejects it rather than making a blind request.
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

    async def fetch_public_url(url: str) -> str:
        """Fetch a public URL and return raw text contents with metadata.

        Performs a single HTTP(S) GET to *url*, following redirects, and
        returns the final HTTP status code, the final URL after any
        redirects, ``Content-Type`` header, response body size (bytes),
        the raw body text (truncated at the configured cap), a truncated
        flag, and a fetch timestamp.

        Safety: only public URLs on the open internet are allowed — SSRF
        protection blocks internal/private IP ranges.  Only GET, no auth
        headers.  401/403 responses are reported clearly.  Every fetch is
        audited at WARNING log level.

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
        if allowed_hosts and hostname not in allowed_hosts:
            result["error"] = (
                f"Hostname {hostname!r} is not in the public_fetch domain "
                f"allowlist. Allowed hosts: {sorted(allowed_hosts)}"
            )
            _audit(result, "blocked:allowlist")
            return json.dumps(result, ensure_ascii=False)

        # --- SSRF check (initial hostname) ---
        if _host_is_private(hostname):
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

                    # SSRF check on every hop (redirect targets included)
                    if _host_is_private(current_host):
                        result["error"] = (
                            f"Hostname {current_host!r} resolves to a "
                            "private/internal IP address — SSRF protection "
                            "blocked the request."
                        )
                        result["final_url"] = current_url
                        _audit(result, "blocked:ssrf-redirect")
                        return json.dumps(result, ensure_ascii=False)

                    response = await client.get(current_url)
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
                                f"Redirect ({response.status_code}) with no "
                                "Location header"
                            )
                            _audit(result, "error:redirect-no-location")
                            return json.dumps(result, ensure_ascii=False)
                        # Resolve relative redirects
                        current_url = str(
                            httpx.URL(current_url).join(httpx.URL(next_url))
                        )
                        continue

                    # Not a redirect — process the final response
                    result["status_code"] = response.status_code
                    result["content_type"] = response.headers.get("content-type", "")

                    full_body = response.text
                    body_size = len(full_body)
                    result["body_size_bytes"] = body_size
                    if body_size > settings.max_body_bytes:
                        result["text"] = full_body[: settings.max_body_bytes]
                        result["truncated"] = True
                    else:
                        result["text"] = full_body

                    response.raise_for_status()
                    break  # success — exit the redirect loop

        except httpx.TimeoutException:
            result["error"] = f"Request timed out after {settings.timeout}s: {url}"
            _audit(result, "error:timeout")
            return json.dumps(result, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            result["status_code"] = exc.response.status_code
            result["final_url"] = str(exc.response.url)
            result["content_type"] = exc.response.headers.get("content-type", "")
            if exc.response.status_code in (401, 403):
                result["error"] = (
                    f"Server returned {exc.response.status_code} — "
                    "URL requires authentication. Only public, "
                    "unauthenticated URLs are supported."
                )
            else:
                result["error"] = (
                    f"HTTP {exc.response.status_code}: {exc.response.reason_phrase}"
                )
            try:
                body = exc.response.text
                body_size = len(body)
                result["body_size_bytes"] = body_size
                if body_size > settings.max_body_bytes:
                    result["text"] = body[: settings.max_body_bytes]
                    result["truncated"] = True
                else:
                    result["text"] = body
            except Exception:
                logger.debug("Could not read error response body", exc_info=True)
            _audit(result, f"error:http-{exc.response.status_code}")
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.exception("fetch_public_url failed for %s", url)
            result["error"] = f"{type(exc).__name__}: {exc}"
            _audit(result, "error:exception")
            return json.dumps(result, ensure_ascii=False)

        # --- Success ---
        _audit(result, "success")
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

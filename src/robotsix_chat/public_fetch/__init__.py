"""Public URL fetch tool — read raw content from any public forge.

Fetches raw content from arbitrary public URLs with SSRF protection,
size limits, and no authentication. Designed for reading files from
public forges (GitLab, Bitbucket, codeberg, university GitLabs, etc.)
that are not covered by the GitHub-scoped repo-study tools.

Safe by construction: DNS-level SSRF check on every redirect hop,
scheme restricted to http/https, bare IP addresses rejected, response
body size-capped, short timeout.

Exposes :func:`build_public_fetch_tools` — a factory returning the LLM tool.
Returns no tools when disabled, so the chat runs exactly as before.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

import httpx

if TYPE_CHECKING:
    from robotsix_chat.config import PublicFetchSettings

__all__ = ["build_public_fetch_tools"]

logger = logging.getLogger(__name__)


def _check_host_public(hostname: str) -> str:
    """Resolve *hostname* and verify all resolved IPs are public.

    Returns an empty string on success, or an error message describing
    what was blocked and why.
    """
    # Reject bare IPv6 addresses in brackets.
    if hostname.startswith("[") and hostname.endswith("]"):
        return f"Bare IP addresses are not allowed: {hostname!r}"

    # Reject bare IPv4 / IPv6 addresses.
    try:
        ipaddress.ip_address(hostname)
        return f"Bare IP addresses are not allowed: {hostname!r}"
    except ValueError:
        pass  # Not a bare IP — a hostname; proceed to DNS check.

    try:
        addrs = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        return f"DNS resolution failed for {hostname!r}: {exc}"

    for addr in addrs:
        ip_str = addr[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return f"Invalid IP address {ip_str!r} resolved for {hostname!r}"
        if not ip.is_global:
            return f"Hostname {hostname!r} resolves to non-public IP {ip_str!r}"
    return ""


def _validate_url(url: str) -> str:
    """Validate *url* for scheme, hostname, and SSRF safety.

    Returns an empty string on success, or an error message.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if parsed.scheme not in ("http", "https"):
        return (
            f"Unsupported URL scheme {parsed.scheme!r} — "
            "only http and https are allowed."
        )

    if not hostname:
        return f"URL {url!r} has no hostname."

    return _check_host_public(hostname)


def build_public_fetch_tools(
    settings: PublicFetchSettings,
) -> list[Callable[..., Any]]:
    """Return the ``fetch_public_url`` tool, or ``[]`` when disabled.

    Args:
        settings: PublicFetch configuration (``enabled`` master switch,
            timeout, body cap, max redirects).

    Returns:
        A single-element list containing the ``fetch_public_url`` async
        callable, or ``[]`` when *settings.enabled* is ``False``.

    """
    if not settings.enabled:
        return []

    async def fetch_public_url(url: str) -> str:
        """Fetch raw content from a public URL.

        Downloads the content at *url* and returns the body text.
        Only public-internet hosts are reachable — private, loopback,
        link-local, and multicast addresses are blocked to prevent SSRF.
        The response body is capped at the configured size limit
        (default 1 MB).

        Use this to read a raw file, document, or repository listing
        from any public forge (GitLab, Bitbucket, codeberg, university
        GitLabs, etc.) — anything not covered by the GitHub-scoped
        fetch_repo_for_study.

        Args:
            url: The fully-qualified http:// or https:// URL to fetch.

        Returns:
            The body text (up to the size limit), prefixed by a status
            summary line, or an error message.

        """
        # --- Initial URL validation ---
        err = _validate_url(url)
        if err:
            return f"SSRF check failed: {err}"

        try:
            async with httpx.AsyncClient(
                timeout=settings.timeout,
                follow_redirects=False,
            ) as client:
                current_url = url
                redirect_count = 0

                while redirect_count <= settings.max_redirects:
                    response = await client.get(current_url)

                    # Follow redirects manually, checking each target.
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location", "")
                        if not location:
                            return (
                                f"Redirect ({response.status_code}) "
                                f"without a Location header for {current_url}."
                            )
                        # Resolve relative redirect targets.
                        next_url = urljoin(current_url, location)
                        err = _validate_url(next_url)
                        if err:
                            return f"Redirect target rejected for {current_url}: {err}"
                        redirect_count += 1
                        current_url = next_url
                        continue

                    # Not a redirect — process the final response.
                    response.raise_for_status()

                    content_type = response.headers.get("content-type", "")
                    raw_body = response.text[: settings.max_body_bytes]
                    body_size = len(response.text)

                    summary = (
                        f"Fetched {response.url}\n"
                        f"Status: {response.status_code}\n"
                        f"Content-Type: {content_type}\n"
                        f"Body size: {body_size} bytes"
                    )
                    if body_size > settings.max_body_bytes:
                        summary += f" (truncated to {settings.max_body_bytes} bytes)"
                    return f"{summary}\n\n{raw_body}"

                return f"Too many redirects (max {settings.max_redirects}) for {url}"

        except httpx.TimeoutException:
            return f"Request timed out after {settings.timeout}s: {url}"
        except httpx.HTTPStatusError as exc:
            return (
                f"HTTP {exc.response.status_code} for {exc.response.url}\n"
                f"{exc.response.text[: settings.max_body_bytes]}"
            )
        except Exception as exc:
            logger.exception("fetch_public_url failed for %s", url)
            return f"{type(exc).__name__}: {exc}"

    return [fetch_public_url]

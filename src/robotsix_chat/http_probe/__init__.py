"""Read-only HTTP uptime/render-probe tool for the agent.

Performs a plain HTTPS GET to a public URL, follows redirects, and returns
the HTTP status, final URL, response time, Content-Type, response size, and
a snippet of the body text.  The caller may also supply optional assertions
(``expect_status``, ``expect_contains``, ``expect_absent``) and the tool
returns a ``healthy`` boolean with which checks failed.

Safe by construction: only GET, public-internet URLs gated by a configurable
hostname allowlist, body read is size-capped, one request per call, short
timeout.  Internal-fleet hosts are unreachable unless explicitly allowlisted
by the operator.

Exposes :func:`build_http_probe_tools` — a factory returning the LLM tool.
Returns no tools when disabled, so the chat runs exactly as before.  Also
exposes :func:`load_http_probe_skill` which returns the component skill
markdown for injection into the agent instruction.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from robotsix_chat.config import HttpProbeSettings

__all__ = ["build_http_probe_tools", "load_http_probe_skill"]

logger = logging.getLogger(__name__)


def load_http_probe_skill() -> str:
    """Return the HTTP-probe component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.

    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_http_probe_tools(
    settings: HttpProbeSettings,
) -> list[Callable[..., Any]]:
    """Return the ``http_probe`` tool, or an empty list when disabled.

    Args:
        settings: HttpProbe configuration (``enabled`` master switch,
            timeout, allowlist, body-cap, max redirects).

    Returns:
        A single-element list containing the ``http_probe`` async callable,
        or ``[]`` when *settings.enabled* is ``False``.

    """
    if not settings.enabled:
        return []

    allowed_hosts: set[str] = set(settings.allowlist)

    async def http_probe(
        url: str,
        expect_status: int = 200,
        expect_contains: list[str] | None = None,
        expect_absent: list[str] | None = None,
    ) -> str:
        """Probe a public URL with an HTTPS GET and return health + body snippet.

        Performs a single HTTPS GET to *url*, following redirects, and
        returns the final HTTP status code, the final URL after any
        redirects, response time (ms), ``Content-Type`` header, response
        body size (bytes), and the first ~2 KB of the body text.

        When assertion parameters are provided, the tool also evaluates:
        - Status matches *expect_status* (default 200)
        - Body contains every substring in *expect_contains*
        - Body contains none of the substrings in *expect_absent*

        A ``healthy`` boolean is returned — ``False`` when any assertion
        fails, along with a list of which checks failed.

        Args:
            url: The fully-qualified https:// URL to probe.
            expect_status: Expected HTTP status code (default 200).
            expect_contains: Substrings that must appear in the response
                body.  Default ``[]`` (no content check).
            expect_absent: Substrings that must NOT appear in the response
                body (e.g. ``"Index of /"``, ``"Not Found"``).  Default
                ``[]`` (no negative check).

        Returns:
            A JSON string with ``url``, ``final_url``, ``status_code``,
            ``response_time_ms``, ``content_type``, ``body_size_bytes``,
            ``body_snippet`` (first ~2 KB of text), ``healthy`` (bool),
            and ``checks`` (list of dicts: ``"check"`` name, ``"passed"``
            bool, ``"detail"`` string).

        """
        if expect_contains is None:
            expect_contains = []
        if expect_absent is None:
            expect_absent = []

        result: dict[str, Any] = {
            "url": url,
            "final_url": url,
            "status_code": None,
            "response_time_ms": None,
            "content_type": None,
            "body_size_bytes": 0,
            "body_snippet": "",
            "healthy": True,
            "checks": [],
            "error": "",
        }

        # --- Hostname allowlist check ---
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if allowed_hosts and hostname not in allowed_hosts:
            result["error"] = (
                f"Hostname {hostname!r} is not in the http_probe allowlist. "
                f"Allowed hosts: {sorted(allowed_hosts)}"
            )
            result["healthy"] = False
            return json.dumps(result, ensure_ascii=False)

        if parsed.scheme not in ("http", "https"):
            result["error"] = (
                f"Unsupported URL scheme {parsed.scheme!r} — "
                "only http and https are allowed."
            )
            result["healthy"] = False
            return json.dumps(result, ensure_ascii=False)

        # --- HTTP GET ---
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=settings.timeout,
                follow_redirects=True,
                max_redirects=settings.max_redirects,
            ) as client:
                response = await client.get(url)
                elapsed = (time.monotonic() - start) * 1000.0  # ms

                result["final_url"] = str(response.url)
                result["status_code"] = response.status_code
                result["response_time_ms"] = round(elapsed, 1)
                result["content_type"] = response.headers.get("content-type", "")

                # Read body up to the cap.
                raw_body = response.text[: settings.max_body_bytes]
                body_size = len(response.text)
                result["body_size_bytes"] = body_size
                result["body_snippet"] = raw_body

                response.raise_for_status()
        except httpx.TooManyRedirects:
            elapsed = (time.monotonic() - start) * 1000.0
            result["response_time_ms"] = round(elapsed, 1)
            result["error"] = (
                f"Too many redirects (max {settings.max_redirects}) for {url}"
            )
            result["healthy"] = False
            return json.dumps(result, ensure_ascii=False)
        except httpx.TimeoutException:
            elapsed = (time.monotonic() - start) * 1000.0
            result["response_time_ms"] = round(elapsed, 1)
            result["error"] = f"Request timed out after {settings.timeout}s: {url}"
            result["healthy"] = False
            return json.dumps(result, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            # We still have status/body info — don't bail, proceed to checks.
            result["status_code"] = exc.response.status_code
            result["final_url"] = str(exc.response.url)
            elapsed = (time.monotonic() - start) * 1000.0
            result["response_time_ms"] = round(elapsed, 1)
            result["content_type"] = exc.response.headers.get("content-type", "")
            try:
                raw_body = exc.response.text[: settings.max_body_bytes]
                body_size = len(exc.response.text)
            except Exception:
                raw_body = ""
                body_size = 0
            result["body_size_bytes"] = body_size
            result["body_snippet"] = raw_body
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000.0
            result["response_time_ms"] = round(elapsed, 1)
            logger.exception("http_probe failed for %s", url)
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["healthy"] = False
            return json.dumps(result, ensure_ascii=False)

        # --- Assertion checks ---
        body_lower = result["body_snippet"].lower()

        # Status check
        status_pass = result["status_code"] == expect_status
        result["checks"].append(
            {
                "check": "status",
                "passed": status_pass,
                "detail": (
                    f"expected {expect_status}, got {result['status_code']}"
                    if not status_pass
                    else f"status {result['status_code']} matches expected"
                ),
            }
        )

        # expect_contains checks
        for substring in expect_contains:
            present = substring.lower() in body_lower
            result["checks"].append(
                {
                    "check": f"expect_contains({substring!r})",
                    "passed": present,
                    "detail": (
                        f"substring {substring!r} not found in body"
                        if not present
                        else f"substring {substring!r} found"
                    ),
                }
            )

        # expect_absent checks
        for substring in expect_absent:
            absent = substring.lower() not in body_lower
            result["checks"].append(
                {
                    "check": f"expect_absent({substring!r})",
                    "passed": absent,
                    "detail": (
                        f"substring {substring!r} found in body"
                        if not absent
                        else f"substring {substring!r} not found"
                    ),
                }
            )

        # Overall healthy: all checks passed AND no error
        result["healthy"] = not result["error"] and all(
            c["passed"] for c in result["checks"]
        )

        return json.dumps(result, ensure_ascii=False)

    return [http_probe]

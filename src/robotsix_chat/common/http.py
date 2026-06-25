"""Safe HTTP request helper — one error-handling path for all clients.

All three HTTP client modules (board_reader, refdocs, version_check)
previously duplicated the same 3-way ``except (HTTPStatusError,
TimeoutException, Exception)`` cascade.  This module consolidates it into
:func:`safe_http_request`, which returns an :class:`HttpResult` so callers
never have to write their own error-formatting boilerplate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class HttpResult:
    """Result of a safe HTTP request that never raises to the caller.

    On success, ``text`` holds the response body and ``error`` is ``None``.
    On failure, ``error`` is a human-readable diagnostic and ``text`` is
    ``None``.  ``status_code`` is populated for both successes and
    HTTP-level errors.
    """

    text: str | None = None
    """Response body text on success, ``None`` on failure."""

    status_code: int | None = None
    """HTTP status code (set for successes and HTTPStatusError failures)."""

    error: str | None = None
    """Human-readable diagnostic on failure, ``None`` on success."""

    @property
    def ok(self) -> bool:
        """``True`` when the request succeeded (no error)."""
        return self.error is None


async def safe_http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    label: str = "HTTP",
) -> HttpResult:
    """Make an HTTP request, catching all errors — never raises.

    The three-way ``except (HTTPStatusError, TimeoutException, Exception)``
    cascade that previously appeared verbatim in five methods across three
    client modules is consolidated here so error formatting is consistent.

    Args:
        method: HTTP method (``"GET"`` or ``"POST"``).
        url: Full URL to request.
        headers: Optional request headers.
        timeout: Seconds before the request times out.
        json_body: JSON-serialisable body for POST requests.
        params: URL query parameters for GET requests.
        label: Human-readable prefix for log / error messages (e.g.
            ``"Board API"``, ``"RefDocs"``, ``"GitHub API"``).

    Returns:
        :class:`HttpResult` — inspect ``.error`` to decide success/failure.

    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method.upper() == "POST":
                response = await client.post(url, headers=headers, json=json_body)
            else:
                kwargs: dict[str, Any] = {"headers": headers}
                if params is not None:
                    kwargs["params"] = params
                response = await client.get(url, **kwargs)
            response.raise_for_status()
            # Defensive: mocked responses in tests may lack ``.text``.
            try:
                body_text = response.text
            except Exception:
                body_text = ""
            return HttpResult(text=body_text, status_code=response.status_code)
    except httpx.HTTPStatusError as exc:
        # Defensive: mocked responses in tests may lack ``.text``.
        try:
            raw = exc.response.text
        except Exception:
            raw = ""
        body = raw[:500] if raw else "(empty body)"
        status = exc.response.status_code
        logger.warning("%s returned %d for %s", label, status, url)
        return HttpResult(
            status_code=status,
            error=f"{label} error {status} for {method.upper()} {url}: {body}",
        )
    except httpx.TimeoutException:
        logger.warning("%s timed out for %s", label, url)
        return HttpResult(
            error=f"{label} request timed out after {timeout}s: {url}",
        )
    except Exception as exc:  # noqa: BLE001 — surface as text, never crash
        logger.warning("%s request failed for %s: %s", label, url, exc)
        return HttpResult(error=f"{label} request failed: {exc}")

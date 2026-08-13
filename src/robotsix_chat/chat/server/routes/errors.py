"""Error handlers — consistent JSON error responses.

All handlers emit the same envelope shape so API clients never have to
branch on which key appeared.
"""

from __future__ import annotations

import logging

import httpx
from asgi_correlation_id import correlation_id
from robotsix_llmio.claude_sdk import is_claude_sdk_usage_exhausted
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

#: Stable, machine-readable codes for the mid-stream SSE error frame. Clients
#: branch on these (retry vs. abort); the wording below may change freely, the
#: codes may not.
STREAM_ERROR_SERVER = "server_error"
STREAM_ERROR_TIMEOUT = "timeout"
STREAM_ERROR_RATE_LIMIT = "rate_limit_exceeded"
STREAM_ERROR_AUTH = "authentication_error"
STREAM_ERROR_INVALID_REQUEST = "invalid_request_error"
STREAM_ERROR_BUDGET_EXHAUSTED = "budget_exhausted"

#: Curated, client-safe wording per code. Deliberately free of exception
#: detail: ``str(exc)`` routinely embeds filesystem paths, upstream URLs and
#: provider error bodies, and the SSE error frame is broadcast to *every*
#: client watching the session.
_STREAM_ERROR_MESSAGES: dict[str, str] = {
    STREAM_ERROR_SERVER: (
        "The assistant hit an internal error and couldn't complete the response."
    ),
    STREAM_ERROR_TIMEOUT: ("The assistant took too long to respond. Please try again."),
    STREAM_ERROR_RATE_LIMIT: (
        "The assistant is rate limited right now. Please retry in a moment."
    ),
    STREAM_ERROR_AUTH: (
        "The assistant could not authenticate with its model provider."
    ),
    STREAM_ERROR_INVALID_REQUEST: ("The assistant could not process that request."),
    STREAM_ERROR_BUDGET_EXHAUSTED: (
        "The assistant's model budget is exhausted. Resume with a new message "
        "to continue, or switch to a different model level."
    ),
}


def stream_error_code(exc: BaseException) -> str:
    """Map ``exc`` to a stable, client-safe error code.

    Categories are derived from transport-level facts (timeout, HTTP status on
    an attached response) plus one semantic classifier — a Claude SDK tier
    reporting exhausted usage credits maps to ``budget_exhausted``. Anything
    unrecognised degrades to ``server_error`` rather than guessing.
    """
    if is_claude_sdk_usage_exhausted(exc):
        return STREAM_ERROR_BUDGET_EXHAUSTED
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return STREAM_ERROR_TIMEOUT
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status == 429:
            return STREAM_ERROR_RATE_LIMIT
        if status in (401, 403):
            return STREAM_ERROR_AUTH
        if 400 <= status < 500:
            return STREAM_ERROR_INVALID_REQUEST
    return STREAM_ERROR_SERVER


def curated_stream_error(
    exc: BaseException, *, fallback_id: str = ""
) -> dict[str, str]:
    """Build the client-facing payload for a mid-stream failure.

    Returns a stable ``code``, curated ``message``, and the request's
    ``correlation_id`` so a user-reported error can be grepped straight to the
    server-side ``logger.exception`` line. Falls back to ``fallback_id`` (the
    turn id) when no correlation id is in context — the coalescer can outlive
    the request that spawned it.
    """
    code = stream_error_code(exc)
    return {
        "code": code,
        "message": _STREAM_ERROR_MESSAGES[code],
        "correlation_id": correlation_id.get() or fallback_id,
    }


def _error_body(detail: str) -> dict[str, str]:
    """Build the standardised error envelope."""
    return {
        "error": detail or "internal server error",
        "correlation_id": correlation_id.get() or "",
    }


async def http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for ``HTTPException`` instead of plain text."""
    if isinstance(exc, HTTPException):
        return JSONResponse(_error_body(str(exc.detail)), status_code=exc.status_code)
    return JSONResponse(_error_body(str(exc)), status_code=500)


async def not_found_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for unmatched routes instead of plain text.

    When an endpoint explicitly raises ``HTTPException(404)`` with a
    custom detail (e.g.  "unknown subsession 'xyz'"), forward that
    detail.  Starlette dispatches by status code before exception
    class, so this handler sees both genuine unmatched-route 404s and
    explicit raises.
    """
    if isinstance(exc, HTTPException) and str(exc.detail) != "Not Found":
        return JSONResponse(_error_body(str(exc.detail)), status_code=404)
    return JSONResponse(_error_body("not found"), status_code=404)


async def server_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
    """Return JSON for unhandled server errors.

    Logs the full traceback so operators can diagnose the root cause.
    """
    logger.exception("Unhandled server error")
    return JSONResponse(_error_body("internal server error"), status_code=500)


async def unhandled_exception_handler(
    _request: Request, _exc: Exception
) -> JSONResponse:
    """Catch-all handler for any exception not caught by more specific handlers.

    Logs the full traceback and returns a generic 500 so no raw
    traceback leaks to the client.
    """
    logger.exception("Unhandled exception")
    return JSONResponse(_error_body("internal server error"), status_code=500)

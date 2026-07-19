"""Error handlers — consistent JSON error responses.

All handlers emit the same envelope shape so API clients never have to
branch on which key appeared.
"""

from __future__ import annotations

import logging

from asgi_correlation_id import correlation_id
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


def _error_body(detail: str) -> dict[str, str]:
    """Build the standardised error envelope."""
    return {"error": detail, "correlation_id": correlation_id.get() or ""}


async def http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for ``HTTPException`` instead of plain text."""
    if isinstance(exc, HTTPException):
        return JSONResponse(_error_body(str(exc.detail)), status_code=exc.status_code)
    return JSONResponse(_error_body(str(exc)), status_code=500)


async def not_found_handler(_request: Request, _exc: Exception) -> JSONResponse:
    """Return JSON for unmatched routes instead of plain text."""
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

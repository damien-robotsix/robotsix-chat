"""Error handlers — JSON-formatted 404, 400, and 500 responses."""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse


async def http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for ``HTTPException`` instead of plain text."""
    if isinstance(exc, HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return JSONResponse({"detail": str(exc)}, status_code=500)


async def not_found_handler(_request: Request, _exc: Exception) -> JSONResponse:
    """Return JSON for unmatched routes instead of plain text."""
    return JSONResponse({"error": "not found"}, status_code=404)


async def server_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
    """Return JSON for unhandled server errors."""
    return JSONResponse({"error": "internal server error"}, status_code=500)

"""Structlog context middleware for the chat server.

Binds the request correlation id into structlog's contextvars so every log
line emitted while handling a request carries it.
"""

from __future__ import annotations

from typing import Any

from asgi_correlation_id.context import correlation_id
from structlog.contextvars import bind_contextvars, clear_contextvars

# ---------------------------------------------------------------------------
# Structlog context middleware — binds correlation_id into structlog's
# contextvars so merge_contextvars picks it up for every log line.
# ---------------------------------------------------------------------------


class StructlogContextMiddleware:
    """ASGI middleware that binds correlation_id into structlog contextvars.

    Must be placed after ``CorrelationIdMiddleware`` in the middleware
    stack so the correlation ID is already populated when this middleware
    runs.
    """

    def __init__(self, app: Any) -> None:
        """Store the downstream ASGI application."""
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Clear contextvars, bind the current correlation_id, then delegate."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        clear_contextvars()
        bind_contextvars(correlation_id=correlation_id.get())
        await self.app(scope, receive, send)

"""Minimal stub for asgi-correlation-id's CorrelationIdMiddleware.

Used when the package is not installed to avoid a hard import dependency.
"""


class CorrelationIdMiddleware:
    """Stub middleware that passes through to the inner ASGI app."""

    def __init__(self, app, *args, **kwargs):
        """Initialize with the inner ASGI application."""
        self.app = app

    async def __call__(self, scope, receive, send):
        """Pass the request through to the inner application unchanged."""
        await self.app(scope, receive, send)

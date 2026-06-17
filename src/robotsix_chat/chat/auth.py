"""HTTP Basic Auth for the chat server.

A small, pure-ASGI middleware that gates every request behind HTTP Basic
credentials, except the paths in *exclude_paths* (``/health`` by default,
so liveness probes stay open). Pure ASGI — rather than
``BaseHTTPMiddleware`` — so it never buffers the SSE response body.

Browsers handle Basic auth natively: the first 401 with a
``WWW-Authenticate`` header triggers the login dialog, and the browser
then attaches the cached credentials to same-origin ``fetch`` calls
(including ``POST /chat``), so the bundled UI needs no changes.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from dataclasses import dataclass

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class BasicAuthConfig:
    """Credentials accepted by :class:`BasicAuthMiddleware`."""

    username: str
    password: str


class BasicAuthMiddleware:
    """Reject requests lacking valid HTTP Basic credentials.

    Args:
        app: The wrapped ASGI application.
        config: The single accepted username/password pair.
        exclude_paths: Paths served without authentication (default
            ``("/health",)`` so liveness probes are not gated).
        realm: The ``WWW-Authenticate`` realm shown in the browser dialog.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        config: BasicAuthConfig,
        exclude_paths: tuple[str, ...] = ("/health",),
        realm: str = "robotsix-chat",
    ) -> None:
        self.app = app
        self.config = config
        self.exclude_paths = frozenset(exclude_paths)
        self.realm = realm

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exclude_paths:
            await self.app(scope, receive, send)
            return

        if self._authorized(scope):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{self.realm}"'},
        )
        await response(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        """Return whether the request carries valid Basic credentials."""
        header: bytes | None = None
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                header = value
                break
        if header is None:
            return False

        try:
            scheme, _, param = header.decode("latin-1").partition(" ")
            if scheme.lower() != "basic" or not param:
                return False
            decoded = base64.b64decode(param, validate=True).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return False

        username, sep, password = decoded.partition(":")
        if not sep:
            return False

        # ``compare_digest`` for both fields to avoid leaking which one
        # was wrong via timing. Both must be evaluated (no short-circuit).
        user_ok = secrets.compare_digest(username, self.config.username)
        password_ok = secrets.compare_digest(password, self.config.password)
        return user_ok and password_ok

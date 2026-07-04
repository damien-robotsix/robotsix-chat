"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure locally-installed packages (asgi-correlation-id etc.) are
# importable before any application code is loaded.
_local_pkgs = Path(__file__).resolve().parent.parent / "local-deps"
if str(_local_pkgs) not in sys.path and _local_pkgs.is_dir():
    sys.path.insert(0, str(_local_pkgs))

from httpx import ASGITransport, AsyncClient  # noqa: E402
from starlette.applications import Starlette  # noqa: E402

from robotsix_chat.chat.server import create_app  # noqa: E402


class MockAgent:
    """A :class:`ChatAgent` that yields a fixed list of tokens."""

    def __init__(
        self,
        tokens: list[str] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        """Initialise with a fixed token list and optional error."""
        self.tokens = tokens or ["Hello", " ", "world!"]
        self.error = error
        self.call_count = 0
        self.called_with: str | None = None
        # Capture the conversation context the server passes, for assertions.
        self.history: list[tuple[str, str]] | None = None
        self.session_id: str | None = None
        self.client_id: str | None = None

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
    ) -> AsyncIterator[str]:
        """Yield tokens or raise the configured error."""
        self.call_count += 1
        self.called_with = message
        self.history = history
        self.session_id = session_id
        self.client_id = client_id
        self.images = images
        if self.error is not None:
            raise self.error
        for token in self.tokens:
            yield token


@dataclass
class AppFixture:
    """Holder for agent, app, and client yielded by :func:`mock_app`."""

    agent: MockAgent
    app: Starlette
    client: AsyncClient


@asynccontextmanager
async def http_client(
    app: Any,
    **transport_kwargs: Any,
) -> AsyncIterator[AsyncClient]:
    """Yield an ``httpx.AsyncClient`` wired to *app* via ``ASGITransport``."""
    async with AsyncClient(
        transport=ASGITransport(app=app, **transport_kwargs), base_url="http://test"
    ) as client:
        yield client


@asynccontextmanager
async def mock_app(
    tokens: list[str] | None = None,
    *,
    error: Exception | None = None,
    raise_app_exceptions: bool | None = None,
    **create_app_kwargs: Any,
) -> AsyncIterator[AppFixture]:
    """Create a ``MockAgent``, build ``create_app``, and yield an ``AppFixture``."""
    agent_kwargs: dict[str, Any] = {}
    if tokens is not None:
        agent_kwargs["tokens"] = tokens
    if error is not None:
        agent_kwargs["error"] = error
    agent = MockAgent(**agent_kwargs)

    app = create_app(agent, **create_app_kwargs)

    transport_kwargs: dict[str, Any] = {}
    if raise_app_exceptions is not None:
        transport_kwargs["raise_app_exceptions"] = raise_app_exceptions

    async with http_client(app, **transport_kwargs) as client:
        yield AppFixture(agent=agent, app=app, client=client)


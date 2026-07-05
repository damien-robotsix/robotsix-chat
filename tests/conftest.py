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

import pytest  # noqa: E402
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


# -- pytest fixtures -------------------------------------------------------


@pytest.fixture(scope="session")
def agent() -> MockAgent:
    """A :class:`MockAgent` with the default token sequence."""
    return MockAgent()


@pytest.fixture(scope="session")
def app(agent: MockAgent) -> Starlette:
    """The Starlette application wired to the default mock agent."""
    return create_app(agent)


@pytest.fixture
async def async_client(app: Starlette) -> AsyncIterator[AsyncClient]:
    """An ``httpx.AsyncClient`` wired to *app* via ``ASGITransport``."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def mock_app(request: pytest.FixtureRequest) -> AsyncIterator[AppFixture]:
    """Build an :class:`AppFixture` with a customisable agent and app.

    Use ``@pytest.mark.parametrize('mock_app', [{...}], indirect=True)``
    to pass customisation parameters.  Supported keys:

    * ``tokens`` — token list forwarded to :class:`MockAgent`
    * ``error`` — exception that :class:`MockAgent` raises on ``stream()``
    * ``raise_app_exceptions`` — forwarded to ``ASGITransport``
    * any other key is forwarded to :func:`create_app` as a keyword argument

    Without parametrization, ``mock_app`` uses a default :class:`MockAgent`
    and default ``create_app`` arguments.
    """
    params: dict[str, Any] = dict(getattr(request, "param", None) or {})

    tokens = params.pop("tokens", None)
    error = params.pop("error", None)
    raise_app_exceptions = params.pop("raise_app_exceptions", None)

    agent_kwargs: dict[str, Any] = {}
    if tokens is not None:
        agent_kwargs["tokens"] = tokens
    if error is not None:
        agent_kwargs["error"] = error
    agent_obj = MockAgent(**agent_kwargs)

    # Provide a default SubsessionRegistry so tests that register subsessions
    # locally can still interact with the app's registry.
    if "subsession_registry" not in params and "event_bus" not in params:
        from robotsix_chat.chat.events import EventBus  # noqa: E402
        from robotsix_chat.subsessions import SubsessionRegistry  # noqa: E402

        params.setdefault("subsession_registry", SubsessionRegistry(store_path=None))
        params.setdefault("event_bus", EventBus())

    app_obj = create_app(agent_obj, **params)

    transport_kwargs: dict[str, Any] = {}
    if raise_app_exceptions is not None:
        transport_kwargs["raise_app_exceptions"] = raise_app_exceptions

    async with AsyncClient(
        transport=ASGITransport(app=app_obj, **transport_kwargs),
        base_url="http://test",
    ) as client:
        yield AppFixture(agent=agent_obj, app=app_obj, client=client)


# -- legacy helpers (used by tests that build their own Starlette app) -----


@asynccontextmanager
async def http_client(
    app: Any,
    **transport_kwargs: Any,
) -> AsyncIterator[AsyncClient]:
    """Yield an ``httpx.AsyncClient`` wired to *app* via ``ASGITransport``.

    Prefer the :func:`async_client` fixture for tests using the default app.
    This helper remains for tests that construct their own ``Starlette``
    instance (e.g. the subsession end-to-end lifecycle test).
    """
    async with AsyncClient(
        transport=ASGITransport(app=app, **transport_kwargs), base_url="http://test"
    ) as client:
        yield client

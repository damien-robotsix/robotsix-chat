"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Ensure locally-installed packages (asgi-correlation-id etc.) are
# importable before any application code is loaded.
_local_pkgs = Path(__file__).resolve().parent.parent / "local-deps"
if str(_local_pkgs) not in sys.path and _local_pkgs.is_dir():
    sys.path.insert(0, str(_local_pkgs))

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_llmio_failover_tracker() -> Any:
    """Isolate every test from llmio's process-wide provider-failover state.

    Tests that arm the failover window (or drive default-slot failures)
    would otherwise leak an armed window into unrelated tests, silently
    flipping slot resolution — including ``level_needs_api_key`` and the
    ``/models`` payload.
    """
    from robotsix_llmio.core.failover import reset_failover_tracker

    reset_failover_tracker()
    yield
    reset_failover_tracker()


@pytest.fixture(autouse=True)
def _reset_feedback_repo_cache() -> Any:
    """Isolate every test from the feedback runner's allowed-repo cache.

    ``robotsix_chat.feedback.runner`` caches the resolved deploy∩mill repo
    set in a module-global dict (``_repo_cache``) for ``_REPO_CACHE_TTL``
    seconds.  Without a reset the cached list leaks across tests, so under
    certain random orderings a value resolved by one test (e.g. a run that
    fell back to a single repo) is still live when a later ``_run`` test
    reads it — silently filtering out the tickets that test expects to file.
    """
    from robotsix_chat.feedback import runner as _feedback_runner

    _feedback_runner._repo_cache.clear()
    yield
    _feedback_runner._repo_cache.clear()


from robotsix_chat.chat.server import create_app  # noqa: E402

if TYPE_CHECKING:
    from robotsix_chat.config.models import MobileAuthSettings


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
        self.model_level: int | None = None

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
        trace_metadata: dict[str, str] | None = None,
        trace_name: str | None = None,
        model_level: int | None = None,
        skip_recall: bool = False,
    ) -> AsyncIterator[str]:
        """Yield tokens or raise the configured error."""
        self.call_count += 1
        self.called_with = message
        self.history = history
        self.session_id = session_id
        self.client_id = client_id
        self.images = images
        self.model_level = model_level
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


ClientFactory = Callable[..., TestClient]
"""Type of the factory returned by the :func:`client_factory` fixture."""


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


@pytest.fixture
def client_factory() -> ClientFactory:
    """Return a factory building a sync ``starlette.testclient.TestClient``.

    The sync counterpart of :func:`mock_app`: with no ``app`` argument it
    builds ``create_app`` around a fresh :class:`MockAgent` (extra keyword
    arguments are forwarded to ``create_app``), so route tests stop
    re-implementing the ``create_app(agent)`` + ``TestClient`` wrapper per
    module. Pass a pre-built ``app`` to wrap a minimal test app instead.
    Dependency overrides ``create_app`` accepts (``mobile_auth``, and any
    other ``create_app`` keyword) are forwarded as first-class keyword
    arguments — the auth suite builds its auth-injected app via
    ``client_factory(mobile_auth=auth)`` instead of standing up its own
    Starlette app. ``raise_server_exceptions`` defaults to ``False`` — the
    failure-mode semantics the per-module helpers all encoded: error
    responses are observed, not exceptions raised into the test.
    """

    def _make_client(
        app: Starlette | None = None,
        *,
        tokens: list[str] | None = None,
        error: Exception | None = None,
        mobile_auth: MobileAuthSettings | None = None,
        raise_server_exceptions: bool = False,
        **create_app_kwargs: Any,
    ) -> TestClient:
        if app is None:
            agent_kwargs: dict[str, Any] = {}
            if tokens is not None:
                agent_kwargs["tokens"] = tokens
            if error is not None:
                agent_kwargs["error"] = error
            if mobile_auth is not None:
                create_app_kwargs["mobile_auth"] = mobile_auth
            app = create_app(MockAgent(**agent_kwargs), **create_app_kwargs)
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    return _make_client

"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

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
        self.called_with: str | None = None
        # Capture the conversation context the server passes, for assertions.
        self.history: list[tuple[str, str]] | None = None
        self.session_id: str | None = None

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
        self.called_with = message
        self.history = history
        self.session_id = session_id
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


# ---------------------------------------------------------------------------
# Shared fake robotsix_agent_comm helpers
# ---------------------------------------------------------------------------


class _FakeMetadata:
    """Stand-in for robotsix_agent_comm.protocol.Metadata."""

    @staticmethod
    def create(sender: str) -> _FakeMetadata:
        return _FakeMetadata()


class _FakeRequest:
    """Stand-in for robotsix_agent_comm.protocol.messages.Request."""

    def __init__(
        self,
        metadata: Any = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        self.metadata = metadata
        self.body = body or {}


class _FakeResponse:
    """Stand-in for robotsix_agent_comm.protocol.messages.Response."""

    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self.body = body or {}

    @classmethod
    def to(cls, request: Any, *, body: dict[str, Any] | None = None) -> _FakeResponse:
        return cls(body=body)


class _FakeError:
    """Stand-in for robotsix_agent_comm.protocol.Error."""

    def __init__(self, body: Any = None) -> None:
        self.body = body or {}

    @classmethod
    def to(
        cls,
        request: Any,
        *,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> _FakeError:
        body: dict[str, Any] = {"code": code, "message": message}
        if details:
            body["details"] = details
        body.update(kwargs)
        return cls(body=body)


class _FakeProtocolError(Exception):
    """Stand-in for robotsix_agent_comm.protocol.Error (structured)."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class _FakeBrokeredAgent:
    """Fake SDK BrokeredAgent capturing registration + handler invocation."""

    def __init__(
        self,
        agent_id: str,
        *,
        broker_host: str,
        broker_token: str | None,
        broker_port: int = 443,
        broker_scheme: str = "https",
        tls_ca: str | None = None,
        ssl_context: object | None = None,
        timeout: float = 30.0,
        on_request: Any = None,
        on_notification: Any = None,
    ) -> None:
        self.agent_id = agent_id
        self.broker_host = broker_host
        self.broker_token = broker_token
        self.broker_port = broker_port
        self.broker_scheme = broker_scheme
        self.timeout = timeout
        self._on_request = on_request
        self._on_notification = on_notification
        self._running = False

    def on_request(self, handler: Any) -> Any:
        """Register the request handler (also returns it)."""
        self._on_request = handler
        return handler

    def on_notification(self, handler: Any) -> Any:
        """Register the notification handler."""
        self._on_notification = handler
        return handler

    def start(self) -> None:
        """No-op start."""
        self._running = True

    def stop(self) -> None:
        """No-op stop."""
        self._running = False

    def serve_forever(self) -> None:
        """Blocking serve loop — no-op in the fake."""
        self._running = True

    def invoke_handler(self, request: Any) -> Any:
        """Invoke the registered on_request handler directly (test hook)."""
        if self._on_request is None:
            raise RuntimeError("No request handler registered")
        return self._on_request(request)


class _Reply:
    def __init__(self, body: Any) -> None:
        self.body = body


def _install_fake_agent_comm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply: Any = None,
    raise_exc: Exception | None = None,
) -> dict[str, Any]:
    """Install a fake robotsix_agent_comm module tree; return a capture dict."""
    captured: dict[str, Any] = {}

    class _FakeBrokeredRequester:
        def __init__(
            self,
            agent_id: str,
            target_agent_id: str,
            *,
            broker_host: str,
            broker_token: str | None,
            broker_port: int = 443,
            broker_scheme: str = "https",
            broker_ssl_context: object | None = None,
            timeout: float = 30.0,
            default_reply: str = "",
        ) -> None:
            captured["agent_id"] = agent_id
            captured["recipient"] = target_agent_id
            captured["broker_host"] = broker_host
            captured["broker_port"] = broker_port
            captured["broker_scheme"] = broker_scheme
            captured["broker_token"] = broker_token
            captured["timeout"] = timeout
            captured["default_reply"] = default_reply
            self._raise_exc = raise_exc
            self._reply = reply
            self._default_reply = default_reply

        def request(
            self,
            payload: dict[str, Any] | None = None,
            *,
            timeout: float | None = None,
            default: str | None = None,
        ) -> str:
            captured["payload"] = payload
            if self._raise_exc is not None:
                raise self._raise_exc
            if isinstance(self._reply, _FakeError):
                msg = (
                    self._reply.body.get("message")
                    if isinstance(self._reply.body, dict)
                    else None
                )
                raise RuntimeError(f"brokered request to ... failed: {msg}")
            body = getattr(self._reply, "body", self._reply)
            # Replicate reply_text behaviour used by the real BrokeredRequester
            if isinstance(body, dict):
                r = body.get("reply")
                if r is not None and r != "":
                    return r if isinstance(r, str) else str(r)
                return str(body)
            if body is None:
                return default if default is not None else self._default_reply
            return str(body)

    root = types.ModuleType("robotsix_agent_comm")
    sdk = types.ModuleType("robotsix_agent_comm.sdk")
    protocol = types.ModuleType("robotsix_agent_comm.protocol")
    protocol_messages = types.ModuleType("robotsix_agent_comm.protocol.messages")

    # Give the modules a valid __spec__ and __path__ so find_spec
    # and submodule imports work against the fake tree.
    import importlib.machinery

    _spec = importlib.machinery.ModuleSpec
    root.__spec__ = _spec("robotsix_agent_comm", None)
    root.__path__ = []  # prevent fallback to real filesystem submodules
    sdk.__spec__ = _spec("robotsix_agent_comm.sdk", None)
    protocol.__spec__ = _spec("robotsix_agent_comm.protocol", None)
    protocol.__path__ = []
    protocol_messages.__spec__ = _spec("robotsix_agent_comm.protocol.messages", None)

    sdk.BrokeredRequester = _FakeBrokeredRequester  # type: ignore[attr-defined]
    sdk.BrokeredAgent = _FakeBrokeredAgent  # type: ignore[attr-defined]

    # -- Fake protocol types (module-level _FakeMetadata, _FakeRequest,  ---
    #    _FakeResponse, _FakeError) ---------------------------------------

    protocol.Metadata = _FakeMetadata  # type: ignore[attr-defined]
    protocol_messages.Request = _FakeRequest  # type: ignore[attr-defined]
    protocol_messages.Response = _FakeResponse  # type: ignore[attr-defined]
    protocol_messages.Error = _FakeError  # type: ignore[attr-defined]
    # Re-export at the protocol package level so
    # ``from robotsix_agent_comm.protocol import Error, Response`` works.
    protocol.Error = _FakeError  # type: ignore[attr-defined]
    protocol.Response = _FakeResponse  # type: ignore[attr-defined]

    for name, mod in {
        "robotsix_agent_comm": root,
        "robotsix_agent_comm.sdk": sdk,
        "robotsix_agent_comm.protocol": protocol,
        "robotsix_agent_comm.protocol.messages": protocol_messages,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return captured


# ---------------------------------------------------------------------------
# Import-time fake robotsix_agent_comm modules for test collection
# ---------------------------------------------------------------------------
# The test suite must be collectible even when the ``broker`` extra is not
# installed.  Install minimal stand-in modules in ``sys.modules`` at
# conftest import time so that ``from robotsix_agent_comm.protocol import
# Metadata`` (etc.) succeeds during discovery.  Fixtures that need a richer
# fake (e.g. *fake_broker*) replace these via ``_install_fake_agent_comm``.

if "robotsix_agent_comm" not in sys.modules:
    import importlib.machinery as _importlib_machinery

    _spec = _importlib_machinery.ModuleSpec

    _root = types.ModuleType("robotsix_agent_comm")
    _sdk = types.ModuleType("robotsix_agent_comm.sdk")
    _protocol = types.ModuleType("robotsix_agent_comm.protocol")
    _protocol_messages = types.ModuleType("robotsix_agent_comm.protocol.messages")

    _root.__spec__ = _spec("robotsix_agent_comm", None)
    _root.__path__ = []
    _sdk.__spec__ = _spec("robotsix_agent_comm.sdk", None)
    _protocol.__spec__ = _spec("robotsix_agent_comm.protocol", None)
    _protocol.__path__ = []
    _protocol_messages.__spec__ = _spec("robotsix_agent_comm.protocol.messages", None)

    _protocol.Metadata = _FakeMetadata  # type: ignore[attr-defined]
    _protocol_messages.Request = _FakeRequest  # type: ignore[attr-defined]
    _protocol_messages.Response = _FakeResponse  # type: ignore[attr-defined]
    _protocol_messages.Error = _FakeError  # type: ignore[attr-defined]
    _protocol.Error = _FakeError  # type: ignore[attr-defined]
    _protocol.Response = _FakeResponse  # type: ignore[attr-defined]

    sys.modules["robotsix_agent_comm"] = _root
    sys.modules["robotsix_agent_comm.sdk"] = _sdk
    sys.modules["robotsix_agent_comm.protocol"] = _protocol
    sys.modules["robotsix_agent_comm.protocol.messages"] = _protocol_messages

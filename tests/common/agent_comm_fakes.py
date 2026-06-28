"""Shared fake ``robotsix_agent_comm`` module-tree helpers.

Provides stand-in classes for protocol types and a reusable
:func:`_install_fake_agent_comm` factory so that every test file
that needs to monkey-patch the broker SDK can use the same
implementation.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fake protocol types
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


# ---------------------------------------------------------------------------
# Fake SDK agent (BrokeredAgent)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _install_fake_agent_comm factory
# ---------------------------------------------------------------------------


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

    class _FakeBrokeredAgentWithSend:
        """Fake SDK BrokeredAgent that captures ``send_request`` calls.

        Mirrors the real ``BrokeredAgent`` constructor signature and adds a
        ``send_request`` method whose return value / exception is controlled
        by the *reply* and *raise_exc* parameters of
        :func:`_install_fake_agent_comm`.
        """

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
            captured["agent_id"] = agent_id
            captured["broker_host"] = broker_host
            captured["broker_port"] = broker_port
            captured["broker_scheme"] = broker_scheme
            captured["broker_token"] = broker_token
            captured["timeout"] = timeout
            self.agent_id = agent_id
            self._raise_exc = raise_exc
            self._reply = reply
            self._on_request = on_request
            self._on_notification = on_notification
            self._running = False

        def start(self) -> None:
            self._running = True

        def stop(self) -> None:
            self._running = False

        def serve_forever(self) -> None:
            self._running = True

        def send_request(
            self,
            recipient: str,
            body: dict[str, Any] | None = None,
            *,
            timeout: float | None = None,
            **extra: Any,
        ) -> Any:
            captured["recipient"] = recipient
            captured["payload"] = body
            if self._raise_exc is not None:
                raise self._raise_exc
            if isinstance(self._reply, _FakeError):
                msg = (
                    self._reply.body.get("message")
                    if isinstance(self._reply.body, dict)
                    else None
                )
                raise RuntimeError(f"brokered request to ... failed: {msg}")
            # Return the configured reply directly — it already carries
            # a ``.body`` attribute that _extract_reply_text will read.
            return self._reply

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
    sdk.BrokeredAgent = _FakeBrokeredAgentWithSend  # type: ignore[attr-defined]

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

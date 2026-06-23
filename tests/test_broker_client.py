"""Tests for :class:`BaseBrokeredClient` — the shared brokered client base.

robotsix-agent-comm is faked via ``sys.modules`` so these run without the
``broker`` extra installed and never touch a real broker.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from robotsix_chat.broker_client import BaseBrokeredClient


def _settings(**kw: Any) -> Any:
    """Return a mock settings object with broker-related attributes."""
    from types import SimpleNamespace

    defaults: dict[str, Any] = {
        "agent_id": "test-agent",
        "broker_host": "broker.example.com",
        "broker_port": 443,
        "broker_scheme": "https",
        "broker_token": "test-token",
        "timeout": 60.0,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class _FakeError:
    """Stand-in for robotsix_agent_comm.protocol.Error."""

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
            captured["target_agent_id"] = target_agent_id
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
    sdk.BrokeredRequester = _FakeBrokeredRequester  # type: ignore[attr-defined]

    for name, mod in {
        "robotsix_agent_comm": root,
        "robotsix_agent_comm.sdk": sdk,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return captured


class _Reply:
    def __init__(self, body: Any) -> None:
        self.body = body


# ---------------------------------------------------------------------------
# __init__ tests
# ---------------------------------------------------------------------------


def test_init_constructs_brokered_requester_with_correct_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BrokeredRequester receives all settings fields and constructor args."""
    captured = _install_fake_agent_comm(monkeypatch)
    s = _settings(
        agent_id="chat-agent",
        broker_host="host.example.com",
        broker_port=8443,
        broker_scheme="http",
        broker_token="secret-token",
        timeout=120.0,
    )
    BaseBrokeredClient(
        s,
        target_agent_id="target-agent-1",
        default_reply="no reply from target",
    )
    assert captured["agent_id"] == "chat-agent"
    assert captured["target_agent_id"] == "target-agent-1"
    assert captured["broker_host"] == "host.example.com"
    assert captured["broker_port"] == 8443
    assert captured["broker_scheme"] == "http"
    assert captured["broker_token"] == "secret-token"  # pragma: allowlist secret
    assert captured["timeout"] == 120.0
    assert captured["default_reply"] == "no reply from target"


# ---------------------------------------------------------------------------
# consult() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_empty_request_returns_empty_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty or whitespace-only request returns empty_reply without calling broker."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "x"}))
    client = BaseBrokeredClient(
        _settings(), target_agent_id="t", default_reply="default"
    )
    out = await client.consult("   ", empty_reply="EMPTY", error_label="test")
    assert out == "EMPTY"
    assert "payload" not in captured  # never contacted broker


@pytest.mark.asyncio
async def test_consult_valid_request_calls_broker_via_to_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid request calls BrokeredRequester.request via asyncio.to_thread."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "done"}))
    client = BaseBrokeredClient(
        _settings(), target_agent_id="t", default_reply="default"
    )

    # Patch asyncio.to_thread to verify it is used for the broker call.
    _real_to_thread = asyncio.to_thread
    to_thread_calls: list[tuple[object, object]] = []

    async def _fake_to_thread(func: object, *args: object) -> object:
        to_thread_calls.append((func, args))
        return await _real_to_thread(func, *args)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

    out = await client.consult("hello", empty_reply="EMPTY", error_label="test")
    assert out == "done"
    assert captured["payload"] == {"message": "hello"}
    assert len(to_thread_calls) == 1
    # The function passed to to_thread should be the requester's request method.
    assert to_thread_calls[0][0] == client._requester.request
    assert to_thread_calls[0][1] == ({"message": "hello"},)


@pytest.mark.asyncio
async def test_consult_forwards_extra_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra keyword arguments are forwarded into the request payload."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "ok"}))
    client = BaseBrokeredClient(
        _settings(), target_agent_id="t", default_reply="default"
    )
    out = await client.consult(
        "query", empty_reply="E", error_label="test", domain="tasks", repo_id="x"
    )
    assert out == "ok"
    assert captured["payload"] == {
        "message": "query",
        "domain": "tasks",
        "repo_id": "x",
    }


@pytest.mark.asyncio
async def test_consult_exception_caught_and_returned_as_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceptions from the broker are caught and returned as error text."""
    _install_fake_agent_comm(monkeypatch, raise_exc=RuntimeError("broker down"))
    client = BaseBrokeredClient(
        _settings(), target_agent_id="t", default_reply="default"
    )
    out = await client.consult("hi", empty_reply="E", error_label="my-service")
    assert "could not be completed" in out.lower()
    assert "my-service" in out
    assert "broker down" in out


# ---------------------------------------------------------------------------
# lazy import tests
# ---------------------------------------------------------------------------


def test_lazy_import_works_when_extra_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the broker extra is in sys.modules, the lazy import succeeds."""
    _install_fake_agent_comm(monkeypatch)
    # This should not raise — the import inside __init__ finds our fake.
    client = BaseBrokeredClient(
        _settings(), target_agent_id="t", default_reply="default"
    )
    assert client is not None


def test_lazy_import_missing_raises_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the broker extra is absent, constructing raises ImportError."""
    # Ensure neither module is present in sys.modules.
    for mod_name in ("robotsix_agent_comm.sdk", "robotsix_agent_comm"):
        monkeypatch.delitem(sys.modules, mod_name, raising=False)

    with pytest.raises(ImportError):
        BaseBrokeredClient(_settings(), target_agent_id="t", default_reply="default")

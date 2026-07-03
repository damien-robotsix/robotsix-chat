"""Tests for :class:`BaseBrokeredClient` — the shared brokered client base.

robotsix-agent-comm is faked via ``sys.modules`` so these run without the
``broker`` extra installed and never touch a real broker.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
import pytest

from robotsix_chat import broker_client
from robotsix_chat.broker_client import BaseBrokeredClient, BrokerUnavailableError

from ..conftest import _install_fake_agent_comm, _Reply


class _FakeResp:
    """Minimal stand-in for an ``httpx.Response`` from ``GET /agents``."""

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _patch_preflight_ok(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    """Make the pre-flight reachability check a no-op pass for *client*."""
    monkeypatch.setattr(client, "_check_reachable", lambda: (True, ""))


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
    # Wrap broker_token in SecretStr so .get_secret_value() works.
    from pydantic import SecretStr

    if "broker_token" in defaults:
        defaults["broker_token"] = SecretStr(defaults["broker_token"])
    return SimpleNamespace(**defaults)


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
    assert captured["recipient"] == "target-agent-1"
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
    _patch_preflight_ok(monkeypatch, client)

    # Patch asyncio.to_thread to verify it is used for the broker call.
    _real_to_thread = asyncio.to_thread
    to_thread_calls: list[tuple[object, object]] = []

    async def _fake_to_thread(func: Any, *args: Any) -> Any:
        to_thread_calls.append((func, args))
        return await _real_to_thread(func, *args)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

    out = await client.consult("hello", empty_reply="EMPTY", error_label="test")
    assert out == "done"
    assert captured["payload"] == {"message": "hello"}
    # The broker request runs via to_thread (alongside the pre-flight check).
    request_calls = [c for c in to_thread_calls if c[0] == client._requester.request]
    assert len(request_calls) == 1
    assert request_calls[0][1] == ({"message": "hello"},)


@pytest.mark.asyncio
async def test_consult_forwards_extra_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra keyword arguments are forwarded into the request payload."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "ok"}))
    client = BaseBrokeredClient(
        _settings(), target_agent_id="t", default_reply="default"
    )
    _patch_preflight_ok(monkeypatch, client)
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
async def test_request_key_override_changes_payload_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subclass overriding ``_request_key`` sends the request under that key.

    The calendar agent reads ``"instruction"`` (not the board-manager's
    ``"message"``); regression guard for that wire-contract mismatch.
    """
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "ok"}))

    class _InstructionClient(BaseBrokeredClient):
        _request_key = "instruction"

    client = _InstructionClient(
        _settings(), target_agent_id="t", default_reply="default"
    )
    _patch_preflight_ok(monkeypatch, client)
    out = await client.consult("book a meeting", empty_reply="E", error_label="cal")
    assert out == "ok"
    assert captured["payload"] == {"instruction": "book a meeting"}
    assert "message" not in captured["payload"]


@pytest.mark.asyncio
async def test_consult_exception_caught_and_returned_as_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceptions from the broker are caught and returned as error text."""
    _install_fake_agent_comm(monkeypatch, raise_exc=RuntimeError("broker down"))
    client = BaseBrokeredClient(
        _settings(), target_agent_id="t", default_reply="default"
    )
    _patch_preflight_ok(monkeypatch, client)
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


# ---------------------------------------------------------------------------
# pre-flight reachability check
# ---------------------------------------------------------------------------


def test_check_reachable_ok_when_agent_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broker reachable + target registered → (True, '')."""
    _install_fake_agent_comm(monkeypatch)
    client = BaseBrokeredClient(
        _settings(), target_agent_id="board-mgr", default_reply="d"
    )
    monkeypatch.setattr(
        broker_client.httpx,
        "get",
        lambda *a, **k: _FakeResp(
            200, {"agents": [{"agent_id": "board-mgr"}, {"agent_id": "other"}]}
        ),
    )
    assert client._check_reachable() == (True, "")


def test_check_reachable_false_when_broker_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport error → (False, 'broker unreachable ...')."""
    _install_fake_agent_comm(monkeypatch)
    client = BaseBrokeredClient(_settings(), target_agent_id="t", default_reply="d")

    def _raise(*a: Any, **k: Any) -> Any:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(broker_client.httpx, "get", _raise)
    ok, reason = client._check_reachable()
    assert ok is False
    assert "unreachable" in reason


def test_check_reachable_false_when_agent_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broker reachable but target not in the registry → (False, 'not registered')."""
    _install_fake_agent_comm(monkeypatch)
    client = BaseBrokeredClient(
        _settings(), target_agent_id="board-mgr", default_reply="d"
    )
    monkeypatch.setattr(
        broker_client.httpx,
        "get",
        lambda *a, **k: _FakeResp(200, {"agents": [{"agent_id": "someone-else"}]}),
    )
    ok, reason = client._check_reachable()
    assert ok is False
    assert "not registered" in reason


def test_check_reachable_permissive_on_non_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-200 (auth/rate-limit hiccup) does not block the real request."""
    _install_fake_agent_comm(monkeypatch)
    client = BaseBrokeredClient(_settings(), target_agent_id="t", default_reply="d")
    monkeypatch.setattr(broker_client.httpx, "get", lambda *a, **k: _FakeResp(503, {}))
    assert client._check_reachable() == (True, "")


@pytest.mark.asyncio
async def test_consult_preflight_failure_raises_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pre-flight raises BrokerUnavailableError without sending the request."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "x"}))
    client = BaseBrokeredClient(_settings(), target_agent_id="t", default_reply="d")
    monkeypatch.setattr(
        client, "_check_reachable", lambda: (False, "broker unreachable (boom)")
    )

    with pytest.raises(BrokerUnavailableError):
        await client.consult("hi", empty_reply="E", error_label="mill")

    # The broker request was never sent.
    assert "payload" not in captured

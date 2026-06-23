"""Tests for the mill integration — :func:`build_mill_tools` and ``MillClient``.

robotsix-agent-comm is faked via ``sys.modules`` so these run without the
``broker`` extra installed and never touch a real broker.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from robotsix_chat.config import MillSettings
from robotsix_chat.mill import build_mill_tools
from robotsix_chat.mill.client import MillClient


def _settings(**kw: Any) -> MillSettings:
    base: dict[str, Any] = {"enabled": True, "broker_token": "tok"}
    base.update(kw)
    return MillSettings(**base)


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
            captured["recipient"] = target_agent_id
            captured["broker_host"] = broker_host
            captured["broker_token"] = broker_token
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
# build_mill_tools
# ---------------------------------------------------------------------------


def test_build_mill_tools_disabled() -> None:
    """Verify that disabled mill returns no tools."""
    assert build_mill_tools(MillSettings(enabled=False)) == []


def test_build_mill_tools_without_broker_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that missing broker extra returns no tools."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert build_mill_tools(_settings()) == []


def test_build_mill_tools_returns_consult_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that enabled mill with broker extra returns the consult tool."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch)
    tools = build_mill_tools(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "consult_mill"


# ---------------------------------------------------------------------------
# MillClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_sends_nl_to_board_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that consult sends a natural-language request to the board manager."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "done"}))
    client = MillClient(_settings(agent_id="robotsix-chat"))
    out = await client.consult("create a ticket to add X")

    assert out == "done"
    assert captured["agent_id"] == "robotsix-chat"
    assert captured["recipient"] == "board-manager-robotsix-mill"
    assert captured["payload"] == {"message": "create a ticket to add X"}
    assert captured["broker_token"] == "tok"  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_consult_includes_repo_id_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that repo_id is included in the payload when set."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "ok"}))
    client = MillClient(_settings(repo_id="robotsix-chat"))
    await client.consult("status?")
    assert captured["payload"] == {"message": "status?", "repo_id": "robotsix-chat"}


@pytest.mark.asyncio
async def test_consult_blank_request_skips_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return an error for blank requests without contacting the broker.

    The broker is never contacted for empty or whitespace-only requests.
    """
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "x"}))
    client = MillClient(_settings())
    out = await client.consult("   ")
    assert "No request" in out
    assert "payload" not in captured  # never contacted the broker


@pytest.mark.asyncio
async def test_consult_never_raises_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that transport errors degrade to a message instead of raising."""
    _install_fake_agent_comm(monkeypatch, raise_exc=RuntimeError("broker down"))
    client = MillClient(_settings())
    out = await client.consult("hi")
    assert "could not be completed" in out.lower()
    assert "broker down" in out


@pytest.mark.asyncio
async def test_consult_handles_board_error_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that board manager error replies are surfaced as text."""
    err = _FakeError({"code": "BAD_REQUEST", "message": "nope"})
    _install_fake_agent_comm(monkeypatch, reply=err)
    client = MillClient(_settings())
    out = await client.consult("do a thing")
    assert "nope" in out

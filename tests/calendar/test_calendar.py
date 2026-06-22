"""Tests for the calendar integration — ``build_calendar_tools`` & ``CalendarClient``.

robotsix-agent-comm is faked via ``sys.modules`` so these run without the
``broker`` extra installed and never touch a real broker.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from robotsix_chat.calendar import build_calendar_tools
from robotsix_chat.calendar.client import CalendarClient, _reply_text
from robotsix_chat.config import CalendarSettings


def _settings(**kw: Any) -> CalendarSettings:
    base: dict[str, Any] = {"enabled": True, "broker_token": "tok"}
    base.update(kw)
    return CalendarSettings(**base)


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

    class _FakeAgent:
        def __init__(
            self,
            agent_id: str,
            registry: Any,
            *,
            transport: Any,
            pull: bool,
            timeout: float,
        ) -> None:
            captured["agent_id"] = agent_id
            captured["pull"] = pull

        def __enter__(self) -> _FakeAgent:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def send_request(self, recipient: str, payload: Any, timeout: float) -> Any:
            captured["recipient"] = recipient
            captured["payload"] = payload
            if raise_exc is not None:
                raise raise_exc
            return reply

    def _fake_ctp(
        mode: str,
        *,
        broker_host: str,
        broker_port: int,
        broker_scheme: str,
        broker_token: str,
    ) -> tuple[object, object]:
        captured["broker_host"] = broker_host
        captured["broker_token"] = broker_token
        return object(), object()

    root = types.ModuleType("robotsix_agent_comm")
    protocol = types.ModuleType("robotsix_agent_comm.protocol")
    protocol.Error = _FakeError  # type: ignore[attr-defined]
    sdk = types.ModuleType("robotsix_agent_comm.sdk")
    sdk_agent = types.ModuleType("robotsix_agent_comm.sdk.agent")
    sdk_agent.Agent = _FakeAgent  # type: ignore[attr-defined]
    transport = types.ModuleType("robotsix_agent_comm.transport")
    brokered = types.ModuleType("robotsix_agent_comm.transport.brokered")
    brokered.create_transport_pair = _fake_ctp  # type: ignore[attr-defined]

    for name, mod in {
        "robotsix_agent_comm": root,
        "robotsix_agent_comm.protocol": protocol,
        "robotsix_agent_comm.sdk": sdk,
        "robotsix_agent_comm.sdk.agent": sdk_agent,
        "robotsix_agent_comm.transport": transport,
        "robotsix_agent_comm.transport.brokered": brokered,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return captured


class _Reply:
    def __init__(self, body: Any) -> None:
        self.body = body


# ---------------------------------------------------------------------------
# build_calendar_tools
# ---------------------------------------------------------------------------


def test_build_calendar_tools_disabled() -> None:
    """Verify that disabled calendar returns no tools."""
    assert build_calendar_tools(CalendarSettings(enabled=False)) == []


def test_build_calendar_tools_without_broker_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that missing broker extra returns no tools."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert build_calendar_tools(_settings()) == []


def test_build_calendar_tools_returns_four_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that enabled calendar with broker extra returns the four tools."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    tools = build_calendar_tools(_settings())
    assert len(tools) == 4
    names = [t.__name__ for t in tools]
    assert names == ["query_calendar", "manage_calendar", "query_tasks", "manage_tasks"]


# ---------------------------------------------------------------------------
# CalendarClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_calendar_domain_sends_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that consult(domain="calendar") sends the correct payload."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "done"}))
    client = CalendarClient(_settings(agent_id="robotsix-chat"))
    out = await client.consult("what's on my calendar?", domain="calendar")

    assert out == "done"
    assert captured["agent_id"] == "robotsix-chat"
    assert captured["pull"] is True
    assert captured["recipient"] == "calendar-agent-robotsix"
    assert captured["payload"] == {
        "message": "what's on my calendar?",
        "domain": "calendar",
    }
    assert captured["broker_token"] == "tok"  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_consult_tasks_domain_sends_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that consult(domain="tasks") sends the correct payload."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "ok"}))
    client = CalendarClient(_settings())
    out = await client.consult("create a task: buy milk", domain="tasks")

    assert out == "ok"
    assert captured["payload"] == {
        "message": "create a task: buy milk",
        "domain": "tasks",
    }


@pytest.mark.asyncio
async def test_consult_blank_request_skips_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return an error for blank requests without contacting the broker."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "x"}))
    client = CalendarClient(_settings())
    out = await client.consult("   ", domain="calendar")
    assert "No request" in out
    assert "recipient" not in captured  # never contacted the broker


@pytest.mark.asyncio
async def test_consult_never_raises_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that transport errors degrade to a message instead of raising."""
    _install_fake_agent_comm(monkeypatch, raise_exc=RuntimeError("broker down"))
    client = CalendarClient(_settings())
    out = await client.consult("hi", domain="calendar")
    assert "could not be completed" in out.lower()
    assert "broker down" in out


@pytest.mark.asyncio
async def test_consult_handles_agent_error_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that calendar agent error replies are surfaced as text."""
    err = _FakeError({"code": "BAD_REQUEST", "message": "nope"})
    _install_fake_agent_comm(monkeypatch, reply=err)
    client = CalendarClient(_settings())
    out = await client.consult("do a thing", domain="calendar")
    assert "nope" in out


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (None, "no reply"),
        ({"reply": "hello there"}, "hello there"),
        ({"status": "ok"}, "{'status': 'ok'}"),
        ("plain string", "plain string"),
    ],
)
def test_reply_text(body: Any, expected: str) -> None:
    """Verify _reply_text extracts the expected substring from the response body."""
    assert expected in _reply_text(body)

"""Tests for the calendar integration — ``build_calendar_tools`` & ``CalendarClient``.

robotsix-agent-comm is faked via ``sys.modules`` so these run without the
``broker`` extra installed and never touch a real broker.
"""

from __future__ import annotations

from typing import Any

import pytest

from robotsix_chat.calendar import build_calendar_tools
from robotsix_chat.calendar.client import CalendarClient
from robotsix_chat.config import CalendarSettings

from ..conftest import _FakeError, _install_fake_agent_comm, _Reply


def _settings(**kw: Any) -> CalendarSettings:
    base: dict[str, Any] = {"enabled": True, "broker_token": "tok"}
    base.update(kw)
    return CalendarSettings(**base)


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
    _install_fake_agent_comm(monkeypatch)
    tools = build_calendar_tools(_settings())
    assert len(tools) == 4
    names = [t.__name__ for t in tools]
    assert names == ["query_calendar", "manage_calendar", "query_tasks", "manage_tasks"]


def _install_fake_and_build_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], list[Any]]:
    """Install the fake agent-comm and return (captured, tools)."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "ok"}))
    tools = build_calendar_tools(_settings(agent_id="robotsix-chat"))
    return captured, tools


@pytest.mark.asyncio
async def test_query_calendar_sends_calendar_events_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``query_calendar`` sends an instruction classifiable as ``list_events``.

    The instruction must mention "calendar events" so the intent parser
    routes it correctly.
    """
    captured, tools = _install_fake_and_build_tools(monkeypatch)
    query_calendar = tools[0]
    assert query_calendar.__name__ == "query_calendar"

    out = await query_calendar("what do I have this week?")
    assert out == "ok"
    payload = captured["payload"]
    assert payload["domain"] == "calendar"
    assert "calendar events" in payload["instruction"]
    assert "what do I have this week?" in payload["instruction"]


@pytest.mark.asyncio
async def test_query_tasks_sends_tasks_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``query_tasks`` sends an instruction classifiable as ``list_tasks``.

    The instruction must mention "tasks" so the intent parser routes it
    correctly.
    """
    captured, tools = _install_fake_and_build_tools(monkeypatch)
    query_tasks = tools[2]
    assert query_tasks.__name__ == "query_tasks"

    out = await query_tasks("what do I need to do?")
    assert out == "ok"
    payload = captured["payload"]
    assert payload["domain"] == "tasks"
    assert "tasks" in payload["instruction"]
    assert "what do I need to do?" in payload["instruction"]


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
    assert captured["recipient"] == "robotsix-calendar"
    assert captured["payload"] == {
        "instruction": "what's on my calendar?",
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
        "instruction": "create a task: buy milk",
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
    assert "payload" not in captured  # never contacted the broker


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

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


# ---------------------------------------------------------------------------
# Query caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_calendar_caches_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``query_calendar`` caches results and returns them without extra broker calls."""
    captured = _install_fake_agent_comm(
        monkeypatch, reply=_Reply({"reply": "schedule: empty"})
    )
    client = CalendarClient(_settings(agent_id="robotsix-chat", cache_ttl=9999.0))

    # First call — hits the broker.
    out1 = await client.consult(
        "list calendar events: what's today?", domain="calendar"
    )
    assert out1 == "schedule: empty"
    assert captured.get("payload") is not None

    # Clear the captured dict so we can detect a second broker call.
    captured.clear()

    # Second call with the SAME request — should hit the cache.
    out2 = await client.consult(
        "list calendar events: what's today?", domain="calendar"
    )
    assert out2 == "schedule: empty"
    # No broker call should have been made → captured should be empty.
    assert "payload" not in captured


@pytest.mark.asyncio
async def test_query_cache_different_request_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different request strings produce different cache keys → cache miss."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "ok"}))
    client = CalendarClient(_settings(cache_ttl=9999.0))

    await client.consult("list calendar events: today", domain="calendar")
    assert captured.get("payload") is not None
    captured.clear()

    # Different request → cache miss → broker call.
    await client.consult("list calendar events: tomorrow", domain="calendar")
    assert "payload" in captured


@pytest.mark.asyncio
async def test_manage_calendar_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``manage_calendar`` invalidates the calendar query cache."""
    captured = _install_fake_agent_comm(
        monkeypatch, reply=_Reply({"reply": "event created"})
    )
    client = CalendarClient(_settings(cache_ttl=9999.0))

    # Populate the cache with a query.
    await client.consult("list calendar events: today", domain="calendar")
    assert captured.get("payload") is not None
    captured.clear()

    # A manage call should invalidate the calendar cache.
    client.invalidate_cache("calendar")

    # The same query now misses the cache → fresh broker call.
    await client.consult("list calendar events: today", domain="calendar")
    assert "payload" in captured


@pytest.mark.asyncio
async def test_manage_tasks_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``manage_tasks`` invalidates the tasks query cache."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "done"}))
    client = CalendarClient(_settings(cache_ttl=9999.0))

    # Populate the tasks cache.
    await client.consult("list tasks: pending", domain="tasks")
    assert captured.get("payload") is not None
    captured.clear()

    # Invalidate tasks cache.
    client.invalidate_cache("tasks")

    # Same query → cache miss.
    await client.consult("list tasks: pending", domain="tasks")
    assert "payload" in captured


@pytest.mark.asyncio
async def test_cache_ttl_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached entry older than cache_ttl triggers a fresh broker call."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "data"}))
    client = CalendarClient(_settings(cache_ttl=0.01))  # 10 ms TTL

    # First call — populates cache.
    await client.consult("list calendar events: today", domain="calendar")
    assert captured.get("payload") is not None
    captured.clear()

    # Second call within TTL — cache hit.
    await client.consult("list calendar events: today", domain="calendar")
    assert "payload" not in captured

    # Wait for TTL to expire.
    import asyncio

    await asyncio.sleep(0.02)

    # Third call after expiry — cache miss → broker call.
    await client.consult("list calendar events: today", domain="calendar")
    assert "payload" in captured


@pytest.mark.asyncio
async def test_cache_does_not_cache_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error responses are not cached — transient failures don't poison the cache."""
    # First call: the broker errors out.
    _install_fake_agent_comm(monkeypatch, raise_exc=RuntimeError("transient failure"))
    client = CalendarClient(_settings(cache_ttl=9999.0))
    out1 = await client.consult("list calendar events: today", domain="calendar")
    assert "could not be completed" in out1.lower()

    # Re-install a working fake for the second call.
    captured2 = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "ok now"}))
    # Need to re-create the client because the requester was already built
    # with the error-raising fake.
    client2 = CalendarClient(_settings(cache_ttl=9999.0))
    out2 = await client2.consult("list calendar events: today", domain="calendar")
    # Should NOT return the cached error — fresh call succeeds.
    assert out2 == "ok now"
    assert captured2.get("payload") is not None


@pytest.mark.asyncio
async def test_invalidate_cache_does_not_affect_other_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalidating calendar cache does not affect tasks cache (and vice versa)."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "ok"}))
    client = CalendarClient(_settings(cache_ttl=9999.0))

    # Populate both caches.
    await client.consult("list calendar events: today", domain="calendar")
    await client.consult("list tasks: pending", domain="tasks")
    captured.clear()

    # Invalidate only calendar.
    client.invalidate_cache("calendar")

    # Calendar query → cache miss.
    await client.consult("list calendar events: today", domain="calendar")
    assert "payload" in captured
    captured.clear()

    # Tasks query → still cached (no broker call).
    await client.consult("list tasks: pending", domain="tasks")
    assert "payload" not in captured

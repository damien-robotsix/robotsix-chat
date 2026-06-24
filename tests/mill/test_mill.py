"""Tests for the mill integration — :func:`build_mill_tools` and ``MillClient``.

robotsix-agent-comm is faked via ``sys.modules`` so these run without the
``broker`` extra installed and never touch a real broker.
"""

from __future__ import annotations

from typing import Any

import pytest

from robotsix_chat.config import MillSettings
from robotsix_chat.mill import build_mill_tools
from robotsix_chat.mill.client import MillClient

from ..conftest import _FakeError, _install_fake_agent_comm, _Reply


def _settings(**kw: Any) -> MillSettings:
    base: dict[str, Any] = {"enabled": True, "broker_token": "tok"}
    base.update(kw)
    return MillSettings(**base)


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
    assert len(tools) == 2
    assert tools[0].__name__ == "consult_mill"
    assert tools[1].__name__ == "get_board_write_queue_status"


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

"""Tests for the mail board integration — :func:`build_mail_tools` and ``MailClient``.

robotsix-agent-comm is faked via ``sys.modules`` so these run without the
``broker`` extra installed and never touch a real broker.
"""

from __future__ import annotations

from typing import Any

import pytest

from robotsix_chat.config import MailSettings
from robotsix_chat.mail import build_mail_tools
from robotsix_chat.mail.client import MailClient

from ..conftest import _FakeError, _install_fake_agent_comm, _Reply


def _settings(**kw: Any) -> MailSettings:
    base: dict[str, Any] = {"enabled": True, "broker_token": "tok"}
    base.update(kw)
    return MailSettings(**base)


# ---------------------------------------------------------------------------
# build_mail_tools
# ---------------------------------------------------------------------------


def test_build_mail_tools_disabled() -> None:
    """Verify that disabled mail returns no tools."""
    assert build_mail_tools(MailSettings(enabled=False)) == []


def test_build_mail_tools_without_broker_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that missing broker extra returns no tools."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert build_mail_tools(_settings()) == []


def test_build_mail_tools_returns_consult_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that enabled mail with broker extra returns the consult tool."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch)
    tools = build_mail_tools(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "consult_mail_board"


# ---------------------------------------------------------------------------
# MailClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_sends_nl_to_board_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that consult sends a natural-language request to the board manager."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "done"}))
    client = MailClient(_settings(agent_id="robotsix-chat"))
    out = await client.consult("list open tickets")

    assert out == "done"
    assert captured["agent_id"] == "robotsix-chat"
    assert captured["recipient"] == "board-manager-robotsix-auto-mail"
    assert captured["payload"] == {"message": "list open tickets"}
    assert captured["broker_token"] == "tok"  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_consult_blank_request_skips_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return an error for blank requests without contacting the broker."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "x"}))
    client = MailClient(_settings())
    out = await client.consult("   ")
    assert "No request" in out
    assert "payload" not in captured  # never contacted the broker


@pytest.mark.asyncio
async def test_consult_never_raises_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that transport errors degrade to a message instead of raising."""
    _install_fake_agent_comm(monkeypatch, raise_exc=RuntimeError("broker down"))
    client = MailClient(_settings())
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
    client = MailClient(_settings())
    out = await client.consult("do a thing")
    assert "nope" in out

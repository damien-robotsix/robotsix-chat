"""Tests for the mail integration — :func:`build_mail_tools` and ``MailClient``.

robotsix-agent-comm is faked via ``sys.modules`` so these run without the
``broker`` extra installed and never touch a real broker.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from robotsix_chat.config import MailSettings
from robotsix_chat.mail import build_mail_tools

from ..conftest import _install_fake_agent_comm


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
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify that missing broker extra returns no tools and emits a warning."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with caplog.at_level(logging.WARNING):
        tools = build_mail_tools(_settings())
    assert tools == []
    assert "mail.enabled is true but the 'broker' extra" in caplog.text


def test_build_mail_tools_returns_consult_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that enabled mail with broker extra returns the consult_mail tool."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch)
    tools = build_mail_tools(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "consult_mail"

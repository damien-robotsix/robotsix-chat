"""Tests for the notification integration.

:func:`build_notification_tools` and :func:`load_notification_skill`, using
a real :class:`EventBus` or a spy implementing :class:`EventSink` instead of
HTTP mocking.
"""

from __future__ import annotations

from typing import Any

import pytest

from robotsix_chat.chat.events import EventBus, EventSink
from robotsix_chat.config import NotificationSettings
from robotsix_chat.notification import (
    build_notification_tools,
    load_notification_skill,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(**kw: Any) -> NotificationSettings:
    base: dict[str, Any] = {"enabled": True}
    base.update(kw)
    return NotificationSettings(**base)


class SpySink:
    """An :class:`EventSink` spy that records every published frame."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def publish(self, session_id: str, frame: dict[str, object]) -> None:
        self.calls.append((session_id, dict(frame)))


# ---------------------------------------------------------------------------
# load_notification_skill
# ---------------------------------------------------------------------------


def test_load_notification_skill_returns_non_empty() -> None:
    """The bundled skill.md is readable and non-empty."""
    skill = load_notification_skill()
    assert len(skill) > 0
    assert "notify_user" in skill


# ---------------------------------------------------------------------------
# build_notification_tools — disabled / enabled
# ---------------------------------------------------------------------------


def test_build_disabled_returns_empty() -> None:
    """Disabled notification returns no tools."""
    tools = build_notification_tools(
        settings=NotificationSettings(enabled=False),
        event_sink=EventBus(),
        session_id="sess-1",
    )
    assert tools == []


def test_build_enabled_returns_one_tool() -> None:
    """Enabled notification returns a single callable named notify_user."""
    tools = build_notification_tools(
        _settings(),
        event_sink=EventBus(),
        session_id="sess-1",
    )
    assert len(tools) == 1
    assert tools[0].__name__ == "notify_user"


# ---------------------------------------------------------------------------
# notify_user — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_user_success() -> None:
    """Calling notify_user returns 'Notification sent.' and publishes the
    expected frame on the EventSink."""
    spy = SpySink()
    session_id = "sess-1"

    tools = build_notification_tools(
        _settings(), event_sink=spy, session_id=session_id
    )
    result = await tools[0](title="Test", body="Test body")

    assert result == "Notification sent."
    assert len(spy.calls) == 1
    sid, frame = spy.calls[0]
    assert sid == session_id
    assert frame["type"] == "notification"
    assert frame["title"] == "Test"
    assert frame["body"] == "Test body"
    assert frame["urgency"] == "default"
    assert frame["link"] == ""


@pytest.mark.asyncio
async def test_notify_user_with_link() -> None:
    """A notification with a link includes the link in the published frame."""
    spy = SpySink()
    session_id = "sess-2"

    tools = build_notification_tools(
        _settings(), event_sink=spy, session_id=session_id
    )
    await tools[0](
        title="PR merged",
        body="PR #42 was merged.",
        urgency="default",
        link="https://github.com/org/repo/pull/42",
    )

    assert len(spy.calls) == 1
    _, frame = spy.calls[0]
    assert frame["link"] == "https://github.com/org/repo/pull/42"
    assert frame["type"] == "notification"
    assert frame["title"] == "PR merged"


@pytest.mark.asyncio
async def test_notify_user_urgency_high() -> None:
    """High urgency maps correctly in the published frame."""
    spy = SpySink()

    tools = build_notification_tools(_settings(), event_sink=spy, session_id="s")
    await tools[0](title="Urgent", body="Something needs attention", urgency="high")

    _, frame = spy.calls[0]
    assert frame["urgency"] == "high"


@pytest.mark.asyncio
async def test_notify_user_urgency_low() -> None:
    """Low urgency maps correctly in the published frame."""
    spy = SpySink()

    tools = build_notification_tools(_settings(), event_sink=spy, session_id="s")
    await tools[0](title="Routine", body="Routine check completed", urgency="low")

    _, frame = spy.calls[0]
    assert frame["urgency"] == "low"


@pytest.mark.asyncio
async def test_notify_user_invalid_urgency_falls_back() -> None:
    """An invalid urgency value falls back to 'default' in the frame."""
    spy = SpySink()

    tools = build_notification_tools(_settings(), event_sink=spy, session_id="s")
    await tools[0](
        title="Test",
        body="Test",
        urgency="critical",  # invalid — should fall back
    )

    _, frame = spy.calls[0]
    assert frame["urgency"] == "default"


# ---------------------------------------------------------------------------
# notify_user — EventBus integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_user_publishes_to_correct_session() -> None:
    """The frame is published to the correct session_id."""
    spy = SpySink()

    tools_a = build_notification_tools(_settings(), event_sink=spy, session_id="a")
    tools_b = build_notification_tools(_settings(), event_sink=spy, session_id="b")

    await tools_a[0](title="For A", body="msg")
    await tools_b[0](title="For B", body="msg")

    assert len(spy.calls) == 2
    sid_a, frame_a = spy.calls[0]
    sid_b, frame_b = spy.calls[1]
    assert sid_a == "a"
    assert frame_a["title"] == "For A"
    assert sid_b == "b"
    assert frame_b["title"] == "For B"


@pytest.mark.asyncio
async def test_notify_user_no_subscribers_returns_success() -> None:
    """When no client is subscribed to the EventBus session, the tool still
    returns 'Notification sent.' (silent drop — design trade-off)."""
    bus = EventBus()
    session_id = "sess-no-sub"

    tools = build_notification_tools(_settings(), event_sink=bus, session_id=session_id)
    result = await tools[0](title="Test", body="Body")

    assert result == "Notification sent."


@pytest.mark.asyncio
async def test_notify_user_eventbus_delivers_to_subscriber() -> None:
    """A subscriber on the EventBus receives the notification frame."""
    bus = EventBus()
    session_id = "sess-sub"

    # Subscribe a queue BEFORE publishing.
    queue = bus.subscribe(session_id)

    tools = build_notification_tools(_settings(), event_sink=bus, session_id=session_id)
    await tools[0](title="Hello", body="World", urgency="high", link="/ticket/1")

    # The subscriber should receive the frame.
    frame = queue.get_nowait()
    assert frame["type"] == "notification"
    assert frame["title"] == "Hello"
    assert frame["body"] == "World"
    assert frame["urgency"] == "high"
    assert frame["link"] == "/ticket/1"


@pytest.mark.asyncio
async def test_notify_user_empty_link_omitted() -> None:
    """When link is empty, it is published as an empty string (not omitted)."""
    spy = SpySink()

    tools = build_notification_tools(_settings(), event_sink=spy, session_id="s")
    await tools[0](title="T", body="B")

    _, frame = spy.calls[0]
    assert frame["link"] == ""
    assert "link" in frame  # always present, even when empty


# ---------------------------------------------------------------------------
# Config validation — no longer requires ntfy_topic
# ---------------------------------------------------------------------------


def test_settings_enabled_without_extra_fields() -> None:
    """When notification is enabled, no extra fields are required."""
    settings = NotificationSettings(enabled=True)
    assert settings.enabled is True


def test_settings_no_ntfy_fields_remain() -> None:
    """NotificationSettings has no ntfy-specific fields."""
    field_names = set(NotificationSettings.model_fields.keys())
    assert field_names == {"enabled"}
    assert "ntfy_topic" not in field_names
    assert "ntfy_token" not in field_names
    assert "ntfy_server" not in field_names
    assert "provider" not in field_names

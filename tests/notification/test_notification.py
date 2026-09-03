"""Tests for the notification integration.

:func:`build_notification_tools` and :func:`load_notification_skill`, using
a real :class:`EventBus` or a spy implementing :class:`EventSink` instead of
HTTP mocking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE, EventBus
from robotsix_chat.config import NotificationSettings
from robotsix_chat.notification import (
    build_notification_tools,
    load_notification_skill,
)
from robotsix_chat.notification.store import NotificationStore

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(**kw: Any) -> NotificationSettings:
    base: dict[str, Any] = {"enabled": True}
    base.update(kw)
    return NotificationSettings(**base)


class SpySink:
    """An :class:`EventSink` spy that records every published frame.

    ``calls`` captures session-scoped :meth:`publish` frames; ``broadcasts``
    captures session-agnostic :meth:`publish_all` frames (the path
    ``notify_user`` uses).  A spy exposes no ``total_subscriber_count`` so
    notifications persist undelivered, mirroring a live drop when nothing is
    connected.
    """

    def __init__(self) -> None:
        """Initialize the spy with empty call logs."""
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.broadcasts: list[dict[str, object]] = []

    def publish(self, session_id: str, frame: dict[str, object]) -> None:
        """Record the published frame along with the session id."""
        self.calls.append((session_id, dict(frame)))

    def publish_all(self, frame: dict[str, object]) -> None:
        """Record a broadcast frame (delivered to every connected client)."""
        self.broadcasts.append(dict(frame))


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
    """notify_user returns success and publishes the expected frame."""
    spy = SpySink()
    session_id = "sess-1"

    tools = build_notification_tools(_settings(), event_sink=spy, session_id=session_id)
    result = await tools[0](title="Test", body="Test body")

    assert result == "Notification sent."
    assert len(spy.broadcasts) == 1
    frame = spy.broadcasts[0]
    assert frame["type"] == SSE_NOTIFICATION_TYPE
    assert frame["title"] == "Test"
    assert frame["body"] == "Test body"
    assert frame["urgency"] == "default"
    assert frame["link"] == ""


@pytest.mark.asyncio
async def test_notify_user_with_link() -> None:
    """A notification with a link includes the link in the published frame."""
    spy = SpySink()
    session_id = "sess-2"

    tools = build_notification_tools(_settings(), event_sink=spy, session_id=session_id)
    await tools[0](
        title="PR merged",
        body="PR #42 was merged.",
        urgency="default",
        link="https://github.com/org/repo/pull/42",
    )

    assert len(spy.broadcasts) == 1
    frame = spy.broadcasts[0]
    assert frame["link"] == "https://github.com/org/repo/pull/42"
    assert frame["type"] == SSE_NOTIFICATION_TYPE
    assert frame["title"] == "PR merged"


@pytest.mark.asyncio
async def test_notify_user_urgency_high() -> None:
    """High urgency maps correctly in the published frame."""
    spy = SpySink()

    tools = build_notification_tools(_settings(), event_sink=spy, session_id="s")
    await tools[0](title="Urgent", body="Something needs attention", urgency="high")

    frame = spy.broadcasts[0]
    assert frame["urgency"] == "high"


@pytest.mark.asyncio
async def test_notify_user_urgency_low() -> None:
    """Low urgency maps correctly in the published frame."""
    spy = SpySink()

    tools = build_notification_tools(_settings(), event_sink=spy, session_id="s")
    await tools[0](title="Routine", body="Routine check completed", urgency="low")

    frame = spy.broadcasts[0]
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

    frame = spy.broadcasts[0]
    assert frame["urgency"] == "default"


# ---------------------------------------------------------------------------
# notify_user — EventBus integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_user_broadcasts_regardless_of_source_session() -> None:
    """Each notify_user call broadcasts its frame (not scoped to a session)."""
    spy = SpySink()

    tools_a = build_notification_tools(_settings(), event_sink=spy, session_id="a")
    tools_b = build_notification_tools(_settings(), event_sink=spy, session_id="b")

    await tools_a[0](title="For A", body="msg")
    await tools_b[0](title="For B", body="msg")

    # Broadcast path is used — never the session-scoped publish.
    assert spy.calls == []
    assert len(spy.broadcasts) == 2
    assert spy.broadcasts[0]["title"] == "For A"
    assert spy.broadcasts[1]["title"] == "For B"


@pytest.mark.asyncio
async def test_notify_user_no_subscribers_returns_success() -> None:
    """notify_user still returns success when no subscriber is connected."""
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
    assert frame["type"] == SSE_NOTIFICATION_TYPE
    assert frame["title"] == "Hello"
    assert frame["body"] == "World"
    assert frame["urgency"] == "high"
    assert frame["link"] == "/ticket/1"


@pytest.mark.asyncio
async def test_notify_user_from_periodic_session_reaches_other_session() -> None:
    """A periodic-session notify_user reaches a browser on a DIFFERENT session.

    This mirrors the real bug: escalations fire from a background/periodic
    session id nobody watches, while the browser is subscribed only to the
    session it currently views.  Session-scoped delivery dropped these
    silently; the broadcast path must reach the connected client.
    """
    bus = EventBus()

    # The browser is viewing session "user-visible"; the escalation fires
    # from the "periodic-board-drain" session the UI never subscribes to.
    viewer_queue = bus.subscribe("user-visible")

    tools = build_notification_tools(
        _settings(), event_sink=bus, session_id="periodic-board-drain"
    )
    result = await tools[0](title="Escalation", body="Decision needed", urgency="high")

    assert result == "Notification sent."
    frame = viewer_queue.get_nowait()
    assert frame["type"] == SSE_NOTIFICATION_TYPE
    assert frame["title"] == "Escalation"
    assert frame["urgency"] == "high"


@pytest.mark.asyncio
async def test_notify_user_persists_undelivered_when_no_subscriber(
    tmp_path: Path,
) -> None:
    """With no browser connected, notify_user stores one undelivered record."""
    bus = EventBus()
    session_id = "sess-no-sub"
    store = NotificationStore(tmp_path / "notifications.json")

    tools = build_notification_tools(
        _settings(), event_sink=bus, session_id=session_id, store=store
    )
    result = await tools[0](title="Missed", body="No browser was listening")

    assert result == "Notification sent."
    records = store.list()
    assert len(records) == 1
    record = records[0]
    assert record.delivered is False
    assert record.read is False
    assert record.title == "Missed"
    assert record.body == "No browser was listening"
    assert record.source_session == session_id


@pytest.mark.asyncio
async def test_notify_user_does_not_persist_when_store_and_forward_disabled(
    tmp_path: Path,
) -> None:
    """With the feature flag off, notify_user publishes live but never persists."""
    bus = EventBus()
    session_id = "sess-flag-off"
    store = NotificationStore(tmp_path / "notifications.json")

    tools = build_notification_tools(
        _settings(store_and_forward=False),
        event_sink=bus,
        session_id=session_id,
        store=store,
    )
    # A live subscriber connects so we can assert the SSE frame is unchanged.
    queue = bus.subscribe(session_id)
    result = await tools[0](title="Live", body="Only", urgency="high", link="/x")

    assert result == "Notification sent."
    # Live SSE contract is preserved.
    frame = queue.get_nowait()
    assert frame == {
        "type": SSE_NOTIFICATION_TYPE,
        "title": "Live",
        "body": "Only",
        "urgency": "high",
        "link": "/x",
    }
    # Nothing was persisted to the store.
    assert store.list() == []


@pytest.mark.asyncio
async def test_notify_user_persists_delivered_when_subscriber_connected(
    tmp_path: Path,
) -> None:
    """With a browser connected, the record is delivered and SSE unchanged."""
    bus = EventBus()
    session_id = "sess-sub"
    store = NotificationStore(tmp_path / "notifications.json")

    # Subscribe a queue BEFORE publishing — a live connected browser.
    queue = bus.subscribe(session_id)

    tools = build_notification_tools(
        _settings(), event_sink=bus, session_id=session_id, store=store
    )
    await tools[0](title="Hello", body="World", urgency="high", link="/ticket/1")

    # Live SSE frame is unchanged — the exact same shape as before.
    frame = queue.get_nowait()
    assert frame == {
        "type": SSE_NOTIFICATION_TYPE,
        "title": "Hello",
        "body": "World",
        "urgency": "high",
        "link": "/ticket/1",
    }

    # The persisted record is marked delivered (no replay later).
    records = store.list()
    assert len(records) == 1
    record = records[0]
    assert record.delivered is True
    assert record.read is False
    assert record.title == "Hello"
    assert record.body == "World"
    assert record.source_session == session_id


@pytest.mark.asyncio
async def test_notify_user_persist_failure_does_not_break_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store failure is logged; the live publish still succeeds."""
    spy = SpySink()
    session_id = "sess-1"
    store = NotificationStore(tmp_path / "notifications.json")

    def _boom(**kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "append", _boom)

    tools = build_notification_tools(
        _settings(), event_sink=spy, session_id=session_id, store=store
    )
    result = await tools[0](title="Test", body="Body")

    assert result == "Notification sent."
    assert len(spy.broadcasts) == 1
    frame = spy.broadcasts[0]
    assert frame["type"] == SSE_NOTIFICATION_TYPE
    assert frame["title"] == "Test"


@pytest.mark.asyncio
async def test_notify_user_empty_link_omitted() -> None:
    """When link is empty, it is published as an empty string (not omitted)."""
    spy = SpySink()

    tools = build_notification_tools(_settings(), event_sink=spy, session_id="s")
    await tools[0](title="T", body="B")

    frame = spy.broadcasts[0]
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
    assert field_names == {
        "enabled",
        "store_and_forward",
        "store_path",
        "max_events",
        "retention_days",
        "read_retention_days",
    }
    assert "ntfy_topic" not in field_names
    assert "ntfy_token" not in field_names
    assert "ntfy_server" not in field_names
    assert "provider" not in field_names

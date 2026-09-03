"""End-to-end acceptance tests for the notification store-and-forward path.

These tie the three collaborating pieces together — the real ``notify_user``
tool (:func:`build_notification_tools`), the persistent
:class:`NotificationStore`, and the ``GET /events`` SSE replay endpoint — into
the exact scenario the store-and-forward feature exists to fix:

    1. ``notify_user`` fires while **no browser is connected** for the session.
    2. A browser connects to ``/events`` some time later.
    3. The missed notification is replayed with the **same event shape** a live
       ``notify_user`` frame carries, and its ``delivered`` flag is flipped so it
       is never replayed twice.

The headline test (:func:`test_store_and_forward_end_to_end_matches_live_frame`)
compares the replayed frame against a frame captured from a genuinely live
``notify_user`` call rather than a hand-written literal, so the two paths can
never silently diverge.  :func:`test_notify_user_without_store_is_dropped`
pins down the *original* silent-drop bug the store closes, making this file an
explicit regression guard.

Per the ticket: browser-native Notification permissions are **not** tested here
— that is a pure DOM concern.  These tests exercise the server-side delivery
guarantee only.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from starlette.responses import StreamingResponse

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE, EventBus
from robotsix_chat.chat.server.routes.events import events_endpoint
from robotsix_chat.config import NotificationSettings
from robotsix_chat.notification import build_notification_tools
from robotsix_chat.notification.store import NotificationStore
from tests.chat.server.routes.test_events import _make_request, _parse_sse_bytes
from tests.conftest import mock_app

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(**kw: object) -> NotificationSettings:
    """Build enabled :class:`NotificationSettings` with optional overrides."""
    base: dict[str, object] = {"enabled": True}
    base.update(kw)
    return NotificationSettings(**base)  # type: ignore[arg-type]


async def _capture_live_frame(title: str, body: str) -> dict[str, object]:
    """Return the frame a live ``notify_user`` emits to a *connected* browser.

    A queue is subscribed to a fresh :class:`EventBus` *before* the tool fires,
    so the tool sees an active subscriber and publishes the live SSE frame we
    can compare the store-and-forward replay against.
    """
    bus = EventBus()
    session_id = "live"
    queue = bus.subscribe(session_id)
    tools = build_notification_tools(_settings(), event_sink=bus, session_id=session_id)
    await tools[0](title=title, body=body)
    return dict(queue.get_nowait())


# ---------------------------------------------------------------------------
# end-to-end acceptance path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_and_forward_end_to_end_matches_live_frame(tmp_path: Path) -> None:
    """notify_user with no browser → SSE replay carries the live frame shape.

    This is the acceptance path: the notification is published while no client
    is subscribed (the silent-drop scenario), persisted undelivered, then
    replayed byte-for-byte identically to a live ``notify_user`` frame when a
    browser connects, and finally marked delivered.
    """
    store = NotificationStore(tmp_path / "notifications.json")
    session_id = "sess-e2e"

    async with mock_app(notification_store=store) as f:
        # No browser is subscribed to this session on the app's own EventBus —
        # exactly the condition that used to drop notifications on the floor.
        tools = build_notification_tools(
            _settings(),
            event_sink=f.app.state.event_bus,
            session_id=session_id,
            store=store,
        )
        result = await tools[0](title="Missed", body="No browser was listening")
        assert result == "Notification sent."

        # It was NOT dropped: it is persisted, awaiting an undelivered replay.
        pending = store.list()
        assert len(pending) == 1
        assert pending[0].delivered is False

        # A browser now connects to the SSE channel for the same session.
        request = _make_request(f.app, session_id=session_id)
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        try:
            heartbeat = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert heartbeat == b": keepalive\n\n"
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            replayed = _parse_sse_bytes(chunk)
        finally:
            await body_iter.aclose()

    assert len(replayed) == 1
    replayed_frame = replayed[0]

    # The replayed frame carries the SAME type and payload fields a *live*
    # notify_user event would (default urgency / empty link for these inputs),
    # captured from a real notify_user call rather than a hand-written literal.
    live_frame = await _capture_live_frame("Missed", "No browser was listening")
    assert replayed_frame == live_frame
    assert replayed_frame["type"] == SSE_NOTIFICATION_TYPE
    assert replayed_frame["title"] == "Missed"
    assert replayed_frame["body"] == "No browser was listening"

    # Delivered state was updated so the notification is never replayed twice.
    assert all(r.delivered for r in store.list())


@pytest.mark.asyncio
async def test_store_and_forward_survives_store_reopen(tmp_path: Path) -> None:
    """A missed notification survives a process restart (store reopen).

    Guards the durability half of store-and-forward: the record persisted
    while disconnected is still replayable after the store is reconstructed
    from disk (as it would be after a container restart).
    """
    path = tmp_path / "notifications.json"
    session_id = "sess-restart"

    # First process: publish with no subscriber, then "crash" (drop the store).
    store = NotificationStore(path)
    tools = build_notification_tools(
        _settings(),
        event_sink=EventBus(),
        session_id=session_id,
        store=store,
    )
    await tools[0](title="Persisted", body="across a restart")
    assert [r.delivered for r in store.list()] == [False]

    # Second process: a fresh store over the same file still has the record,
    # and the SSE endpoint replays it on connect.
    reopened = NotificationStore(path)
    async with mock_app(notification_store=reopened) as f:
        request = _make_request(f.app, session_id=session_id)
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        try:
            heartbeat = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert heartbeat == b": keepalive\n\n"
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            replayed = _parse_sse_bytes(chunk)
        finally:
            await body_iter.aclose()

    assert [fr["title"] for fr in replayed] == ["Persisted"]
    assert all(r.delivered for r in reopened.list())


# ---------------------------------------------------------------------------
# regression guard — the original silent-drop bug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_user_without_store_is_dropped(tmp_path: Path) -> None:
    """Legacy behaviour: with no store wired, a disconnected notify_user is lost.

    This documents the *original* silent-drop bug the store-and-forward path
    fixes — with no persistent store, a notification fired while no browser is
    connected is dropped and a later connect replays nothing.  The store-backed
    tests above are the regression guard that this can no longer happen once a
    store is wired.
    """
    session_id = "sess-legacy"

    async with mock_app() as f:  # no notification_store wired into app.state
        tools = build_notification_tools(
            _settings(),
            event_sink=f.app.state.event_bus,
            session_id=session_id,
            store=None,
        )
        result = await tools[0](title="Lost", body="no store, no browser")
        assert result == "Notification sent."

        # A browser connects afterwards: only the heartbeat arrives — the
        # notification was dropped because nothing persisted it.
        request = _make_request(f.app, session_id=session_id)
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        try:
            heartbeat = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert heartbeat == b": keepalive\n\n"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(body_iter.__anext__(), timeout=0.5)
        finally:
            await body_iter.aclose()


@pytest.mark.asyncio
async def test_store_and_forward_disabled_drops_like_legacy(tmp_path: Path) -> None:
    """With the feature flag off, a store is wired but nothing is persisted.

    The emergency-disable flag must fully revert to legacy silent-drop
    behaviour: even with a store present, ``store_and_forward=False`` persists
    nothing, so a later connect replays nothing.
    """
    store = NotificationStore(tmp_path / "notifications.json")
    session_id = "sess-flag-off"

    async with mock_app(
        notification_store=store, notification_store_and_forward=False
    ) as f:
        tools = build_notification_tools(
            _settings(store_and_forward=False),
            event_sink=f.app.state.event_bus,
            session_id=session_id,
            store=store,
        )
        await tools[0](title="Disabled", body="flag off")

        # Nothing persisted — the flag reverted to legacy behaviour.
        assert store.list() == []

        request = _make_request(f.app, session_id=session_id)
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        try:
            heartbeat = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert heartbeat == b": keepalive\n\n"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(body_iter.__anext__(), timeout=0.5)
        finally:
            await body_iter.aclose()

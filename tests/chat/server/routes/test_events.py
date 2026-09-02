"""Tests for the ``/events`` SSE endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE
from robotsix_chat.chat.server.routes.constants import SSE_CONTENT_TYPE
from robotsix_chat.chat.server.routes.events import events_endpoint
from robotsix_chat.notification.store import NotificationStore
from tests.conftest import mock_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    app: object,
    *,
    session_id: str | None = None,
    client_id: str | None = None,
) -> Request:
    """Build a minimal Starlette ``Request`` for ``GET /events``."""
    params: list[str] = []
    if session_id is not None:
        params.append(f"session_id={session_id}")
    if client_id is not None:
        params.append(f"client_id={client_id}")
    query_string = "&".join(params).encode()

    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/events",
        "query_string": query_string,
        "headers": [],
        "app": app,
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    return Request(scope, receive)


def _days_ago(days: float) -> str:
    """Return an ISO-8601 UTC timestamp *days* days in the past (within retention)."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _parse_sse_bytes(data: bytes) -> list[dict[str, object]]:
    """Split SSE byte content into parsed JSON frames from ``data:`` lines."""
    text = data.decode()
    events = [e for e in text.split("\n\n") if e]
    frames: list[dict[str, object]] = []
    for e in events:
        if e.startswith("data: "):
            frames.append(json.loads(e[len("data: ") :]))
    return frames


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_endpoint_returns_sse_content_type() -> None:
    """``GET /events?session_id=...`` returns ``text/event-stream``."""
    async with mock_app() as f:
        request = _make_request(f.app, session_id="s1")
        response = await events_endpoint(request)

        assert isinstance(response, StreamingResponse)
        assert response.media_type == SSE_CONTENT_TYPE
        assert response.headers["Content-Type"] == SSE_CONTENT_TYPE

        # Clean up the stream
        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        await body_iter.aclose()


@pytest.mark.asyncio
async def test_events_endpoint_sends_heartbeat_first() -> None:
    """The first SSE frame is the ``: keepalive`` heartbeat comment."""
    async with mock_app() as f:
        request = _make_request(f.app, session_id="s1")
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        try:
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert chunk == b": keepalive\n\n"
        finally:
            await body_iter.aclose()


@pytest.mark.asyncio
async def test_events_endpoint_delivers_published_frame() -> None:
    """A frame published to the EventBus is delivered over the SSE stream."""
    async with mock_app() as f:
        request = _make_request(f.app, session_id="s1")
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        try:
            # Consume the heartbeat
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert chunk == b": keepalive\n\n"

            # Publish a frame
            f.app.state.event_bus.publish("s1", {"type": "test", "payload": "hello"})

            # Read the data frame
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            frames = _parse_sse_bytes(chunk)
            assert frames == [{"type": "test", "payload": "hello"}]
        finally:
            await body_iter.aclose()


@pytest.mark.asyncio
async def test_events_endpoint_unsubscribes_on_disconnect() -> None:
    """Closing the SSE connection removes the subscriber from the EventBus."""
    async with mock_app() as f:
        request = _make_request(f.app, session_id="s1")
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        # Consume the heartbeat so we know subscription happened
        chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
        assert chunk == b": keepalive\n\n"

        # Verify subscriber is registered
        assert "s1" in f.app.state.event_bus._subscribers
        assert len(f.app.state.event_bus._subscribers["s1"]) == 1

        # Simulate disconnect by closing the body iterator
        await body_iter.aclose()
        await asyncio.sleep(0)  # let the finally block run

    # After cleanup, the subscriber set is gone
    assert "s1" not in f.app.state.event_bus._subscribers


@pytest.mark.asyncio
async def test_events_endpoint_replays_undelivered_notifications(
    tmp_path: object,
) -> None:
    """Connecting replays undelivered notifications oldest-first, then marks them.

    Mirrors the acceptance path: ``notify_user`` persisted notifications while
    no client was connected; on connect the client receives each missed
    notification with the same event shape as a live ``notify_user`` frame,
    and the records are flipped to ``delivered=true``.
    """
    store = NotificationStore(tmp_path / "notifications.json")  # type: ignore[operator]
    store.append(title="first", body="b1", source_session="s1", ts=_days_ago(2))
    store.append(title="second", body="b2", source_session="s1", ts=_days_ago(1))

    async with mock_app(notification_store=store) as f:
        request = _make_request(f.app, session_id="s1")
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        try:
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert chunk == b": keepalive\n\n"

            # Oldest-first: "first" then "second", each in the live frame shape.
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert _parse_sse_bytes(chunk) == [
                {
                    "type": SSE_NOTIFICATION_TYPE,
                    "title": "first",
                    "body": "b1",
                    "urgency": "default",
                    "link": "",
                }
            ]
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert _parse_sse_bytes(chunk) == [
                {
                    "type": SSE_NOTIFICATION_TYPE,
                    "title": "second",
                    "body": "b2",
                    "urgency": "default",
                    "link": "",
                }
            ]
        finally:
            await body_iter.aclose()

    # Both records are now delivered — no record remains undelivered.
    assert all(r.delivered for r in store.list())


@pytest.mark.asyncio
async def test_events_endpoint_second_connect_does_not_replay(tmp_path: object) -> None:
    """A second connect does not replay notifications delivered on the first."""
    store = NotificationStore(tmp_path / "notifications.json")  # type: ignore[operator]
    store.append(title="once", body="b", source_session="s1", ts=_days_ago(1))

    async with mock_app(notification_store=store) as f:
        # First connect: drain the replayed notification.
        request = _make_request(f.app, session_id="s1")
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)
        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
        assert chunk == b": keepalive\n\n"
        chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
        assert _parse_sse_bytes(chunk)[0]["title"] == "once"
        await body_iter.aclose()

        # Second connect: heartbeat only, then no further frame (nothing to replay).
        request2 = _make_request(f.app, session_id="s1")
        response2 = await events_endpoint(request2)
        assert isinstance(response2, StreamingResponse)
        body_iter2: AsyncGenerator[bytes] = response2.body_iterator  # type: ignore[assignment]
        try:
            chunk = await asyncio.wait_for(body_iter2.__anext__(), timeout=2.0)
            assert chunk == b": keepalive\n\n"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(body_iter2.__anext__(), timeout=0.5)
        finally:
            await body_iter2.aclose()


@pytest.mark.asyncio
async def test_events_endpoint_skips_already_delivered_notifications(
    tmp_path: object,
) -> None:
    """Records already ``delivered=true`` are not replayed on connect."""
    store = NotificationStore(tmp_path / "notifications.json")  # type: ignore[operator]
    delivered = store.append(
        title="old", body="b", source_session="s1", ts=_days_ago(1)
    )
    store.mark_delivered([delivered.id])

    async with mock_app(notification_store=store) as f:
        request = _make_request(f.app, session_id="s1")
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)
        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        try:
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert chunk == b": keepalive\n\n"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(body_iter.__anext__(), timeout=0.5)
        finally:
            await body_iter.aclose()


@pytest.mark.asyncio
async def test_events_endpoint_legacy_client_id() -> None:
    """``GET /events?client_id=...`` (without session_id) delivers frames."""
    async with mock_app() as f:
        request = _make_request(f.app, client_id="s2")
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        try:
            # Consume the heartbeat
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert chunk == b": keepalive\n\n"

            # Publish to s2 (the legacy client_id value)
            f.app.state.event_bus.publish(
                "s2", {"type": "legacy_test", "payload": "works"}
            )

            # Read the data frame
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            frames = _parse_sse_bytes(chunk)
            assert frames == [{"type": "legacy_test", "payload": "works"}]
        finally:
            await body_iter.aclose()

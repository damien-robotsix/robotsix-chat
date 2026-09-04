"""Tests for the ``/events`` SSE endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from robotsix_chat.chat.server.routes.constants import SSE_CONTENT_TYPE
from robotsix_chat.chat.server.routes.events import events_endpoint
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

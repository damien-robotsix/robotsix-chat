"""Tests for the subsession SSE frame builders, EventBus, and /events endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from robotsix_chat.chat.events import (
    SSE_ACTIVITY_TYPE,
    SSE_SUBSESSION_CLOSED_TYPE,
    SSE_SUBSESSION_FAILED_TYPE,
    SSE_SUBSESSION_MESSAGE_TYPE,
    SSE_SUBSESSION_RESULT_TYPE,
    SSE_SUBSESSION_STARTED_TYPE,
    SSE_SUBSESSION_UPDATED_TYPE,
    EventBus,
    activity_frame,
    subsession_closed_frame,
    subsession_failed_frame,
    subsession_message_frame,
    subsession_result_frame,
    subsession_started_frame,
    subsession_updated_frame,
)
from robotsix_chat.chat.server import SSE_CONTENT_TYPE, events_endpoint
from tests.conftest import mock_app

# ---------------------------------------------------------------------------
# Frame-builder helpers — unit tests
# ---------------------------------------------------------------------------


def test_subsession_started_frame_shape() -> None:
    """``subsession_started_frame`` merges the snapshot under the started type."""
    snapshot: dict[str, object] = {
        "subsession_id": "sub-1",
        "kind": "task",
        "owner_session_id": "sess-1",
        "parent_id": None,
        "depth": 1,
        "title": "summarise",
        "status": "running",
    }
    frame = subsession_started_frame(snapshot)
    assert frame == {"type": SSE_SUBSESSION_STARTED_TYPE, **snapshot}


def test_subsession_started_frame_does_not_mutate_snapshot() -> None:
    """The builder returns a new dict — the input snapshot gains no keys."""
    snapshot: dict[str, object] = {"subsession_id": "sub-1"}
    frame = subsession_started_frame(snapshot)
    assert "type" not in snapshot
    assert frame is not snapshot


def test_subsession_updated_frame_shape() -> None:
    """``subsession_updated_frame`` returns the documented JSON shape."""
    frame = subsession_updated_frame(
        "sub-1",
        "sleeping",
        runs=3,
        next_run_at=100.5,
        last_activity_at=90.0,
        last_result="all good",
    )
    assert frame == {
        "type": SSE_SUBSESSION_UPDATED_TYPE,
        "subsession_id": "sub-1",
        "status": "sleeping",
        "runs": 3,
        "next_run_at": 100.5,
        "last_activity_at": 90.0,
        "last_result": "all good",
    }


def test_subsession_updated_frame_defaults() -> None:
    """Optional keyword fields default to zero/None but stay present."""
    frame = subsession_updated_frame("sub-2", "running")
    assert frame == {
        "type": SSE_SUBSESSION_UPDATED_TYPE,
        "subsession_id": "sub-2",
        "status": "running",
        "runs": 0,
        "next_run_at": None,
        "last_activity_at": None,
        "last_result": None,
    }


def test_subsession_message_frame_shape() -> None:
    """``subsession_message_frame`` returns the documented JSON shape."""
    frame = subsession_message_frame("sub-1", "assistant", "hello", 42.0)
    assert frame == {
        "type": SSE_SUBSESSION_MESSAGE_TYPE,
        "subsession_id": "sub-1",
        "role": "assistant",
        "text": "hello",
        "timestamp": 42.0,
    }


def test_subsession_result_frame_shape() -> None:
    """``subsession_result_frame`` returns the documented JSON shape."""
    frame = subsession_result_frame(
        "sub-1", "periodic", "watch CI", 2, "build is green", "parent-1"
    )
    assert frame == {
        "type": SSE_SUBSESSION_RESULT_TYPE,
        "subsession_id": "sub-1",
        "kind": "periodic",
        "title": "watch CI",
        "run": 2,
        "text": "build is green",
        "parent_id": "parent-1",
    }


def test_subsession_closed_frame_shape() -> None:
    """``subsession_closed_frame`` returns the documented JSON shape."""
    frame = subsession_closed_frame(
        "sub-1",
        kind="task",
        title="summarise",
        reason="completed",
        summary="all done",
        closed_by="agent",
        parent_id=None,
    )
    assert frame == {
        "type": SSE_SUBSESSION_CLOSED_TYPE,
        "subsession_id": "sub-1",
        "kind": "task",
        "title": "summarise",
        "reason": "completed",
        "summary": "all done",
        "closed_by": "agent",
        "parent_id": None,
        "status": "closed",
    }


def test_subsession_failed_frame_shape() -> None:
    """``subsession_failed_frame`` returns the documented JSON shape."""
    frame = subsession_failed_frame(
        "sub-1",
        kind="user_chat",
        title="ask about deploy",
        error="boom",
        summary="Failed: boom",
        parent_id="parent-9",
    )
    assert frame == {
        "type": SSE_SUBSESSION_FAILED_TYPE,
        "subsession_id": "sub-1",
        "kind": "user_chat",
        "title": "ask about deploy",
        "error": "boom",
        "summary": "Failed: boom",
        "parent_id": "parent-9",
        "status": "failed",
    }


def test_activity_frame_shape() -> None:
    """``activity_frame`` returns the documented JSON shape."""
    frame = activity_frame(
        "tool_call", 3, tool_name="search", detail='{"q": "x"}', is_error=False
    )
    assert frame == {
        "type": SSE_ACTIVITY_TYPE,
        "kind": "tool_call",
        "turn": 3,
        "tool_name": "search",
        "detail": '{"q": "x"}',
        "is_error": False,
    }


def test_activity_frame_defaults() -> None:
    """tool_name/detail/is_error default to None/""/False.

    (thinking and text kinds carry no tool_name.)
    """
    frame = activity_frame("thinking", 1)
    assert frame == {
        "type": SSE_ACTIVITY_TYPE,
        "kind": "thinking",
        "turn": 1,
        "tool_name": None,
        "detail": "",
        "is_error": False,
    }


# ---------------------------------------------------------------------------
# EventBus — unit tests
# ---------------------------------------------------------------------------


def test_event_bus_publish_to_subscriber() -> None:
    """A frame published to a subscribed client is delivered to its queue."""
    bus = EventBus()
    q = bus.subscribe("c1")

    bus.publish("c1", {"type": "test", "data": "hello"})

    assert q.get_nowait() == {"type": "test", "data": "hello"}


def test_event_bus_publish_no_listener_is_silent_noop() -> None:
    """Publishing to a client with no subscriber drops the frame — no error."""
    bus = EventBus()
    # Should not raise
    bus.publish("nobody", {"type": "test"})

    assert "nobody" not in bus._subscribers


def test_event_bus_multiple_subscribers_same_client() -> None:
    """Two queues for the same client both receive each published frame."""
    bus = EventBus()
    q1 = bus.subscribe("c1")
    q2 = bus.subscribe("c1")

    bus.publish("c1", {"type": "x"})

    assert q1.get_nowait() == {"type": "x"}
    assert q2.get_nowait() == {"type": "x"}


def test_event_bus_subscriber_isolation() -> None:
    """Frames published to one client are not delivered to another."""
    bus = EventBus()
    q_a = bus.subscribe("client-a")
    q_b = bus.subscribe("client-b")

    bus.publish("client-a", {"type": "for-a"})

    assert q_a.get_nowait() == {"type": "for-a"}
    assert q_b.empty()


def test_event_bus_unsubscribe_removes_queue() -> None:
    """After unsubscribe the queue no longer receives frames."""
    bus = EventBus()
    q = bus.subscribe("c1")

    bus.unsubscribe("c1", q)
    bus.publish("c1", {"type": "after"})

    assert q.empty()
    assert "c1" not in bus._subscribers  # empty set is dropped


def test_event_bus_unsubscribe_unknown_queue_is_noop() -> None:
    """Unsubscribing a queue that was never subscribed does not raise."""
    bus = EventBus()
    q: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    bus.unsubscribe("c1", q)  # no-op

    assert "c1" not in bus._subscribers


def test_event_bus_unsubscribe_keeps_other_queues() -> None:
    """Unsubscribing one queue leaves the other for the same client intact."""
    bus = EventBus()
    q1 = bus.subscribe("c1")
    q2 = bus.subscribe("c1")

    bus.unsubscribe("c1", q1)

    # q2 still receives frames
    bus.publish("c1", {"type": "only-q2"})
    assert q2.get_nowait() == {"type": "only-q2"}
    assert q1.empty()
    assert "c1" in bus._subscribers  # set not empty


# ---------------------------------------------------------------------------
# /events endpoint — HTTP-level tests
# ---------------------------------------------------------------------------

# Because ``httpx.ASGITransport`` buffers the entire response body before
# making it available to the client, it cannot be used to test a persistent
# (never-ending) SSE stream.  The streaming tests below construct a Starlette
# ``Request`` directly, call ``events_endpoint``, and consume the returned
# ``StreamingResponse.body_iterator`` — exactly as the ASGI server would.
# ``body_iterator.aclose()`` simulates a client disconnect and triggers the
# generator's ``finally`` block.


def _make_request(client_id: str, app: object) -> Request:
    """Build a minimal Starlette ``Request`` for ``GET /events?session_id=...``."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/events",
        "query_string": f"session_id={client_id}".encode(),
        "headers": [],
        "app": app,
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    return Request(scope, receive)


def _parse_data_line(line: str) -> dict[str, object]:
    """Extract and parse the JSON payload from an SSE ``data:`` line."""
    assert line.startswith("data: ")
    return json.loads(line[len("data: ") :])  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_events_endpoint_missing_client_id() -> None:
    """``GET /events`` without a ``session_id`` query param returns 400."""
    async with mock_app() as f:
        response = await f.client.get("/events")

    assert response.status_code == 400
    assert response.json() == {"error": "session_id query parameter is required"}


@pytest.mark.asyncio
async def test_events_endpoint_empty_client_id() -> None:
    """``GET /events?session_id=`` (empty value) returns 400."""
    async with mock_app() as f:
        response = await f.client.get("/events", params={"session_id": ""})

    assert response.status_code == 400
    assert response.json() == {"error": "session_id query parameter is required"}


@pytest.mark.asyncio
async def test_events_endpoint_opens_with_heartbeat() -> None:
    """The persistent stream opens with a ``: keepalive`` heartbeat comment."""
    async with mock_app() as f:
        request = _make_request("c1", f.app)
        response = await events_endpoint(request)

        assert isinstance(response, StreamingResponse)
        assert response.media_type == SSE_CONTENT_TYPE
        assert response.headers["Content-Type"] == SSE_CONTENT_TYPE

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        try:
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert chunk == b": keepalive\n\n"
            # Verify subscriber was registered
            assert "c1" in f.app.state.event_bus._subscribers
        finally:
            await body_iter.aclose()


@pytest.mark.asyncio
async def test_events_endpoint_receives_pushed_frame() -> None:
    """A frame published via EventBus is delivered over the SSE stream."""
    async with mock_app() as f:
        request = _make_request("c1", f.app)
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        try:
            # Consume the heartbeat
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert chunk == b": keepalive\n\n"

            # Publish a frame
            f.app.state.event_bus.publish(
                "c1",
                subsession_message_frame("sub-1", "assistant", "hi", 1.0),
            )

            # Read the data frame
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            text = chunk.decode()
            lines = text.rstrip("\n").split("\n")
            # SSE frame: one "data:" line followed by an empty line
            data_lines = [ln for ln in lines if ln.startswith("data: ")]
            assert len(data_lines) == 1
            parsed = _parse_data_line(data_lines[0])
            assert parsed == {
                "type": SSE_SUBSESSION_MESSAGE_TYPE,
                "subsession_id": "sub-1",
                "role": "assistant",
                "text": "hi",
                "timestamp": 1.0,
            }
        finally:
            await body_iter.aclose()


@pytest.mark.asyncio
async def test_events_endpoint_unsubscribes_on_disconnect() -> None:
    """Closing the SSE connection removes the subscriber from the EventBus."""
    async with mock_app() as f:
        request = _make_request("c1", f.app)
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter: AsyncGenerator[bytes] = response.body_iterator  # type: ignore[assignment]
        # Consume the heartbeat so we know subscription happened
        chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
        assert chunk == b": keepalive\n\n"

        # Verify subscriber is registered
        assert "c1" in f.app.state.event_bus._subscribers
        assert len(f.app.state.event_bus._subscribers["c1"]) == 1

        # Simulate disconnect by closing the body iterator
        await body_iter.aclose()
        await asyncio.sleep(0)  # let the finally block run

    # After cleanup, the subscriber set is gone
    assert "c1" not in f.app.state.event_bus._subscribers

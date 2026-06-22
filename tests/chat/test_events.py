"""Tests for the persistent SSE events channel and EventBus/TaskRegistry integration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from robotsix_chat.chat.events import (
    SSE_TASK_COMPLETED_TYPE,
    SSE_TASK_FAILED_TYPE,
    SSE_TASK_STARTED_TYPE,
    EventBus,
    task_completed_frame,
    task_failed_frame,
    task_started_frame,
)
from robotsix_chat.chat.server import SSE_CONTENT_TYPE, events_endpoint
from robotsix_chat.chat.tasks import TaskRegistry, TaskStatus
from tests.conftest import mock_app

# ---------------------------------------------------------------------------
# Frame-builder helpers — unit tests
# ---------------------------------------------------------------------------


def test_task_started_frame_shape() -> None:
    """``task_started_frame`` returns the documented JSON shape."""
    frame = task_started_frame("t1", "c1", "summarise")
    assert frame == {
        "type": SSE_TASK_STARTED_TYPE,
        "task_id": "t1",
        "client_id": "c1",
        "prompt": "summarise",
        "status": "running",
    }


def test_task_completed_frame_shape() -> None:
    """``task_completed_frame`` returns the documented JSON shape."""
    frame = task_completed_frame("t1", "done")
    assert frame == {
        "type": SSE_TASK_COMPLETED_TYPE,
        "task_id": "t1",
        "status": "completed",
        "result": "done",
    }


def test_task_failed_frame_shape() -> None:
    """``task_failed_frame`` returns the documented JSON shape."""
    frame = task_failed_frame("t1", "boom")
    assert frame == {
        "type": SSE_TASK_FAILED_TYPE,
        "task_id": "t1",
        "status": "failed",
        "error": "boom",
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
    """Build a minimal Starlette ``Request`` for ``GET /events?client_id=...``."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/events",
        "query_string": f"client_id={client_id}".encode(),
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
    """``GET /events`` without a ``client_id`` query param returns 400."""
    async with mock_app() as f:
        response = await f.client.get("/events")

    assert response.status_code == 400
    assert response.json() == {"error": "client_id query parameter is required"}


@pytest.mark.asyncio
async def test_events_endpoint_empty_client_id() -> None:
    """``GET /events?client_id=`` (empty value) returns 400."""
    async with mock_app() as f:
        response = await f.client.get("/events", params={"client_id": ""})

    assert response.status_code == 400
    assert response.json() == {"error": "client_id query parameter is required"}


@pytest.mark.asyncio
async def test_events_endpoint_opens_with_heartbeat() -> None:
    """The persistent stream opens with a ``: keepalive`` heartbeat comment."""
    async with mock_app() as f:
        request = _make_request("c1", f.app)
        response = await events_endpoint(request)

        assert isinstance(response, StreamingResponse)
        assert response.media_type == SSE_CONTENT_TYPE
        assert response.headers["Content-Type"] == SSE_CONTENT_TYPE

        body_iter: AsyncGenerator[bytes, None] = response.body_iterator  # type: ignore[assignment]
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

        body_iter: AsyncGenerator[bytes, None] = response.body_iterator  # type: ignore[assignment]
        try:
            # Consume the heartbeat
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert chunk == b": keepalive\n\n"

            # Publish a frame
            f.app.state.event_bus.publish(
                "c1",
                {"type": "task_started", "task_id": "t1", "status": "running"},
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
                "type": "task_started",
                "task_id": "t1",
                "status": "running",
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

        body_iter: AsyncGenerator[bytes, None] = response.body_iterator  # type: ignore[assignment]
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


# ---------------------------------------------------------------------------
# TaskRegistry → EventBus integration (unit-level)
# ---------------------------------------------------------------------------


class _FakeCoro:
    """Stand-in for ``asyncio.Task[None]`` — no event loop required.

    Copied from ``tests/chat/test_tasks.py`` so this module stays self-contained.
    """

    def add_done_callback(self, _cb: object) -> None:
        pass

    def cancel(self, _msg: object = None) -> bool:
        return False

    def done(self) -> bool:
        return False


def _fake_coro() -> _FakeCoro:
    """Return a stand-in for ``asyncio.Task[None]`` for non-async tests."""
    return _FakeCoro()


def test_task_registry_publishes_started_frame_on_register() -> None:
    """Registering a task publishes a ``task_started`` frame to the EventBus."""
    bus = EventBus()
    reg = TaskRegistry(event_sink=bus)
    q = bus.subscribe("c1")

    tid = reg.register("c1", "do x", _fake_coro())  # type: ignore[arg-type]

    frame = q.get_nowait()
    assert frame == task_started_frame(tid, "c1", "do x")


def test_task_registry_publishes_completed_frame_on_complete() -> None:
    """Completing a task publishes a ``task_completed`` frame."""
    bus = EventBus()
    reg = TaskRegistry(event_sink=bus)
    q = bus.subscribe("c1")

    tid = reg.register("c1", "do x", _fake_coro())  # type: ignore[arg-type]
    q.get_nowait()  # consume started frame

    reg.complete(tid, "ok")

    frame = q.get_nowait()
    assert frame == task_completed_frame(tid, "ok")


def test_task_registry_publishes_failed_frame_on_fail() -> None:
    """Failing a task publishes a ``task_failed`` frame."""
    bus = EventBus()
    reg = TaskRegistry(event_sink=bus)
    q = bus.subscribe("c1")

    tid = reg.register("c1", "risky", _fake_coro())  # type: ignore[arg-type]
    q.get_nowait()  # consume started frame

    reg.fail(tid, "boom")

    frame = q.get_nowait()
    assert frame == task_failed_frame(tid, "boom")


def test_task_registry_no_event_sink_no_publish() -> None:
    """When ``event_sink`` is None, existing behaviour is unchanged — no publish."""
    # Construct with default (no event_sink)
    reg = TaskRegistry()
    tid = reg.register("c1", "do x", _fake_coro())  # type: ignore[arg-type]

    reg.complete(tid, "ok")
    reg.fail(tid, "boom")

    # The important things: no crash, and existing test_tasks.py still passes.
    info = reg.get(tid)
    assert info is not None
    assert info.status == TaskStatus.FAILED
    assert info.error == "boom"


@pytest.mark.asyncio
async def test_task_registry_integration_with_real_task() -> None:
    """End-to-end: register → complete → fail using a real asyncio.Task."""
    bus = EventBus()
    reg = TaskRegistry(event_sink=bus)
    q = bus.subscribe("c1")

    async def quick() -> None:
        pass

    task = asyncio.create_task(quick())
    tid = reg.register("c1", "quick-op", task)

    # Started frame delivered
    assert q.get_nowait() == task_started_frame(tid, "c1", "quick-op")

    reg.complete(tid, "done")
    assert q.get_nowait() == task_completed_frame(tid, "done")

    # Fail after complete overwrites
    reg.fail(tid, "oops")
    assert q.get_nowait() == task_failed_frame(tid, "oops")

    await task

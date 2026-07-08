"""SSE event-bus endpoint.

Persistent SSE channel for background-task lifecycle events.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from ._shared import _get_session_id, _sse_frame
from .constants import (
    SSE_CONTENT_TYPE,
    SSE_HEARTBEAT_FRAME,
    SSE_HEARTBEAT_INTERVAL,
)


async def events_endpoint(request: Request) -> JSONResponse | StreamingResponse:
    """Open a persistent SSE channel for background-task lifecycle events.

    ``GET /events?session_id=...`` opens a never-closing ``text/event-stream``
    that delivers ``task_started``, ``task_completed``, and ``task_failed``
    frames pushed via :class:`~robotsix_chat.chat.events.EventBus`.  Heartbeat
    comments keep the connection alive during quiet periods.

    Tolerates ``client_id`` as a legacy fallback (treated as ``session_id``).
    """
    session_id = _get_session_id(request)
    if isinstance(session_id, JSONResponse):
        return session_id

    async def event_stream() -> AsyncIterator[bytes]:
        queue = request.app.state.event_bus.subscribe(session_id)
        try:
            yield SSE_HEARTBEAT_FRAME  # first byte immediately
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), SSE_HEARTBEAT_INTERVAL)
                except TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield SSE_HEARTBEAT_FRAME
                    continue
                yield _sse_frame(frame)
        finally:
            request.app.state.event_bus.unsubscribe(session_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type=SSE_CONTENT_TYPE,
        headers={"Content-Type": SSE_CONTENT_TYPE},
    )

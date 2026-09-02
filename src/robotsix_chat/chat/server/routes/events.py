"""SSE event-bus endpoint.

Persistent SSE channel for background-task lifecycle events.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE

from ._shared import _get_session_id, _sse_frame
from .constants import (
    SSE_CONTENT_TYPE,
    SSE_HEARTBEAT_FRAME,
    SSE_HEARTBEAT_INTERVAL,
)

logger = logging.getLogger(__name__)


def _undelivered_notification_frames(store: Any) -> list[tuple[str, dict[str, object]]]:
    """Return ``(id, frame)`` pairs for undelivered notifications, oldest first.

    Queries the persistent notification store for records with
    ``delivered=false``, ordered by ``ts`` ascending, and builds an SSE frame
    for each using the same event shape as the live ``notify_user``
    publication (``type``/``title``/``body``/``urgency``/``link``).  The store
    does not persist ``urgency``/``link``, so replayed frames carry the same
    defaults a plain ``notify_user`` call would emit.
    """
    records = [r for r in store.list() if not r.delivered]
    records.sort(key=lambda r: r.ts)
    return [
        (
            record.id,
            {
                "type": SSE_NOTIFICATION_TYPE,
                "title": record.title,
                "body": record.body,
                "urgency": "default",
                "link": "",
            },
        )
        for record in records
    ]


async def events_endpoint(request: Request) -> JSONResponse | StreamingResponse:
    """Open a persistent SSE channel for background-task lifecycle events.

    ``GET /events?session_id=...`` opens a never-closing ``text/event-stream``
    that delivers ``task_started``, ``task_completed``, and ``task_failed``
    frames pushed via :class:`~robotsix_chat.chat.events.EventBus`.  Heartbeat
    comments keep the connection alive during quiet periods.

    Tolerates ``client_id`` as a legacy fallback (treated as ``session_id``).
    """
    session_id = _get_session_id(request)

    async def event_stream() -> AsyncIterator[bytes]:
        queue = request.app.state.event_bus.subscribe(session_id)

        # Replay notifications persisted while no client was connected.  Each
        # record is enqueued onto this subscriber's own queue with the same
        # SSE shape as a live notify_user event, then marked delivered as a
        # per-record batch.  Enqueue + mark run synchronously here (before the
        # send loop drains the queue), so a mid-replay failure never re-sends
        # the records already handed off and a later connect never replays
        # them again.
        store = getattr(request.app.state, "notification_store", None)
        if store is not None:
            try:
                pending = _undelivered_notification_frames(store)
            except Exception:
                logger.exception("events: failed to load undelivered notifications")
                pending = []
            for record_id, frame in pending:
                queue.put_nowait(frame)
                try:
                    store.mark_delivered([record_id])
                except Exception:
                    logger.exception(
                        "events: failed to mark notification %s delivered",
                        record_id,
                    )

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

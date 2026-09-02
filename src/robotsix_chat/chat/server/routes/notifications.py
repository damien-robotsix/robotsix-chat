"""Unread-notification API endpoints for the chat UI.

``GET /notifications/unread`` returns the persisted notifications the user
has not yet acknowledged (``read=false``), oldest first.  The list is
paginated via the ``limit`` and ``offset`` query parameters (default
``limit=100``, ``offset=0``) so clients with many unread notifications
retrieve them one page at a time.  ``POST /notifications/read`` marks
notifications as read — either a caller-supplied list of ids, or (with an
empty/omitted ``ids``) all currently unread records.

Both endpoints read from the shared
:class:`~robotsix_chat.notification.store.NotificationStore` that
``notify_user`` writes into.  They are read/mark-only: they never publish to
the SSE EventBus and never alter the ``notify_user`` tool contract, so live
delivery to connected clients is unaffected.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from ._shared import _parse_json_body

logger = logging.getLogger(__name__)

#: Default number of unread notifications returned per page.
DEFAULT_PAGE_LIMIT = 100


def _parse_pagination(request: Request) -> tuple[int, int]:
    """Parse ``limit``/``offset`` query params for the unread listing.

    ``limit`` defaults to :data:`DEFAULT_PAGE_LIMIT` and must be a positive
    integer.  ``offset`` defaults to ``0`` and must be a non-negative
    integer.  Malformed or out-of-range values raise HTTP 400.

    Returns:
        A ``(limit, offset)`` tuple.

    """
    params = request.query_params

    raw_limit = params.get("limit")
    if raw_limit is None:
        limit = DEFAULT_PAGE_LIMIT
    else:
        try:
            limit = int(raw_limit)
        except TypeError, ValueError:
            raise HTTPException(
                status_code=400, detail="limit must be a positive integer"
            ) from None
        if limit < 1:
            raise HTTPException(
                status_code=400, detail="limit must be a positive integer"
            )

    raw_offset = params.get("offset")
    if raw_offset is None:
        offset = 0
    else:
        try:
            offset = int(raw_offset)
        except TypeError, ValueError:
            raise HTTPException(
                status_code=400, detail="offset must be a non-negative integer"
            ) from None
        if offset < 0:
            raise HTTPException(
                status_code=400, detail="offset must be a non-negative integer"
            )

    return limit, offset


def _get_store(request: Request) -> Any:
    """Return the shared notification store, or raise HTTP 503 when unset."""
    store = getattr(request.app.state, "notification_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="notification store not wired")
    return store


def _to_payload(record: Any) -> dict[str, object]:
    """Serialise a store record into the API response shape."""
    return {
        "id": record.id,
        "ts": record.ts,
        "title": record.title,
        "body": record.body,
        "source_session": record.source_session,
        "delivered": record.delivered,
        "read": record.read,
    }


async def notifications_unread_endpoint(request: Request) -> JSONResponse:
    """Return unread notifications (``read=false``), oldest first.

    Records are ordered by ``ts`` ascending so the UI renders the oldest
    unacknowledged notification first.  The listing is paginated via the
    ``limit`` and ``offset`` query parameters (default ``limit=100``,
    ``offset=0``): ``limit`` must be a positive integer and ``offset`` a
    non-negative integer, or the endpoint responds with HTTP 400.  The
    response is a JSON array of at most ``limit`` record objects (``id``,
    ``ts``, ``title``, ``body``, ``source_session``, ``delivered``,
    ``read``) starting at ``offset``.
    """
    limit, offset = _parse_pagination(request)
    store = _get_store(request)
    try:
        records = store.list()
    except Exception:
        logger.exception("notifications/unread: store list failed")
        return JSONResponse(
            {"status": "error", "detail": "notification store unavailable"},
            status_code=500,
        )

    unread = [r for r in records if not r.read]
    unread.sort(key=lambda r: r.ts)
    page = unread[offset : offset + limit]
    return JSONResponse([_to_payload(r) for r in page])


async def notifications_read_endpoint(request: Request) -> JSONResponse:
    """Mark notifications as read.

    The request body may carry ``{"ids": ["...", ...]}`` to mark specific
    notifications, or an empty object / omitted ``ids`` to mark all
    currently unread notifications as read.  Returns the number of records
    whose ``read`` flag changed.
    """
    store = _get_store(request)
    try:
        body = await _parse_json_body(request)
    except HTTPException:
        raise

    raw_ids = body.get("ids")
    if raw_ids is not None:
        if not isinstance(raw_ids, list) or not all(
            isinstance(i, str) for i in raw_ids
        ):
            raise HTTPException(status_code=400, detail="ids must be a list of strings")
        ids = raw_ids
    else:
        # No ids supplied — mark every currently unread record as read.
        try:
            ids = [r.id for r in store.list() if not r.read]
        except Exception:
            logger.exception("notifications/read: store list failed")
            return JSONResponse(
                {"status": "error", "detail": "notification store unavailable"},
                status_code=500,
            )

    try:
        changed = store.mark_read(ids)
    except Exception:
        logger.exception("notifications/read: store mark_read failed")
        return JSONResponse(
            {"status": "error", "detail": "notification store unavailable"},
            status_code=500,
        )

    return JSONResponse({"status": "ok", "marked": changed})

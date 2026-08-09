"""Mill event ingestion endpoint — ticket state-change push from robotsix-mill.

``POST /mill-events`` accepts a JSON payload from the mill when a ticket
changes state.  The endpoint routes the event to all WAIT_FOR_EVENT
monitor subsessions watching that ticket, waking them within seconds
with zero polling between events.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from ._shared import _parse_json_body

logger = logging.getLogger(__name__)

# Required fields in the mill event payload.
_REQUIRED_FIELDS = frozenset({"ticket_id", "old_state", "new_state"})


async def mill_events_endpoint(request: Request) -> JSONResponse:
    """Accept a mill ticket state-change event and route to waiting monitors.

    ``POST /mill-events``

    Request body (JSON):
        ``ticket_id`` (str, required) — the ticket whose state changed.
        ``old_state`` (str, required) — previous ticket state.
        ``new_state`` (str, required) — new ticket state.
        ``board_id`` (str, optional) — originating board identifier.
        ``repo_id`` (str, optional) — originating repository identifier.
        ``timestamp`` (str, optional) — ISO-8601 timestamp of the transition.

    Returns 200 with ``{status: "ok", woken: N}`` on success, where N is
    the number of monitors woken.  Returns 400 on invalid payload.
    Events with no matching waiters return ``woken: 0`` (graceful no-op).
    """
    body = await _parse_json_body(request)

    # Validate required fields.
    missing = _REQUIRED_FIELDS - body.keys()
    if missing:
        return JSONResponse(
            {
                "status": "error",
                "detail": f"missing required fields: {', '.join(sorted(missing))}",
            },
            status_code=400,
        )

    ticket_id: str = body["ticket_id"]
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        return JSONResponse(
            {"status": "error", "detail": "ticket_id must be a non-empty string"},
            status_code=400,
        )

    registry = getattr(request.app.state, "subsession_registry", None)
    if registry is None:
        logger.warning(
            "mill-events: subsession_registry not available — event dropped "
            "for ticket %s",
            ticket_id,
        )
        return JSONResponse({"status": "ok", "woken": 0})

    event_payload: dict[str, object] = {
        "ticket_id": ticket_id,
        "old_state": body.get("old_state", ""),
        "new_state": body.get("new_state", ""),
        "board_id": body.get("board_id", ""),
        "repo_id": body.get("repo_id", ""),
        "timestamp": body.get("timestamp", ""),
    }

    woken = registry.route_mill_event(ticket_id, event_payload)

    logger.info(
        "mill-events: ticket %s state %s → %s — woken %d monitor(s).",
        ticket_id,
        body.get("old_state", ""),
        body.get("new_state", ""),
        woken,
    )

    return JSONResponse({"status": "ok", "woken": woken})

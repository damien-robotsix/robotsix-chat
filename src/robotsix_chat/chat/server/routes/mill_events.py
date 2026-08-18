"""Mill event ingestion endpoint — ticket state-change push from robotsix-mill.

``POST /mill-events`` accepts a JSON payload from the mill when a ticket
changes state.  The endpoint routes the event to all WAIT_FOR_EVENT
monitor subsessions watching that ticket, plus any paused periodic
monitors tracking the same ticket, waking them within seconds with zero
polling between events.
"""

from __future__ import annotations

import logging
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.chat.events import (
    SSE_NOTIFICATION_TYPE,
    agent_message_frame,
)

from ._shared import _parse_json_body

logger = logging.getLogger(__name__)

# Required fields in the mill event payload.
_REQUIRED_FIELDS = frozenset({"ticket_id", "old_state", "new_state"})


async def mill_events_endpoint(request: Request) -> JSONResponse:
    """Accept a mill ticket state-change event and route to waiting monitors.

    Wakes ``WAIT_FOR_EVENT`` monitors registered as event waiters and
    live ``PAUSED`` periodic monitors tracking the ticket.

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

    # Populate the ticket-state cache so the agent can surface last-known
    # state when the board API is unreachable.
    from robotsix_chat.ticket_poll.cache import ticket_state_cache

    ticket_state_cache.put_from_mill_event(event_payload)

    # -- Blocked-transition notification --
    # When a ticket enters BLOCKED from any other state (the automated
    # worker exhausted retries, a pipeline gate failed, etc.), push a
    # high-urgency notification + agent_message frame to every parent
    # conversation that owns a monitor tracking this ticket.  This gives
    # the user an immediate alert with a specific resume action, rather
    # than waiting for the next periodic monitor tick.
    old_state = event_payload.get("old_state", "")
    new_state = event_payload.get("new_state", "")
    if (
        isinstance(old_state, str)
        and isinstance(new_state, str)
        and new_state.lower() == "blocked"
        and old_state.lower() != "blocked"
    ):
        event_bus = getattr(request.app.state, "event_bus", None)
        if event_bus is not None:
            owner_sessions = registry.get_owner_session_ids_for_ticket(ticket_id)
            now = time.time()
            for session_id in sorted(owner_sessions):
                # SSE notification (browser bubble).
                event_bus.publish(
                    session_id,
                    {
                        "type": SSE_NOTIFICATION_TYPE,
                        "title": f"Ticket {ticket_id} is BLOCKED",
                        "body": (
                            f"Ticket {ticket_id} has entered BLOCKED "
                            f"state.  The automated worker may have "
                            f"exhausted retries.  Ask the assistant to "
                            f"inspect the ticket and decide whether to "
                            f"resume it once the blocker is resolved."
                        ),
                        "urgency": "high",
                        "link": ticket_id,
                    },
                )
                # Agent message frame (appears as an assistant chat bubble
                # so the user sees the notification inline).
                event_bus.publish(
                    session_id,
                    agent_message_frame(
                        (
                            f"⚠️ Ticket **{ticket_id}** has just entered "
                            f"**BLOCKED** state.  The automated "
                            f"implementation worker may have exhausted "
                            f"its retries.  I can inspect the ticket "
                            f"history and help you decide the next step — "
                            f"just ask me to resume it once the blocker "
                            f"is resolved."
                        ),
                        now,
                    ),
                )
            if owner_sessions:
                logger.info(
                    "mill-events: ticket %s blocked transition — "
                    "notified %d owner session(s).",
                    ticket_id,
                    len(owner_sessions),
                )

    logger.info(
        "mill-events: ticket %s state %s → %s — woken %d monitor(s).",
        ticket_id,
        body.get("old_state", ""),
        body.get("new_state", ""),
        woken,
    )

    return JSONResponse({"status": "ok", "woken": woken})

"""Queue-health ticket_poll tools: prioritize-all and list-stale-ready."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
from robotsix_http import RetryClient

from robotsix_chat.ticket_poll._internal import _TICKET_POLL_RETRY_CONFIG
from robotsix_chat.ticket_poll.helpers import _board_connection, _parse_json_body
from robotsix_chat.ticket_poll.mill_states import OPEN_STATES, normalize_state

if TYPE_CHECKING:
    from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)


def build_prioritize_all_open_tickets_tool(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``prioritize_all_open_tickets`` tool.

    The tool lists all open, non-prioritized tickets from the mill board
    and sets priority on every one of them in a single call.  It routes
    through *component_request* (roster-based connectivity) when available,
    falling back to the direct ``board_api_base_url`` otherwise.

    Use this when the operator asks to "prioritize tickets" or "prioritize
    all open tickets" — it replaces the manual sequence of listing tickets,
    identifying unflagged ones, and toggling priority on each individually.

    Args:
        settings: Full application settings.
        component_request: The roster-based request callable, or ``None``
            when the component roster is unavailable.

    Returns:
        A one-element list containing the ``prioritize_all_open_tickets``
        async callable, or ``[]`` when neither *component_request* nor
        ``board_api_base_url`` are available.

    """
    conn = _board_connection(settings, component_request)
    if conn is None:
        return []
    board_url, board_token, timeout = conn

    # States that indicate a ticket is still open (not terminal).
    _open_states = OPEN_STATES

    async def _list_all_tickets() -> tuple[list[dict[str, Any]] | None, str]:
        """Fetch the full ticket list from the board API.

        Returns ``(tickets, error)`` — *tickets* is a list of dicts on
        success, or ``None`` on failure (with *error* set).
        """
        if component_request is not None:
            resp = await component_request("mill", "GET", "/tickets")
            if not resp.startswith("Error:"):
                try:
                    newline = resp.index("\n")
                    status_line = resp[:newline]
                    body_str = resp[newline + 1 :]
                except ValueError:
                    return None, "Unexpected response format from /tickets"
                if not status_line.startswith("HTTP "):
                    return None, f"Unexpected status line: {status_line}"
                try:
                    status_code = int(status_line.split()[1])
                except IndexError, ValueError:
                    return None, f"Unparsable status: {status_line!r}"
                if status_code >= 400:
                    return None, f"Board API returned HTTP {status_code}"
                data, parse_error = _parse_json_body(body_str)
                if parse_error:
                    return None, parse_error
                if data is None:
                    return None, "Empty parsed response from board API"
                # The board may return a list or {"tickets": [...]}.
                if isinstance(data, list):
                    return data, ""
                if isinstance(data, dict):
                    tickets = data.get("tickets", [])
                    if isinstance(tickets, list):
                        return tickets, ""
                    return None, "Unexpected /tickets response format"
                return None, "Unexpected /tickets response format"
            logger.info(
                "prioritize_all_open_tickets: roster path failed; "
                "falling back to direct board API"
            )

        # Direct fallback.
        url = f"{board_url}/tickets"
        headers: dict[str, str] = {"Accept": "application/json"}
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
                response = await retry_client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            return None, f"Board API returned HTTP {exc.response.status_code}"
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException:
            return None, f"Board API request timed out after {timeout}s"
        except Exception as exc:
            logger.warning("prioritize_all_open_tickets direct list failed: %s", exc)
            return None, f"Board API unreachable: {exc}"
        if isinstance(data, list):
            return data, ""
        if isinstance(data, dict):
            tickets = data.get("tickets", [])
            if isinstance(tickets, list):
                return tickets, ""
        return None, "Unexpected /tickets response format"

    async def _set_priority(ticket_id: str) -> tuple[bool, str]:
        """Set priority on a single ticket via ``POST /tickets/{id}/priority``.

        Returns ``(ok, error)``.
        """
        if component_request is not None:
            resp = await component_request(
                "mill", "POST", f"/tickets/{ticket_id}/priority"
            )
            if not resp.startswith("Error:"):
                try:
                    newline = resp.index("\n")
                    status_line = resp[:newline]
                except ValueError:
                    return False, "Unexpected response format from /priority"
                if not status_line.startswith("HTTP "):
                    return False, f"Unexpected status line: {status_line}"
                try:
                    status_code = int(status_line.split()[1])
                except IndexError, ValueError:
                    return False, f"Unparsable status: {status_line!r}"
                if status_code >= 400:
                    return False, f"Board API returned HTTP {status_code}"
                return True, ""
            logger.info(
                "prioritize_all_open_tickets: roster path failed for %s; "
                "falling back to direct board API",
                ticket_id,
            )

        url = f"{board_url}/tickets/{ticket_id}/priority"
        headers: dict[str, str] = {"Accept": "application/json"}
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
                response = await retry_client.post(url, headers=headers)
                if response.status_code < 400:
                    return True, ""
                return False, f"HTTP {response.status_code}"
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException:
            return False, f"Request timed out after {timeout}s"
        except Exception as exc:
            logger.warning(
                "prioritize_all_open_tickets set-priority failed for %s: %s",
                ticket_id,
                exc,
            )
            return False, str(exc)

    async def prioritize_all_open_tickets() -> str:
        """Set priority on all open, non-prioritized tickets.

        Fetches the full ticket list from the mill board API, filters to
        tickets in a non-terminal state that are not already prioritized,
        and sets priority on every one.  Returns a summary with counts and
        per-ticket results.

        Use this when the user asks to "prioritize tickets" or
        "prioritize all open tickets" — it replaces the manual sequence
        of listing, filtering, and toggling priority individually.

        Returns:
            A JSON string with ``prioritized`` (count), ``skipped``
            (count), ``errors`` (count), ``total_open`` (count), and a
            ``results`` array of per-ticket outcome objects.

        """
        tickets, list_error = await _list_all_tickets()
        if list_error or tickets is None:
            return json.dumps(
                {
                    "error": list_error,
                    "prioritized": 0,
                    "skipped": 0,
                    "errors": 0,
                    "total_open": 0,
                    "results": [],
                },
                ensure_ascii=False,
            )

        # Classify each ticket: skip terminal/prioritized, prioritize the rest.
        to_prioritize: list[dict[str, Any]] = []
        skipped = 0
        for t in tickets:
            if not isinstance(t, dict):
                continue
            tid = t.get("ticket_id")
            if not isinstance(tid, str) or not tid:
                continue
            state = normalize_state(t.get("state", ""))
            # Skip terminal (closed/done) tickets.
            if state not in _open_states:
                continue
            # Skip already-prioritized tickets (check both field names).
            if t.get("priority") is True or t.get("flagged") is True:
                skipped += 1
                continue
            to_prioritize.append(t)

        total_open = len(to_prioritize) + skipped

        if not to_prioritize:
            return json.dumps(
                {
                    "prioritized": 0,
                    "skipped": skipped,
                    "errors": 0,
                    "total_open": total_open,
                    "results": [],
                    "note": "No open, unflagged tickets to prioritize.",
                },
                ensure_ascii=False,
            )

        # Set priority on each matching ticket concurrently (up to 10).
        sem = asyncio.Semaphore(10)

        async def _prioritize_one(t: dict[str, Any]) -> dict[str, Any]:
            tid = t["ticket_id"]
            async with sem:
                ok, error = await _set_priority(tid)
            return {
                "ticket_id": tid,
                "state": t.get("state"),
                "ok": ok,
                "error": error,
            }

        gathered = await asyncio.gather(*(_prioritize_one(t) for t in to_prioritize))

        prioritized = sum(1 for r in gathered if r["ok"])
        error_count = sum(1 for r in gathered if not r["ok"])

        return json.dumps(
            {
                "prioritized": prioritized,
                "skipped": skipped,
                "errors": error_count,
                "total_open": total_open,
                "results": gathered,
            },
            ensure_ascii=False,
        )

    return [prioritize_all_open_tickets]


def build_list_stale_ready_tickets_tool(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``list_stale_ready_tickets`` tool.

    The tool fetches all tickets from the mill board and returns those
    in ``ready`` state whose last-update timestamp exceeds the configured
    staleness threshold (``periodic.ready_staleness_minutes``).  It routes
    through *component_request* (roster-based connectivity) when available,
    falling back to the direct ``board_api_base_url`` otherwise.

    Use this tool to detect tickets stuck in the implementation queue —
    tickets that have been sitting in ``ready`` without being picked up by
    a worker for longer than expected.  The agent can then escalate or
    surface a notification to the operator.

    Args:
        settings: Full application settings.
        component_request: The roster-based request callable, or ``None``
            when the component roster is unavailable.

    Returns:
        A one-element list containing the ``list_stale_ready_tickets``
        async callable, or ``[]`` when neither *component_request* nor
        ``board_api_base_url`` are available.

    """
    import time as _time

    conn = _board_connection(settings, component_request)
    if conn is None:
        return []
    board_url, board_token, timeout = conn
    threshold_seconds = settings.periodic.ready_staleness_minutes * 60.0
    priority_threshold_seconds = (
        settings.periodic.priority_ready_staleness_minutes * 60.0
    )

    async def _fetch_ticket_list() -> tuple[list[dict[str, Any]] | None, str]:
        """Fetch the full ticket list from the board API.

        Returns ``(tickets, error)`` — *tickets* is a list of dicts on
        success, or ``None`` on failure (with *error* set).
        """
        if component_request is not None:
            resp = await component_request("mill", "GET", "/tickets")
            if not resp.startswith("Error:"):
                try:
                    newline = resp.index("\n")
                    status_line = resp[:newline]
                    body_str = resp[newline + 1 :]
                except ValueError:
                    return None, "Unexpected response format from /tickets"
                if not status_line.startswith("HTTP "):
                    return None, f"Unexpected status line: {status_line}"
                try:
                    status_code = int(status_line.split()[1])
                except IndexError, ValueError:
                    return None, f"Unparsable status: {status_line!r}"
                if status_code >= 400:
                    return None, f"Board API returned HTTP {status_code}"
                data, parse_error = _parse_json_body(body_str)
                if parse_error:
                    return None, parse_error
                if data is None:
                    return None, "Empty parsed response from board API"
                if isinstance(data, list):
                    return data, ""
                if isinstance(data, dict):
                    tickets = data.get("tickets", [])
                    if isinstance(tickets, list):
                        return tickets, ""
                    return None, "Unexpected /tickets response format"
                return None, "Unexpected /tickets response format"
            logger.info(
                "list_stale_ready_tickets: roster path failed; "
                "falling back to direct board API"
            )

        # Direct fallback.
        url = f"{board_url}/tickets"
        headers: dict[str, str] = {"Accept": "application/json"}
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
                response = await retry_client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            return None, f"Board API returned HTTP {exc.response.status_code}"
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException:
            return None, f"Board API request timed out after {timeout}s"
        except Exception as exc:
            logger.warning("list_stale_ready_tickets direct list failed: %s", exc)
            return None, f"Board API unreachable: {exc}"
        if isinstance(data, list):
            return data, ""
        if isinstance(data, dict):
            tickets = data.get("tickets", [])
            if isinstance(tickets, list):
                return tickets, ""
        return None, "Unexpected /tickets response format"

    def _seconds_since_ready(ticket: dict[str, Any], now: float) -> float | None:
        """Return seconds since *ticket* entered (or was last seen in) ``ready``.

        Uses ``updated_at`` as the primary timestamp (it reflects the last
        state transition); falls back to ``created_at`` when unavailable.
        Returns ``None`` when neither timestamp is present.
        """
        raw = ticket.get("updated_at") or ticket.get("created_at")
        if raw is None:
            return None
        # Accept ISO-8601 strings (the canonical board format) and
        # Unix-seconds floats (some board API versions).
        if isinstance(raw, (int, float)):
            return now - float(raw)
        if isinstance(raw, str):
            try:
                # ISO-8601: "2025-01-15T10:30:00Z" or with timezone offset.
                # Strip trailing "Z" and parse.
                ts_str = raw.replace("Z", "+00:00")
                from datetime import UTC, datetime

                parsed = datetime.fromisoformat(ts_str)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return now - parsed.timestamp()
            except ValueError, TypeError, OSError:
                # Try as a Unix-seconds float-as-string.
                try:
                    return now - float(ts_str)
                except ValueError, TypeError:
                    pass
        return None

    def _format_staleness(seconds: float) -> str:
        """Format a staleness duration as a human-readable string."""
        minutes = seconds / 60.0
        if minutes >= 120:
            hours = minutes / 60.0
            return f"{hours:.1f}h"
        if minutes >= 1:
            return f"{minutes:.0f}m"
        return f"{seconds:.0f}s"

    async def list_stale_ready_tickets() -> str:
        """List tickets stuck in the ``ready`` state beyond the staleness threshold.

        Fetches the full ticket list from the mill board API, filters to
        tickets in ``ready`` state, and returns those whose last-update
        timestamp exceeds the configured staleness threshold.  Tickets that
        have been picked up by a worker (no longer ``ready``) are excluded.

        Priority-flagged tickets (``priority: true`` or ``flagged: true``)
        use the longer ``priority_ready_staleness_minutes`` threshold
        instead of ``ready_staleness_minutes``, to avoid false-stall alarms
        for tickets that are legitimately waiting in a serial implementation
        queue.

        Use this to detect queue stalls — tickets sitting in ``ready``
        without being picked up — and to decide whether to escalate or
        notify the operator.

        Returns:
            A JSON string with ``stale_ready_count`` (int), ``total_ready``
            (int), ``threshold_minutes`` (int),
            ``priority_threshold_minutes`` (int), and a ``stale_tickets``
            array.  Each element has ``ticket_id``, ``state``, ``title``,
            ``staleness`` (human-readable duration), ``staleness_seconds``
            (float), ``priority`` (bool), ``updated_at``, and
            ``created_at``.

        """
        now = _time.time()
        tickets, list_error = await _fetch_ticket_list()
        if list_error or tickets is None:
            return json.dumps(
                {
                    "error": list_error,
                    "stale_ready_count": 0,
                    "total_ready": 0,
                    "threshold_minutes": settings.periodic.ready_staleness_minutes,
                    "priority_threshold_minutes": (
                        settings.periodic.priority_ready_staleness_minutes
                    ),
                    "stale_tickets": [],
                },
                ensure_ascii=False,
            )

        # Filter to ready-state tickets and compute staleness.
        stale: list[dict[str, Any]] = []
        total_ready = 0
        for t in tickets:
            if not isinstance(t, dict):
                continue
            state = str(t.get("state", "")).upper()
            if state != "READY":
                continue
            total_ready += 1

            # Priority-flagged tickets get a longer staleness threshold.
            is_priority = t.get("priority") is True or t.get("flagged") is True
            effective_threshold = (
                priority_threshold_seconds if is_priority else threshold_seconds
            )

            age = _seconds_since_ready(t, now)
            if age is None:
                # No timestamp available — include with a caveat.
                stale.append(
                    {
                        "ticket_id": t.get("ticket_id") or t.get("id", ""),
                        "title": t.get("title", ""),
                        "state": state,
                        "staleness": "unknown (no timestamp)",
                        "staleness_seconds": None,
                        "updated_at": t.get("updated_at"),
                        "created_at": t.get("created_at"),
                        "priority": is_priority,
                    }
                )
            elif age >= effective_threshold:
                stale.append(
                    {
                        "ticket_id": t.get("ticket_id") or t.get("id", ""),
                        "title": t.get("title", ""),
                        "state": state,
                        "staleness": _format_staleness(age),
                        "staleness_seconds": age,
                        "updated_at": t.get("updated_at"),
                        "created_at": t.get("created_at"),
                        "priority": is_priority,
                    }
                )

        # Sort by staleness descending (most stale first).
        stale.sort(
            key=lambda x: (
                -(
                    x["staleness_seconds"]
                    if x["staleness_seconds"] is not None
                    else float("inf")
                )
            )
        )

        return json.dumps(
            {
                "stale_ready_count": len(stale),
                "total_ready": total_ready,
                "threshold_minutes": settings.periodic.ready_staleness_minutes,
                "priority_threshold_minutes": (
                    settings.periodic.priority_ready_staleness_minutes
                ),
                "stale_tickets": stale,
            },
            ensure_ascii=False,
        )

    return [list_stale_ready_tickets]

"""Read-only ticket_poll tools: ``ticket_poll`` and ``ticket_poll_batch``."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from robotsix_chat.repo.direct.board_client import BoardClient
from robotsix_chat.ticket_poll._internal import _resolve_ticket_ids
from robotsix_chat.ticket_poll.helpers import (
    _board_connection,
    _delivery_fields,
    _parse_json_body,
)
from robotsix_chat.ticket_poll.mill_states import TERMINAL_STATES, normalize_state

if TYPE_CHECKING:
    from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)


def build_ticket_poll_tools(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``ticket_poll`` and ``ticket_poll_batch`` tools.

    When *component_request* is available the tools route through the
    roster-based component connectivity (same path as ticket-state
    verification).  Otherwise they fall back to the direct
    ``board_api_base_url``.

    When *component_request* is provided, the tool uses the roster-based
    path as its primary connectivity method (resolving ``"mill"`` via the
    central-deploy roster or component fallbacks); on failure it falls
    back to the direct ``board_api_base_url`` path.  This ensures the
    chat container reaches the mill on its actual service hostname rather
    than a potentially misconfigured direct URL.

    Args:
        settings: Full application settings.
        component_request: The roster-based request callable, or ``None``
            when the component roster is unavailable.

    Returns:
        A two-element list containing the ``ticket_poll`` and
        ``ticket_poll_batch`` async callables, or ``[]`` when neither
        *component_request* nor ``board_api_base_url`` are available.

    """
    conn = _board_connection(settings, component_request)
    if conn is None:
        return []
    board_url, board_token, timeout = conn

    board_client = BoardClient(settings.direct_repo)

    async def _fetch_ticket_via_component(
        ticket_id: str,
    ) -> tuple[int, str | None, str]:
        """Fetch a ticket via *component_request*; return ``(status, body, error)``.

        Returns ``(status, body, "")`` on success, ``(status, None, error)``
        on failure.  *body* is the raw response body string.
        """
        if component_request is None:  # type narrow for mypy
            return (
                0,
                None,
                "Board API request via component_request failed: "
                "component_request is not available",
            )
        resp = await component_request("mill", "GET", f"/tickets/{ticket_id}")
        if resp.startswith("Error:"):
            return (
                0,
                None,
                f"Board API request via component_request failed: {resp}",
            )
        try:
            newline = resp.index("\n")
            status_line = resp[:newline]
            body_str = resp[newline + 1 :]
        except ValueError:
            return (
                0,
                None,
                "Board API request via component_request failed: "
                "unexpected response format",
            )
        if not status_line.startswith("HTTP "):
            return (
                0,
                None,
                f"Board API request via component_request failed: {status_line}",
            )
        try:
            status_code = int(status_line.split()[1])
        except IndexError, ValueError:
            return (
                0,
                None,
                f"Board API request via component_request failed: "
                f"unparsable status {status_line!r}",
            )
        return status_code, body_str, ""

    async def _augment_with_history(ticket_id: str, data: dict[str, Any]) -> None:
        """Attach ``GET /tickets/{id}/history`` rows to *data* for terminal tickets.

        ``GET /tickets/{id}`` returns no history, so the delivery check
        would otherwise see only ``state`` + ``pr_url``.  Only terminal
        tickets pay the extra round-trip; failures leave *data* untouched.
        """
        if normalize_state(data.get("state")) not in TERMINAL_STATES:
            return
        if isinstance(data.get("history"), list):
            return
        if component_request is not None:
            resp = await component_request(
                "mill", "GET", f"/tickets/{ticket_id}/history"
            )
            if isinstance(resp, str) and resp.startswith("HTTP "):
                try:
                    status_code = int(resp.split(maxsplit=2)[1])
                    body_str = resp[resp.index("\n") + 1 :]
                except IndexError, ValueError:
                    status_code, body_str = 0, ""
                if status_code and status_code < 400:
                    rows, parse_error = _parse_json_body(body_str)
                    if not parse_error and isinstance(rows, list):
                        data["history"] = [r for r in rows if isinstance(r, dict)]
                        return
        rows_direct = await board_client.get_ticket_history(ticket_id)
        if rows_direct is not None:
            data["history"] = rows_direct

    async def ticket_poll(ticket_id: str) -> str:
        """Poll the mill board for a ticket's current state.

        Routes through the component roster when available, falling back
        to the direct board API on any failure.  Resolves paraphrased /
        abbreviated ticket IDs (e.g. ``...-my-ticket-a3f2``) against the
        live board before making the request — pass a hash suffix or slug
        substring and it will be mapped to the full ticket ID.

        When the board API is unreachable, falls back to the last-known
        state from the ticket-state cache (populated by mill push events
        and prior successful polls), surfacing the cached state with a
        clear staleness caveat.

        Args:
            ticket_id: The ticket identifier (e.g. "20250101T120000Z-my-ticket-a1b2").
                Paraphrased / abbreviated IDs are resolved via hash-suffix
                or slug-substring match against the live board ticket list.

        Returns:
            A JSON string with ``ticket_id``, ``state`` (or ``null`` when
            the field is absent), ``error`` (empty on success), plus the
            delivery evidence: ``pr_url`` (the ticket's PR, or ``null``),
            ``delivered`` (true when a ``done`` / ``closed`` ticket has a
            PR or passed through a merge state — a closed ticket with a
            PR is DELIVERED, not dropped), ``delivery_note`` (e.g.
            ``"closed after delivery (retrospect): PR <url>"``) and
            ``unexpected_terminal`` — a diagnostic string ONLY when the
            ticket reached ``closed`` / ``done`` with no PR and without
            ever entering an active-work or merge state (``draft →
            closed``), ``null`` otherwise.
            When the board is unreachable and a cached entry exists, the
            ``cache_caveat`` field carries a staleness note.

        """
        from robotsix_chat.ticket_poll.cache import ticket_state_cache

        # Resolve paraphrased / abbreviated IDs against the live board
        # before making the request.  This prevents 404 failures when an
        # ID was derived from narrative text rather than from a board API
        # response.
        resolved_map = await _resolve_ticket_ids(
            board_url, board_token, timeout, [ticket_id]
        )
        effective_id = resolved_map.get(ticket_id) or ticket_id

        if component_request is not None:
            status, body, error = await _fetch_ticket_via_component(effective_id)
            if not error and status < 400 and body is not None:
                data, parse_error = _parse_json_body(body)
                if not parse_error and data is not None:
                    state = data.get("state")
                    await _augment_with_history(effective_id, data)
                    result: dict[str, Any] = {
                        "ticket_id": effective_id,
                        "state": state,
                        "error": "",
                        **_delivery_fields(data),
                    }
                    # Populate the cache on every successful fetch so the
                    # fallback path always has a recent entry.
                    ticket_state_cache.put_from_poll(effective_id, result)
                    return json.dumps(result, ensure_ascii=False)
            logger.info(
                "ticket_poll roster path failed for %s; "
                "falling back to direct board API",
                effective_id,
            )

        direct_result = await _ticket_poll_direct(effective_id)
        direct_data = json.loads(direct_result)
        if direct_data.get("error"):
            # Board API unreachable — try the cache.
            cached, caveat = ticket_state_cache.get(effective_id)
            if cached is not None:
                cached["cache_caveat"] = caveat
                # Preserve the original error so the caller sees both
                # that the live lookup failed AND the cached state.
                cached["error"] = direct_data["error"]
                return json.dumps(cached, ensure_ascii=False)
        else:
            # Successful direct fetch — populate the cache.
            ticket_state_cache.put_from_poll(effective_id, direct_data)
        return direct_result

    async def _ticket_poll_direct(ticket_id: str) -> str:
        """Poll the mill board for a ticket's current state.

        Directly queries the board API (bypasses the component roster).
        Use this when ``component_request`` is unavailable or as an
        independent verification of ticket state.
        """
        data, reason = await board_client.get_ticket_data_detailed(ticket_id)
        if data is None:
            return json.dumps(
                {
                    "ticket_id": ticket_id,
                    "state": None,
                    "error": reason or "Board API request failed",
                },
                ensure_ascii=False,
            )
        state = data.get("state")
        await _augment_with_history(ticket_id, data)
        return json.dumps(
            {
                "ticket_id": ticket_id,
                "state": state,
                "error": "",
                **_delivery_fields(data),
            },
            ensure_ascii=False,
        )

    async def ticket_poll_batch(ticket_ids: list[str]) -> str:
        """Fetch full ticket data for multiple tickets concurrently.

        Routes through the component roster when available; falls back to
        the direct board API otherwise.  Queries ``GET /tickets/{id}`` for
        every ticket in parallel (up to 10 concurrent requests).  Returns
        the complete API response for each ticket — including ``state``,
        ``events`` / history, comments, and cycle metadata — so you can
        classify blocked tickets by failure signature (e.g.
        "implement-loop/3of3", "git-failure", "capability-gap") without
        N sequential round-trips.

        This tool uses the direct board API path and does NOT go through
        the roster.  Use ``ticket_poll`` for roster-first connectivity.

        Args:
            ticket_ids: List of ticket identifiers to fetch.

        Returns:
            A JSON string with a ``tickets`` array.  Each element has:

            - ``ticket_id`` — the supplied identifier
            - ``state`` — the ticket's current state string (or ``null``)
            - ``data`` — the full JSON response from the board API (for
              terminal tickets, augmented with the ``history`` rows from
              ``GET /tickets/{id}/history``)
            - ``error`` — empty on success, or a diagnostic message on failure
            - ``pr_url`` / ``delivered`` / ``delivery_note`` /
              ``unexpected_terminal`` — same delivery evidence as
              ``ticket_poll``

        """
        # Resolve paraphrased / abbreviated IDs against the live board
        # before making any per-ticket requests.  This prevents 404
        # failures when an ID was derived from narrative text rather
        # than from a board API response.
        resolved_map = await _resolve_ticket_ids(
            board_url, board_token, timeout, ticket_ids
        )

        # Use resolved IDs where available; keep originals for
        # unresolvable ones (they will surface as 404s, same as before).
        effective_ids = [resolved_map.get(tid) or tid for tid in ticket_ids]

        sem = asyncio.Semaphore(10)

        if component_request is not None:

            async def _fetch_one_via_component(ticket_id: str) -> dict[str, Any]:
                async with sem:
                    status, body, error = await _fetch_ticket_via_component(ticket_id)
                    if error:
                        return {
                            "ticket_id": ticket_id,
                            "state": None,
                            "data": None,
                            "error": error,
                        }
                    if status >= 400:
                        return {
                            "ticket_id": ticket_id,
                            "state": None,
                            "data": None,
                            "error": f"Board API returned HTTP {status}",
                        }
                    if body is None:
                        return {
                            "ticket_id": ticket_id,
                            "state": None,
                            "data": None,
                            "error": "Empty response body from board API",
                        }
                    data, parse_error = _parse_json_body(body)
                    if parse_error:
                        return {
                            "ticket_id": ticket_id,
                            "state": None,
                            "data": None,
                            "error": parse_error,
                        }
                    if data is None:  # guarded by parse_error check above
                        return {
                            "ticket_id": ticket_id,
                            "state": None,
                            "data": None,
                            "error": "Empty parsed response from board API",
                        }
                    await _augment_with_history(ticket_id, data)
                    result: dict[str, Any] = {
                        "ticket_id": ticket_id,
                        "state": data.get("state"),
                        "data": data,
                        "error": "",
                        **_delivery_fields(data),
                    }
                    # Populate cache on every successful batch fetch.
                    from robotsix_chat.ticket_poll.cache import ticket_state_cache

                    ticket_state_cache.put_from_poll(ticket_id, result)
                    return result

            gathered = await asyncio.gather(
                *(_fetch_one_via_component(tid) for tid in effective_ids)
            )
            return json.dumps({"tickets": list(gathered)}, ensure_ascii=False)

        async def _fetch_one_direct(ticket_id: str) -> dict[str, Any]:
            async with sem:
                data, reason = await board_client.get_ticket_data_detailed(ticket_id)
                if data is None:
                    return {
                        "ticket_id": ticket_id,
                        "state": None,
                        "data": None,
                        "error": reason or "Board API request failed",
                    }
                await _augment_with_history(ticket_id, data)
                result: dict[str, Any] = {
                    "ticket_id": ticket_id,
                    "state": data.get("state"),
                    "data": data,
                    "error": "",
                    **_delivery_fields(data),
                }
                # Populate cache on every successful batch fetch.
                from robotsix_chat.ticket_poll.cache import ticket_state_cache

                ticket_state_cache.put_from_poll(ticket_id, result)
                return result

        gathered = await asyncio.gather(
            *(_fetch_one_direct(tid) for tid in effective_ids)
        )
        return json.dumps({"tickets": list(gathered)}, ensure_ascii=False)

    return [ticket_poll, ticket_poll_batch]

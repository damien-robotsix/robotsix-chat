"""Shared internals for ticket_poll tool builders: retry config and id helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from robotsix_http import RetryClient, RetryConfig

from robotsix_chat.ticket_poll.helpers import (
    _component_response_is_error,
    _match_candidates,
    _parse_json_body,
)

logger = logging.getLogger(__name__)

# These internals are consumed via cross-module import (e.g.
# ``ticket_poll_write`` imports the ``_CLASSIFY_*`` pacing constants).
# Declaring them in ``__all__`` documents the export surface and marks the
# module-private constants as intentional exports for static analysis (CodeQL
# ``py/unused-global-variable`` treats ``__all__`` membership as a use).
__all__ = [
    "_CLASSIFY_POLL_SECONDS",
    "_CLASSIFY_WAIT_SECONDS",
    "_CLOSED_LOOKBACK_DAYS",
    "_TICKET_POLL_RETRY_CONFIG",
    "_fetch_board_repo_ids",
    "_fetch_ticket_ids",
    "_resolve_ticket_ids",
]

# Retry configuration for ticket poll requests — transient network blips
# should not surface as "board API unreachable" to the agent.
_TICKET_POLL_RETRY_CONFIG = RetryConfig(
    max_retries=2,
    backoff_base=1.0,
    backoff_cap=10.0,
    jitter_factor=0.5,
)


#: mark_ticket_ready: a ticket filed moments ago is still in mill's
#: ``classifying`` state (ops/scope/dedup classification), from which the only
#: legal exit is ``draft`` — ``classifying -> ready`` is a 409. Rather than
#: burning a turn per approval (observed 2026-09-07: two 409s in two minutes,
#: both on tickets the operator had just asked to file-and-approve), the tool
#: waits for classification to finish, bounded so a busy worker cannot stall a
#: chat turn, then retries the transition once.
_CLASSIFY_WAIT_SECONDS: float = 45.0
_CLASSIFY_POLL_SECONDS: float = 3.0


async def _fetch_board_repo_ids(
    board_url: str,
    board_token: str,
    timeout: float,
    component_request: Callable[..., Any] | None,
) -> set[str] | None:
    """Fetch the set of registered ``repo_id`` values from the board.

    Tries *component_request* (roster-based) first, falling back to the
    direct board API's ``GET /repos`` endpoint.  Returns ``None`` when
    both paths fail — callers should treat this as "validation
    unavailable" and proceed without it rather than blocking the
    operation.
    """
    # Try component_request (roster-based) first.
    if component_request is not None:
        try:
            resp = await component_request("mill", "GET", "/repos")
            if not _component_response_is_error(resp):
                body: Any = resp
                # Strip the "HTTP <status>\n<body>" envelope when present.
                if isinstance(body, str) and body.startswith("HTTP "):
                    try:
                        newline = body.index("\n")
                        body = body[newline + 1 :]
                    except ValueError:
                        # No newline in the "HTTP <status>" line — the body is
                        # already the bare payload, so leave it unchanged.
                        pass
                parsed, _err = (
                    _parse_json_body(body) if isinstance(body, str) else (body, "")
                )
                if isinstance(parsed, list):
                    return {
                        entry["repo_id"]
                        for entry in parsed
                        if isinstance(entry, dict) and "repo_id" in entry
                    }
        except Exception as exc:
            logger.warning(
                "file_ticket: failed to fetch repos via roster: %s",
                exc,
            )

    # Direct fallback via board API.
    if not board_url:
        return None

    url = f"{board_url}/repos"
    headers: dict[str, str] = {"Accept": "application/json"}
    if board_token:
        headers["Authorization"] = f"Bearer {board_token}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
            response = await retry_client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return {
                    entry["repo_id"]
                    for entry in data
                    if isinstance(entry, dict) and "repo_id" in entry
                }
    except Exception as exc:
        logger.warning(
            "file_ticket: failed to fetch repos from board API: %s",
            exc,
        )

    return None


_CLOSED_LOOKBACK_DAYS = 14
"""How far back the closed-ticket fallback looks when an id fails to resolve.

Mill's default ``GET /tickets`` hides closed tickets, so an abbreviated id
of a ticket that already shipped could never resolve — chat then re-filed
already-shipped work (a9bc dup of c64a, 2026-09-07) or fell through to a
bare 404.  The full closed list is ~950 tickets / 26 s; restricting to
recently updated tickets keeps the fallback well inside the tool timeout.
"""


async def _fetch_ticket_ids(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
) -> list[str] | None:
    """Return the ticket ids listed by ``GET /tickets`` or ``None`` on failure."""
    try:
        retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
        response = await retry_client.get(url, headers=headers, params=params)
        response.raise_for_status()
        tickets_data = response.json()
    except Exception as exc:
        logger.warning(
            "ticket_poll: failed to fetch ticket list for ID resolution: %s",
            exc,
        )
        return None

    # Extract ticket IDs from the response.  The board may return a
    # JSON array of ticket objects or an object with a "tickets" key.
    if isinstance(tickets_data, list):
        ticket_objects = tickets_data
    elif isinstance(tickets_data, dict):
        ticket_objects = tickets_data.get("tickets", [])
        if not isinstance(ticket_objects, list):
            logger.warning(
                "ticket_poll: unexpected GET /tickets response "
                "format — expected list or {tickets: [...]}"
            )
            return None
    else:
        logger.warning(
            "ticket_poll: unexpected GET /tickets response type %s",
            type(tickets_data).__name__,
        )
        return None

    # The mill board returns tickets keyed ``id``; older/other boards used
    # ``ticket_id``.  Accepting only the latter made every resolution fail
    # with "(0 tickets listed)" against mill (live logs 2026-09-01).
    return [
        tid
        for t in ticket_objects
        if isinstance(t, dict)
        for tid in (t.get("id") or t.get("ticket_id"),)
        if isinstance(tid, str) and tid
    ]


async def _resolve_ticket_ids(
    board_url: str,
    board_token: str,
    timeout: float,
    candidate_ids: list[str],
) -> dict[str, str | None]:
    """Resolve candidate ticket IDs against the live board.

    Fetches ``GET /tickets`` and for each candidate ID tries, in order:

    1. **Exact match** — the candidate ID appears verbatim in the board.
    2. **Hash-suffix match** — the last 4 hex chars of the candidate
       (e.g. ``a3f2`` from ``...-my-ticket-a3f2``) uniquely match one
       ticket's hash suffix.
    3. **Slug-substring match** — the non-timestamp, non-hash portion
       of the candidate appears as a substring of exactly one ticket's
       full ID.

    Candidates that stay unresolved against the open board are retried
    once against recently updated CLOSED tickets
    (``?include_closed=true&updated_after=<now - 14 days>``): the default
    listing hides closed tickets, so an abbreviated id of a ticket that
    already shipped could otherwise never resolve.

    Returns a dict mapping each candidate ID to its resolved full
    ticket ID, or ``None`` when the candidate could not be resolved.

    Resolution uses the direct board API and is best-effort: when the
    board is unreachable or the response is unparsable, every candidate
    maps to ``None`` and callers fall back to the original IDs.
    """
    if not candidate_ids:
        return {}

    if not board_url:
        return {cid: None for cid in candidate_ids}

    url = f"{board_url}/tickets"
    headers: dict[str, str] = {"Accept": "application/json"}
    if board_token:
        headers["Authorization"] = f"Bearer {board_token}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        open_ids = await _fetch_ticket_ids(client, url, headers)
        if open_ids is None:
            return {cid: None for cid in candidate_ids}

        result = _match_candidates(candidate_ids, open_ids)
        unresolved = [cid for cid, full in result.items() if full is None]
        if unresolved:
            since = datetime.now(UTC) - timedelta(days=_CLOSED_LOOKBACK_DAYS)
            closed_ids = await _fetch_ticket_ids(
                client,
                url,
                headers,
                params={
                    "include_closed": "true",
                    "updated_after": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
            if closed_ids:
                combined = list(dict.fromkeys([*open_ids, *closed_ids]))
                for cid, full in _match_candidates(unresolved, combined).items():
                    if full is not None:
                        logger.info(
                            "ticket_poll: %r resolved against closed tickets → %r",
                            cid,
                            full,
                        )
                        result[cid] = full

    for cid, full in result.items():
        if full is None and isinstance(cid, str) and cid.strip():
            # Unresolvable — the caller will attempt the original ID.
            logger.warning(
                "ticket_poll: could not resolve %r against the board "
                "(%d open tickets listed, closed fallback tried)",
                cid,
                len(open_ids),
            )

    return result

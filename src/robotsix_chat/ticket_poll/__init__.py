"""Mill board API tools — ticket polling and PR merging.

Routes through ``component_request`` (roster-based connectivity) when
available, falling back to the direct ``board_api_base_url`` otherwise.

Provides ``ticket_poll(ticket_id)`` and ``ticket_poll_batch(ticket_ids)`` —
dedicated tools that return ticket state and full data for single-ticket
polling and bulk read-only triage respectively.

Also provides ``merge_pull_request(ticket_id)`` — a dedicated merge tool
that calls ``POST /tickets/{id}/merge-now`` on the mill board API to merge
approved PRs/MRs.  Prefer this over the generic ``component_request`` when
merging PRs for tickets in ``waiting_auto_merge`` or ``human_mr_approval``
state.

Exposes :func:`build_ticket_poll_tools` and
:func:`build_merge_pull_request_tool` — factories returning the LLM tools.
Returns no tools when neither ``component_request`` nor
``board_api_base_url`` are available.  Also exposes
:func:`load_ticket_poll_skill` which returns the component skill markdown
for injection into the agent instruction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from robotsix_http import RetryClient, RetryConfig

from robotsix_chat.repo.direct.board_client import BoardClient

if TYPE_CHECKING:
    from robotsix_chat.config import Settings

__all__ = [
    "build_merge_pull_request_tool",
    "build_prioritize_all_open_tickets_tool",
    "build_ticket_poll_tools",
    "load_ticket_poll_skill",
]

logger = logging.getLogger(__name__)

# Retry configuration for ticket poll requests — transient network blips
# should not surface as "board API unreachable" to the agent.
_TICKET_POLL_RETRY_CONFIG = RetryConfig(
    max_retries=2,
    backoff_base=1.0,
    backoff_cap=10.0,
    jitter_factor=0.5,
)


def _parse_json_body(body: str) -> tuple[dict[str, Any] | None, str]:
    """Parse *body* as JSON, returning ``(data, error)``.

    *error* is empty on success, or a diagnostic message on failure.
    Callers format the error into their own return shape.
    """
    try:
        return json.loads(body), ""
    except json.JSONDecodeError, TypeError:
        return None, "Non-JSON response from board API"


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

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
            response = await retry_client.get(url, headers=headers)
            response.raise_for_status()
            tickets_data = response.json()
    except Exception as exc:
        logger.warning(
            "ticket_poll: failed to fetch ticket list for ID resolution: %s",
            exc,
        )
        return {cid: None for cid in candidate_ids}

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
            return {cid: None for cid in candidate_ids}
    else:
        logger.warning(
            "ticket_poll: unexpected GET /tickets response type %s",
            type(tickets_data).__name__,
        )
        return {cid: None for cid in candidate_ids}

    all_ids: list[str] = [
        t["ticket_id"]
        for t in ticket_objects
        if isinstance(t, dict) and isinstance(t.get("ticket_id"), str)
    ]

    # Build a reverse index: hash-suffix → list of matching full IDs.
    suffix_index: dict[str, list[str]] = {}
    for tid in all_ids:
        m = re.search(r"-([0-9a-f]{4})$", tid)
        if m:
            suffix_index.setdefault(m.group(1), []).append(tid)

    result: dict[str, str | None] = {}
    for cid in candidate_ids:
        if not isinstance(cid, str) or not cid.strip():
            result[cid] = None
            continue

        # 1. Exact match.
        if cid in all_ids:
            result[cid] = cid
            continue

        # 2. Hash-suffix match — extract the last 4 hex chars.
        suffix_match = re.search(r"([0-9a-f]{4})$", cid)
        if suffix_match:
            suffix = suffix_match.group(1)
            matches = suffix_index.get(suffix, [])
            if len(matches) == 1:
                resolved = matches[0]
                logger.info(
                    "ticket_poll: resolved %r → %r (hash suffix %r)",
                    cid,
                    resolved,
                    suffix,
                )
                result[cid] = resolved
                continue
            if len(matches) > 1:
                logger.warning(
                    "ticket_poll: ambiguous hash suffix %r for %r — matches %s",
                    suffix,
                    cid,
                    matches,
                )
                # Fall through to slug match.

        # 3. Slug-substring match.
        slug = re.sub(r"^\d{8}T\d{6}Z-", "", cid)
        slug = re.sub(r"-?[0-9a-f]{4}$", "", slug)
        if slug and len(slug) >= 4:
            slug_matches = [tid for tid in all_ids if slug.lower() in tid.lower()]
            if len(slug_matches) == 1:
                resolved = slug_matches[0]
                logger.info(
                    "ticket_poll: resolved %r → %r (slug match %r)",
                    cid,
                    resolved,
                    slug,
                )
                result[cid] = resolved
                continue
            if len(slug_matches) > 1:
                logger.warning(
                    "ticket_poll: ambiguous slug %r for %r — matches %s",
                    slug,
                    cid,
                    slug_matches,
                )

        # Unresolvable — the caller will attempt the original ID.
        logger.warning(
            "ticket_poll: could not resolve %r against the board (%d tickets listed)",
            cid,
            len(all_ids),
        )
        result[cid] = None

    return result


# ---------------------------------------------------------------------------
# Unexpected-terminal-state detection
# ---------------------------------------------------------------------------

# Board API states that indicate the ticket was in active work (picked up
# by an agent or reviewer), not sitting unexamined in draft/pre-review.
_ACTIVE_WORK_STATES = frozenset({"APPROVED", "IN_PROGRESS", "BLOCKED"})

# States that tell us the ticket reached a terminal outcome.
_TERMINAL_STATES = frozenset({"CLOSED", "DONE"})


def _check_unexpected_terminal(data: dict[str, Any]) -> str | None:
    """Check whether *data* shows an unexpected path to a terminal state.

    Returns a diagnostic string when the ticket is ``CLOSED`` or ``DONE``
    yet the ``events`` / ``history`` arrays show no sign that the ticket
    was ever picked up for implementation, review, or triage — suggesting
    it was closed prematurely (e.g.  ``DRAFT → CLOSED`` without approval).
    Returns ``None`` when the transition looks normal or when the data
    carries insufficient history to decide (no false positives).

    This is a pure function — no I/O.  Callers supply the parsed JSON
    body from a ``GET /tickets/{id}`` response.
    """
    state = data.get("state")
    if not isinstance(state, str) or state.upper() not in _TERMINAL_STATES:
        return None

    # 1. Check history entries for a prior active-work state.
    history: list[dict[str, Any]] = data.get("history", [])
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            prior = str(entry.get("state", entry.get("to", ""))).upper()
            if prior in _ACTIVE_WORK_STATES:
                return None  # Ticket reached an active state — normal.

    # 2. Check events for implement / unblock / resume activity.
    events: list[dict[str, Any]] = data.get("events", [])
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            ev_type = str(ev.get("type", ev.get("action", ""))).lower()
            if any(
                keyword in ev_type for keyword in ("implement", "unblock", "resume")
            ):
                return None  # Implementation activity — normal.

    # 3. No active-work evidence found — flag the transition.
    return (
        f"Ticket reached {state} without ever entering an active work "
        f"state (APPROVED, IN_PROGRESS, or BLOCKED).  "
        f"This may indicate the ticket was closed from a draft or "
        f"pre-review state without approval."
    )


def load_ticket_poll_skill() -> str:
    """Return the ticket-poll component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.
    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _board_connection(
    settings: Settings,
    component_request: Callable[..., Any] | None,
) -> tuple[str, str, float] | None:
    """Return ``(board_url, board_token, timeout)`` or ``None`` if unavailable.

    Returns ``None`` when neither *component_request* nor
    ``board_api_base_url`` are available, signalling callers to return an
    empty tool list.
    """
    board_url = settings.direct_repo.board_api_base_url.strip()
    if not component_request and not board_url:
        return None
    board_url = board_url.rstrip("/") if board_url else ""
    board_token = settings.direct_repo.board_api_token.get_secret_value()
    timeout = settings.direct_repo.timeout
    return board_url, board_token, timeout


def build_merge_pull_request_tool(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``merge_pull_request`` tool.

    The tool calls the mill board's ``POST /tickets/{id}/merge-now`` endpoint
    to merge the approved PR/MR associated with a ticket.  It routes through
    *component_request* (roster-based connectivity) when available, falling
    back to the direct ``board_api_base_url`` otherwise.

    Use this tool when a ticket is in ``waiting_auto_merge`` or
    ``human_mr_approval`` state and the associated PR has been approved —
    it is the primary path for merging approved MRs across the robotsix
    fleet.

    Args:
        settings: Full application settings.
        component_request: The roster-based request callable, or ``None``
            when the component roster is unavailable.

    Returns:
        A one-element list containing the ``merge_pull_request`` async
        callable, or ``[]`` when neither *component_request* nor
        ``board_api_base_url`` are available.

    """
    conn = _board_connection(settings, component_request)
    if conn is None:
        return []
    board_url, board_token, timeout = conn

    async def merge_pull_request(ticket_id: str) -> str:
        """Merge the pull request associated with a ticket.

        Calls the mill board's merge-now endpoint to merge the approved
        PR/MR for the given ticket.  Use this when a ticket is in
        ``waiting_auto_merge`` or ``human_mr_approval`` state and the
        associated PR has been approved by a human reviewer.

        This is a dedicated merge tool — prefer it over the generic
        ``component_request`` for merging PRs.  The tool routes through
        the component roster when available, falling back to the direct
        board API.

        Args:
            ticket_id: The ticket ID whose associated PR should be merged.

        Returns:
            A status message from the mill API — success confirmation or
            an error describing why the merge failed (e.g. the PR is not
            approved, conflicts exist, or required status checks have not
            passed).

        """
        # Resolve paraphrased / abbreviated IDs against the live board
        # before making the request.  This prevents 404 failures when an
        # ID was derived from narrative text rather than from a board API
        # response.
        resolved_map = await _resolve_ticket_ids(
            board_url, board_token, timeout, [ticket_id]
        )
        effective_id = resolved_map.get(ticket_id) or ticket_id

        # Try component_request (roster-based) first.
        if component_request is not None:
            resp = await component_request(
                "mill", "POST", f"/tickets/{effective_id}/merge-now"
            )
            if not resp.startswith("Error:"):
                return str(resp)
            logger.info(
                "merge_pull_request: roster path failed for %s; "
                "falling back to direct board API",
                effective_id,
            )

        # Direct fallback via board API.
        url = f"{board_url}/tickets/{effective_id}/merge-now"
        headers: dict[str, str] = {"Accept": "application/json"}
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
                response = await retry_client.post(url, headers=headers)
                try:
                    body = response.json()
                    body_str = json.dumps(body)
                except Exception:
                    body_str = response.text
                return f"HTTP {response.status_code}\n{body_str}"
        except httpx.HTTPStatusError as exc:
            try:
                body = exc.response.json()
                body_str = json.dumps(body)
            except Exception:
                body_str = exc.response.text
            return f"HTTP {exc.response.status_code}\n{body_str}"
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException:
            return (
                f"Error merging PR for ticket {effective_id}: "
                f"board API request timed out after {timeout}s"
            )
        except Exception as exc:
            logger.warning(
                "merge_pull_request direct path failed for %s: %s",
                effective_id,
                exc,
            )
            return f"Error merging PR for ticket {effective_id}: {exc}"

    return [merge_pull_request]


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
    _open_states = frozenset(
        {
            "DRAFT",
            "REFINING",
            "APPROVED",
            "BLOCKED",
            "IN_PROGRESS",
            "IMPLEMENT_COMPLETE",
            "REVIEW",
            "WAITING_AUTO_MERGE",
            "HUMAN_MR_APPROVAL",
            "AWAITING_USER_REPLY",
        }
    )

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
                    return None, f"Unparseable status: {status_line!r}"
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
                    return False, f"Unparseable status: {status_line!r}"
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
            state = str(t.get("state", "")).upper()
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
            the field is absent), ``error`` (empty on success), and
            ``unexpected_terminal`` — a diagnostic string when the ticket
            reached a terminal state (``CLOSED`` / ``DONE``) without ever
            passing through an active-work state, or ``null`` otherwise.
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
                    unexpected = _check_unexpected_terminal(data)
                    result: dict[str, Any] = {
                        "ticket_id": effective_id,
                        "state": state,
                        "error": "",
                        "unexpected_terminal": unexpected,
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
        unexpected = _check_unexpected_terminal(data)
        return json.dumps(
            {
                "ticket_id": ticket_id,
                "state": state,
                "error": "",
                "unexpected_terminal": unexpected,
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
            - ``data`` — the full JSON response from the board API
            - ``error`` — empty on success, or a diagnostic message on failure

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
                    result: dict[str, Any] = {
                        "ticket_id": ticket_id,
                        "state": data.get("state"),
                        "data": data,
                        "error": "",
                        "unexpected_terminal": _check_unexpected_terminal(data),
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
                result: dict[str, Any] = {
                    "ticket_id": ticket_id,
                    "state": data.get("state"),
                    "data": data,
                    "error": "",
                    "unexpected_terminal": _check_unexpected_terminal(data),
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

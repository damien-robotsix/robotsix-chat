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

        Args:
            ticket_id: The ticket identifier (e.g. "20250101T120000Z-my-ticket-a1b2").
                Paraphrased / abbreviated IDs are resolved via hash-suffix
                or slug-substring match against the live board ticket list.

        Returns:
            A JSON string with ``ticket_id``, ``state`` (or ``null`` when
            the field is absent), and ``error`` (empty on success).

        """
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
                    return json.dumps(
                        {"ticket_id": effective_id, "state": state, "error": ""},
                        ensure_ascii=False,
                    )
            logger.info(
                "ticket_poll roster path failed for %s; "
                "falling back to direct board API",
                effective_id,
            )

        return await _ticket_poll_direct(effective_id)

    async def _ticket_poll_direct(ticket_id: str) -> str:
        """Poll the mill board for a ticket's current state.

        Directly queries the board API (bypasses the component roster).
        Use this when ``component_request`` is unavailable or as an
        independent verification of ticket state.
        """
        data = await board_client.get_ticket_data(ticket_id)
        if data is None:
            return json.dumps(
                {
                    "ticket_id": ticket_id,
                    "state": None,
                    "error": "Board API request failed",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ticket_id": ticket_id,
                "state": data.get("state"),
                "error": "",
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
                    return {
                        "ticket_id": ticket_id,
                        "state": data.get("state"),
                        "data": data,
                        "error": "",
                    }

            gathered = await asyncio.gather(
                *(_fetch_one_via_component(tid) for tid in effective_ids)
            )
            return json.dumps({"tickets": list(gathered)}, ensure_ascii=False)

        async def _fetch_one_direct(ticket_id: str) -> dict[str, Any]:
            async with sem:
                data = await board_client.get_ticket_data(ticket_id)
                if data is None:
                    return {
                        "ticket_id": ticket_id,
                        "state": None,
                        "data": None,
                        "error": "Board API request failed",
                    }
                return {
                    "ticket_id": ticket_id,
                    "state": data.get("state"),
                    "data": data,
                    "error": "",
                }

        gathered = await asyncio.gather(
            *(_fetch_one_direct(tid) for tid in effective_ids)
        )
        return json.dumps({"tickets": list(gathered)}, ensure_ascii=False)

    return [ticket_poll, ticket_poll_batch]

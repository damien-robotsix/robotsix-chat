"""Mill board API tools — ticket polling, PR merging, and ticket filing.

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

Also provides ``mark_ticket_ready(ticket_id)`` — a dedicated state-transition
tool that calls ``POST /tickets/{id}/mark-ready`` to force a stalled ticket
out of ``draft`` / ``human_issue_approval`` into ``ready``.

Also provides ``file_ticket(title, description, kind, repo_id)`` — a
dedicated ticket-creation tool that calls ``POST /tickets/ingest`` on
the mill board API to file a new ticket.  Use this when the user grants
autonomy and you identify a deferred improvement that should be tracked
as a ticket — it avoids recurring manual decisions.

Also provides ``prioritize_all_open_tickets()`` — a batch-prioritization tool
that lists all open tickets and sets priority on every one of them in a
single call.

Also provides ``list_stale_ready_tickets()`` — a queue-health monitoring tool
that surfaces tickets stuck in ``ready`` state beyond the configured
staleness threshold, enabling the agent to detect and escalate queue stalls.

Also provides ``resolve_repo(repo_id)`` — maps a mill ``repo_id`` to the
GitHub ``owner/repo`` full name via the mill's ``GET /repos`` registry, so
GitHub tools are never called with a guessed owner.

The mill state names live in :mod:`robotsix_chat.ticket_poll.mill_states`.

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

from robotsix_chat.repo.direct.board_client import BoardClient, parse_owner_repo
from robotsix_chat.ticket_poll.mill_states import (
    ACTIVE_WORK_STATES,
    MERGE_STATES,
    OPEN_STATES,
    TERMINAL_STATES,
    normalize_state,
)

if TYPE_CHECKING:
    from robotsix_chat.config import Settings

__all__ = [
    "build_file_ticket_tool",
    "build_find_ticket_by_pr_tool",
    "build_list_stale_ready_tickets_tool",
    "build_mark_ticket_done_tool",
    "build_mark_ticket_ready_tool",
    "build_merge_pull_request_tool",
    "build_prioritize_all_open_tickets_tool",
    "build_resolve_repo_tool",
    "build_ticket_poll_tools",
    "build_transition_ticket_tool",
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


def _extract_ingested_ticket_id(response_data: Any) -> str:
    r"""Extract the created ticket ID from a ``/tickets/ingest`` response.

    *response_data* may be a parsed JSON dict, a parsed JSON list, a
    raw JSON string, or a ``"HTTP <status>\n<body>"`` envelope string
    from ``component_request``.
    Returns the ticket ID string, or ``""`` when it cannot be found.
    """
    if isinstance(response_data, str):
        # component_request envelope: "HTTP <status>\n<body>"
        if response_data.startswith("HTTP "):
            try:
                newline = response_data.index("\n")
            except ValueError:
                return ""
            response_data = response_data[newline + 1 :]
        # Parse the (possibly enveloped) string as JSON.
        try:
            body = json.loads(response_data)
        except json.JSONDecodeError, ValueError:
            return ""
        return _extract_ingested_ticket_id(body)

    if isinstance(response_data, list) and response_data:
        response_data = response_data[0]

    if isinstance(response_data, dict):
        ticket_id = response_data.get("id") or response_data.get("ticket_id")
        if isinstance(ticket_id, str):
            return ticket_id
    return ""


def _component_response_is_error(resp: str) -> bool:
    r"""Return True when a ``component_request`` response indicates failure.

    ``component_request`` returns either ``Error: ...`` for early-exit
    failures (unknown component, empty roster, connection error) or
    ``HTTP <status>\n<body>`` for an actual HTTP response.  Success is any
    2xx/3xx status; 4xx/5xx responses (e.g. ``404 Not Found``,
    ``502 Bad Gateway``) are failures and should trigger the direct
    fallback rather than being surfaced to the agent as success.
    """
    if resp.startswith("Error:"):
        return True
    if resp.startswith("HTTP "):
        try:
            status = int(resp.split(" ", 2)[1])
        except IndexError, ValueError:
            return False
        return not 200 <= status < 400
    return False


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

    # The mill board returns tickets keyed ``id``; older/other boards used
    # ``ticket_id``.  Accepting only the latter made every resolution fail
    # with "(0 tickets listed)" against mill (live logs 2026-09-01).
    all_ids: list[str] = [
        tid
        for t in ticket_objects
        if isinstance(t, dict)
        for tid in (t.get("id") or t.get("ticket_id"),)
        if isinstance(tid, str) and tid
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
# Unexpected-terminal-state detection / delivery evidence
# ---------------------------------------------------------------------------

# Keywords in an event ``type`` / ``action`` / ``note`` that signal the
# ticket was worked on before it reached its terminal state.
_ACTIVITY_KEYWORDS = (
    "implement",
    "unblock",
    "resume",
    "merge",
    "approve",
    "complete",
    "retrospect",
    "pull request",
)


def _history_states(data: dict[str, Any]) -> list[str]:
    """Return the normalised state names found in *data*'s history.

    Accepts the ``history`` array (mill ``GET /tickets/{id}/history``
    rows, each carrying ``state``) or legacy entries that use ``to``.
    Non-dict entries are skipped.
    """
    history = data.get("history")
    if not isinstance(history, list):
        return []
    states: list[str] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        states.append(normalize_state(entry.get("state", entry.get("to", ""))))
    return states


def _has_activity_event(data: dict[str, Any]) -> bool:
    """Return True when any event/history note carries an activity keyword."""
    for key in ("events", "history"):
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = " ".join(
                str(row.get(field, "")) for field in ("type", "action", "note")
            ).lower()
            if any(keyword in text for keyword in _ACTIVITY_KEYWORDS):
                return True
    return False


def _delivery_evidence(data: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(delivered, note)`` for the ticket in *data*.

    *delivered* is True when the ticket is in a terminal state AND carries
    evidence that work shipped: a ``pr_url`` on the ticket, a merge-path
    state (``implement_complete`` / ``human_mr_approval`` /
    ``waiting_auto_merge`` / ``done``) in its history, or a ``done``
    transition before ``closed``.  *note* is a human-readable delivery
    summary (``None`` when nothing can be said).
    """
    state = normalize_state(data.get("state"))
    pr_url = data.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url.strip():
        pr_url = None
    history = _history_states(data)
    merged_path = any(prior in MERGE_STATES for prior in history)

    if state not in TERMINAL_STATES:
        return False, None
    if pr_url is None and not merged_path:
        return False, None

    pr_part = f"PR {pr_url}" if pr_url else "PR merged (see ticket history)"
    if state == "closed":
        return True, f"closed after delivery (retrospect): {pr_part}"
    if state == "done":
        return True, f"done — {pr_part} merged, awaiting retrospect"
    return True, pr_part


def _check_unexpected_terminal(data: dict[str, Any]) -> str | None:
    """Check whether *data* shows a ticket closed without any work.

    Returns a diagnostic string ONLY when the ticket is ``closed`` /
    ``done`` and:

      - it has no ``pr_url``, AND
      - its ``history`` shows no active-work state
        (:data:`~robotsix_chat.ticket_poll.mill_states.ACTIVE_WORK_STATES`)
        and no merge-path state
        (:data:`~robotsix_chat.ticket_poll.mill_states.MERGE_STATES`), AND
      - no event / history note mentions implement / merge / approve /
        unblock / resume / complete activity.

    That is the ``draft → closed`` (or ``ready → closed``) shape — a
    ticket dropped by a triage gate or closed by hand before an agent
    touched it.  The normal delivery path (``ready → code_review → … →
    implement_complete → … → done → closed``) never trips it.  Returns
    ``None`` in every other case, including when the data carries no
    history at all but does carry a ``pr_url``.

    This is a pure function — no I/O.  Callers supply the parsed JSON
    body from ``GET /tickets/{id}``, optionally augmented with the
    ``history`` rows from ``GET /tickets/{id}/history``.
    """
    state = normalize_state(data.get("state"))
    if state not in TERMINAL_STATES:
        return None

    delivered, _note = _delivery_evidence(data)
    if delivered:
        return None

    history = _history_states(data)
    if any(prior in ACTIVE_WORK_STATES or prior in MERGE_STATES for prior in history):
        return None
    if _has_activity_event(data):
        return None

    seen = [s for s in history if s and s != state]
    path = " → ".join([*seen, state]) if seen else state
    return (
        f"Ticket reached {state} without a PR and without ever entering an "
        f"active work state ({path}).  It was closed from a draft / "
        f"pre-implementation state — dropped, not delivered."
    )


def _delivery_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Return the delivery-evidence fields every poll result carries.

    ``pr_url`` (or ``None``), ``delivered`` (bool), ``delivery_note``
    (string or ``None``) and ``unexpected_terminal`` (string or ``None``).
    """
    delivered, note = _delivery_evidence(data)
    pr_url = data.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url.strip():
        pr_url = None
    return {
        "pr_url": pr_url,
        "delivered": delivered,
        "delivery_note": note,
        "unexpected_terminal": _check_unexpected_terminal(data),
    }


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
            if not _component_response_is_error(resp):
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


def build_mark_ticket_ready_tool(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``mark_ticket_ready`` tool.

    The tool calls the mill board's ``POST /tickets/{id}/mark-ready`` endpoint
    to force a ticket out of ``draft`` / ``human_issue_approval`` into the
    ``ready`` state — the manual nudge for tickets whose drafting/approval
    worker never picked them up.  It routes through *component_request*
    (roster-based connectivity) when available, falling back to the direct
    ``board_api_base_url`` otherwise.

    Use this only after confirming the ticket is genuinely stuck (still in
    ``draft`` with no event beyond ``created``) and only when the transition
    is authorized — for user-requested tickets at the operator's explicit
    request, or for a low-risk, reversible spec the operator has approved.

    Args:
        settings: Full application settings.
        component_request: The roster-based request callable, or ``None``
            when the component roster is unavailable.

    Returns:
        A one-element list containing the ``mark_ticket_ready`` async
        callable, or ``[]`` when neither *component_request* nor
        ``board_api_base_url`` are available.

    """
    conn = _board_connection(settings, component_request)
    if conn is None:
        return []
    board_url, board_token, timeout = conn

    async def mark_ticket_ready(
        ticket_id: str,
        justification: str = "",
    ) -> str:
        """Force a stalled draft ticket forward into the ``ready`` state.

        Calls the mill board's mark-ready endpoint to transition the given
        ticket out of ``draft`` / ``human_issue_approval``.  Use this when
        a monitored ticket remains stuck in ``draft`` with no event beyond
        ``created`` (the drafting/approval worker never picked it up), or
        to approve a user-requested ticket in the same turn it was filed.

        This is a state mutation — only call it when the transition is
        authorized: operator consent, a standing directive for the
        specific ticket / gate, or the auto-drive promotable-draft
        branch (subsessions.auto_drive_promote_ready_drafts ON).

        Args:
            ticket_id: The ticket ID to transition (e.g.
                ``"20250101T120000Z-my-ticket-a1b2"``).  Paraphrased /
                abbreviated IDs are resolved via hash-suffix or
                slug-substring match against the live board.
            justification: Optional human-readable reason for the forced
                transition, sent as the request body's ``justification``
                field for auditability.

        Returns:
            A status message from the mill API — success confirmation or
            an error describing why the transition failed.

        """
        # Resolve paraphrased / abbreviated IDs against the live board
        # before making the request.  This prevents 404 failures when an
        # ID was derived from narrative text rather than from a board API
        # response.
        resolved_map = await _resolve_ticket_ids(
            board_url, board_token, timeout, [ticket_id]
        )
        effective_id = resolved_map.get(ticket_id) or ticket_id

        path = f"/tickets/{effective_id}/mark-ready"
        json_body: dict[str, str] | None = (
            {"justification": justification} if justification else None
        )

        # Try component_request (roster-based) first.
        if component_request is not None:
            resp = await component_request(
                "mill",
                "POST",
                path,
                json_body=json_body,
            )
            if not _component_response_is_error(resp):
                return str(resp)
            logger.info(
                "mark_ticket_ready: roster path failed for %s; "
                "falling back to direct board API",
                effective_id,
            )

        # Direct fallback via board API.
        url = f"{board_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
                response = await retry_client.post(url, headers=headers, json=json_body)
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
                f"Error marking ticket {effective_id} ready: "
                f"board API request timed out after {timeout}s"
            )
        except Exception as exc:
            logger.warning(
                "mark_ticket_ready direct path failed for %s: %s",
                effective_id,
                exc,
            )
            return f"Error marking ticket {effective_id} ready: {exc}"

    return [mark_ticket_ready]


def build_transition_ticket_tool(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``transition_ticket`` tool.

    The tool calls the mill board's ``POST /tickets/{id}/transition``
    endpoint with an explicit target state and a mandatory rationale note.
    It exists for the master agent's approval duty on ``human_issue_approval``
    tickets (operator directive: no human in the approval loop): approve a
    sound spec to ``ready``, send a thin spec back to ``draft`` for
    re-refinement, or retire a duplicate/obsolete ticket via ``draft`` →
    ``closed`` (the state machine forbids ``human_issue_approval`` →
    ``closed`` directly).

    Args:
        settings: Full application settings.
        component_request: The roster-based request callable, or ``None``
            when the component roster is unavailable.

    Returns:
        A one-element list containing the ``transition_ticket`` async
        callable, or ``[]`` when neither *component_request* nor
        ``board_api_base_url`` are available.

    """
    conn = _board_connection(settings, component_request)
    if conn is None:
        return []
    board_url, board_token, timeout = conn

    allowed_states = ("ready", "draft", "closed")

    async def transition_ticket(
        ticket_id: str,
        state: str,
        note: str,
    ) -> str:
        """Transition a mill ticket to an explicit state, with a rationale.

        This is the approval-duty state mutation: when a ticket sits at
        ``human_issue_approval`` you review it yourself and act — never
        wait for a human and never spawn a subsession that merely waits.

        - ``state="ready"`` — the spec is actionable and consistent with
          robotsix-standards: this IS the approval.
        - ``state="draft"`` — the spec is thin, empty, or ambiguous: the
          note must say what is missing; classify/refine re-run and the
          healthy pipeline's auto-approve applies.
        - ``state="closed"`` — duplicate or obsolete. The state machine
          forbids ``human_issue_approval`` → ``closed`` directly: call
          this tool twice, first with ``state="draft"``, then with
          ``state="closed"``.

        Args:
            ticket_id: The ticket ID to transition. Paraphrased /
                abbreviated IDs are resolved against the live board.
            state: Target state — one of ``ready``, ``draft``, ``closed``.
            note: MANDATORY rationale recorded on the ticket (why it was
                approved / sent back / retired). Never empty.

        Returns:
            A status message from the mill API — success confirmation or
            an error describing why the transition failed.

        """
        if state not in allowed_states:
            return (
                f"Refusing transition: state {state!r} is not one of "
                f"{allowed_states} (other lifecycle moves have dedicated "
                "tools, e.g. mark_ticket_ready / merge-now)."
            )
        if not note.strip():
            return (
                "Refusing transition: a non-empty rationale note is "
                "required — record WHY the ticket is being approved, sent "
                "back to draft, or retired."
            )

        resolved_map = await _resolve_ticket_ids(
            board_url, board_token, timeout, [ticket_id]
        )
        effective_id = resolved_map.get(ticket_id) or ticket_id

        path = f"/tickets/{effective_id}/transition"
        json_body = {"state": state, "note": note}

        if component_request is not None:
            resp = await component_request(
                "mill",
                "POST",
                path,
                json_body=json_body,
            )
            if not _component_response_is_error(resp):
                return str(resp)
            logger.info(
                "transition_ticket: roster path failed for %s; "
                "falling back to direct board API",
                effective_id,
            )

        url = f"{board_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
                response = await retry_client.post(url, headers=headers, json=json_body)
                try:
                    body = response.json()
                    body_str = json.dumps(body)
                except Exception:
                    body_str = response.text
                return f"HTTP {response.status_code}\n{body_str}"
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException:
            return (
                f"Error transitioning ticket {effective_id}: "
                f"board API request timed out after {timeout}s"
            )
        except Exception as exc:
            logger.warning(
                "transition_ticket direct path failed for %s: %s",
                effective_id,
                exc,
            )
            return f"Error transitioning ticket {effective_id}: {exc}"

    return [transition_ticket]


def build_mark_ticket_done_tool(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``mark_ticket_done`` tool.

    The tool calls the mill board's ``POST /tickets/{id}/mark-done`` endpoint
    to transition a ticket to the terminal ``done`` state.  It routes through
    *component_request* (roster-based connectivity) when available, falling
    back to the direct ``board_api_base_url`` otherwise.

    Use this to close a superseded ticket, a duplicate, or any ticket whose
    work is confirmed complete via a terminal state on another ticket.  When
    a superseding ticket is already ``DONE`` / ``CLOSED``, use this tool
    rather than asking the operator to close it manually.

    Args:
        settings: Full application settings.
        component_request: The roster-based request callable, or ``None``
            when the component roster is unavailable.

    Returns:
        A one-element list containing the ``mark_ticket_done`` async
        callable, or ``[]`` when neither *component_request* nor
        ``board_api_base_url`` are available.

    """
    conn = _board_connection(settings, component_request)
    if conn is None:
        return []
    board_url, board_token, timeout = conn

    async def mark_ticket_done(
        ticket_id: str,
        justification: str = "",
    ) -> str:
        """Close a ticket by transitioning it to the terminal ``done`` state.

        Calls the mill board's mark-done endpoint to close the given ticket.
        Use this when a ticket is superseded by another ticket that is
        already ``DONE`` / ``CLOSED``, or when work on the ticket is
        confirmed complete.  Do NOT ask the operator to close tickets
        manually on the UI when this tool can do it.

        Args:
            ticket_id: The ticket ID to close (e.g.
                ``"20250101T120000Z-my-ticket-a1b2"``).  Paraphrased /
                abbreviated IDs are resolved via hash-suffix or
                slug-substring match against the live board.
            justification: Optional human-readable reason for the closure,
                sent as the request body's ``justification`` field for
                auditability.

        Returns:
            A status message from the mill API — success confirmation or
            an error describing why the transition failed.

        """
        # Resolve paraphrased / abbreviated IDs against the live board
        # before making the request.  This prevents 404 failures when an
        # ID was derived from narrative text rather than from a board API
        # response.
        resolved_map = await _resolve_ticket_ids(
            board_url, board_token, timeout, [ticket_id]
        )
        effective_id = resolved_map.get(ticket_id) or ticket_id

        path = f"/tickets/{effective_id}/mark-done"
        json_body: dict[str, str] | None = (
            {"justification": justification} if justification else None
        )

        # Try component_request (roster-based) first.
        if component_request is not None:
            resp = await component_request(
                "mill",
                "POST",
                path,
                json_body=json_body,
            )
            if not _component_response_is_error(resp):
                return str(resp)
            logger.info(
                "mark_ticket_done: roster path failed for %s; "
                "falling back to direct board API",
                effective_id,
            )

        # Direct fallback via board API.
        url = f"{board_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
                response = await retry_client.post(url, headers=headers, json=json_body)
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
                f"Error marking ticket {effective_id} done: "
                f"board API request timed out after {timeout}s"
            )
        except Exception as exc:
            logger.warning(
                "mark_ticket_done direct path failed for %s: %s",
                effective_id,
                exc,
            )
            return f"Error marking ticket {effective_id} done: {exc}"

    return [mark_ticket_done]


def build_find_ticket_by_pr_tool(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``find_ticket_by_pr`` tool.

    The tool looks up a ticket by its linked PR URL via the mill board
    API.  It calls ``GET /tickets?pr_url=...`` (server-side filter) when
    the board supports it, falling back to a client-side scan of the full
    ticket list otherwise.

    Use this when you know a PR URL and need to find the associated ticket
    — e.g. when asked to "verify and merge PR #656" but don't know the
    ticket ID.  It replaces manual enumeration of all tickets.

    Args:
        settings: Full application settings.
        component_request: The roster-based request callable, or ``None``
            when the component roster is unavailable.

    Returns:
        A one-element list containing the ``find_ticket_by_pr`` async
        callable, or ``[]`` when neither *component_request* nor
        ``board_api_base_url`` are available.

    """
    conn = _board_connection(settings, component_request)
    if conn is None:
        return []

    async def find_ticket_by_pr(pr_url: str) -> str:
        """Find the ticket associated with a pull request URL.

        Looks up the mill board for a ticket whose ``pr_url`` field
        matches *pr_url*.  Returns the ticket ID and state, or an error
        when no match is found or the board API is unreachable.

        Args:
            pr_url: The full PR URL (e.g.
                ``"https://github.com/owner/repo/pull/656"``).

        Returns:
            A JSON string with ``ticket_id``, ``state``, ``pr_url``, and
            ``error`` (empty on success, or a diagnostic message).

        """
        board_client = BoardClient(settings.direct_repo)
        ticket = await board_client.find_ticket_by_pr_url(pr_url)
        if ticket is None:
            return json.dumps(
                {
                    "ticket_id": None,
                    "state": None,
                    "pr_url": pr_url,
                    "error": (
                        f"No ticket found with pr_url={pr_url!r}. "
                        "The PR may not be linked to any board ticket, "
                        "or the board API may be unreachable."
                    ),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ticket_id": ticket.get("ticket_id"),
                "state": ticket.get("state"),
                "pr_url": ticket.get("pr_url"),
                "error": "",
            },
            ensure_ascii=False,
        )

    return [find_ticket_by_pr]


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


def build_file_ticket_tool(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``file_ticket`` tool.

    The tool calls the mill board's ``POST /tickets/ingest`` endpoint to
    file a new ticket.  It routes through *component_request*
    (roster-based connectivity) when available, falling back to the
    direct ``board_api_base_url`` otherwise.

    Use this tool to create a ticket for a deferred improvement or
    follow-up task — especially when the user has granted autonomy and
    the improvement would prevent recurring manual decisions.  The tool
    sends a single ``TicketIngest`` object — mill's ingest endpoint
    takes one ticket per call, not a list — and returns the created
    ticket's ID on success.

    Args:
        settings: Full application settings.
        component_request: The roster-based request callable, or ``None``
            when the component roster is unavailable.

    Returns:
        A one-element list containing the ``file_ticket`` async callable,
        or ``[]`` when neither *component_request* nor
        ``board_api_base_url`` are available.

    """
    conn = _board_connection(settings, component_request)
    if conn is None:
        return []
    board_url, board_token, timeout = conn

    async def file_ticket(
        title: str,
        description: str = "",
        kind: str = "task",
        repo_id: str = "",
    ) -> str:
        """File a new ticket on the mill board.

        Creates a ticket via ``POST /tickets/ingest``.  Routes through
        the component roster when available, falling back to the direct
        board API.

        Use this to capture a deferred improvement, follow-up task, or
        any actionable item you identify during a session — especially
        when the user has granted autonomy.  Always mention the filed
        ticket in your final summary so the user is aware.

        Args:
            title: Short, specific ticket title (required).
            description: Detailed ticket body / acceptance criteria.
            kind: Ticket kind — one of ``"task"``, ``"prompt"``,
                ``"bug"``, ``"epic"``.  Defaults to ``"task"``.
            repo_id: Target repository id (e.g. ``"robotsix-chat"``),
                as listed by ``GET /repos``.  Required — mill's ingest
                endpoint has no default repo and rejects an empty or
                missing value.

        Returns:
            A JSON string with ``ticket_id`` (the created ticket's ID)
            on success, or ``error`` with a diagnostic message on
            failure.

        """
        # repo_id is required by mill's TicketIngest model: omitting it
        # is a 422 and an empty string is a 404 ("Unknown repo_id: ''").
        # Fail here with something the agent can act on instead.
        if not repo_id:
            return json.dumps(
                {
                    "ticket_id": "",
                    "error": (
                        "repo_id is required — pass a registered repo id "
                        "(see GET /repos). The board has no default repo."
                    ),
                },
                ensure_ascii=False,
            )

        # Pre-validate repo_id against the board's registered repos.
        # This catches misfiled tickets early — e.g. passing a repo_id
        # that belongs to a different board — with a clear, actionable
        # error instead of letting the board API reject with a generic
        # 404 or, worse, silently accepting and later closing as
        # misfiled.  Best-effort: when the repo list cannot be fetched
        # (network error, timeout), the check is skipped and the filing
        # proceeds — the board API's own 404 is the fallback guard.
        board_repo_ids = await _fetch_board_repo_ids(
            board_url, board_token, timeout, component_request
        )
        if board_repo_ids is not None and repo_id not in board_repo_ids:
            available = ", ".join(sorted(board_repo_ids)) or "(none)"
            return json.dumps(
                {
                    "ticket_id": "",
                    "error": (
                        f"repo_id {repo_id!r} is not registered on this "
                        f"board.  Available repos: {available}.  Verify "
                        "the repo_id matches one listed by GET /repos on "
                        "the target board, or check that the agent is "
                        "connected to the correct board."
                    ),
                },
                ensure_ascii=False,
            )

        # Build the description body with a metadata footer matching the
        # format expected by the mill's /tickets/ingest parser.
        body_lines: list[str] = [description]
        body_lines.append("")
        body_lines.append(f"--- kind: {kind} | source: agent | origin: robotsix-chat")
        body = "\n".join(body_lines)

        # One ticket per call: mill's /tickets/ingest takes a single
        # TicketIngest object.  Wrapping it in a list made every call a
        # 422, so this tool could never file a ticket.
        ingest_payload: dict[str, Any] = {
            "repo_id": repo_id,
            "title": title,
            "body": body,
            "source_tag": "robotsix-chat-tool",
        }

        # Try component_request (roster-based) first.
        if component_request is not None:
            resp = await component_request(
                "mill",
                "POST",
                "/tickets/ingest",
                json_body=ingest_payload,
            )
            if not _component_response_is_error(resp):
                ticket_id = _extract_ingested_ticket_id(resp)
                if not ticket_id:
                    return json.dumps(
                        {
                            "ticket_id": "",
                            "error": (
                                "Board API accepted the ticket but the "
                                "response did not contain a ticket id. "
                                f"Raw response: {resp!s:.500}"
                            ),
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {"ticket_id": ticket_id, "error": ""},
                    ensure_ascii=False,
                )
            logger.info(
                "file_ticket: roster path failed; falling back to direct board API"
            )

        # Direct fallback via board API.
        url = f"{board_url}/tickets/ingest"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
                response = await retry_client.post(
                    url, headers=headers, json=ingest_payload
                )
                try:
                    resp_body = response.json()
                except Exception:
                    resp_body = response.text
                ticket_id = _extract_ingested_ticket_id(resp_body)
                if not ticket_id and 200 <= response.status_code < 300:
                    return json.dumps(
                        {
                            "ticket_id": "",
                            "error": (
                                f"Board API returned HTTP {response.status_code} "
                                "but the response did not contain a ticket id. "
                                f"Raw response: {str(resp_body)[:500]}"
                            ),
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {"ticket_id": ticket_id, "error": ""},
                    ensure_ascii=False,
                )
        except httpx.HTTPStatusError as exc:
            try:
                resp_body = exc.response.json()
            except Exception:
                resp_body = exc.response.text
            ticket_id = _extract_ingested_ticket_id(resp_body)
            return json.dumps(
                {
                    "ticket_id": ticket_id,
                    "error": f"Board API returned HTTP {exc.response.status_code}",
                },
                ensure_ascii=False,
            )
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException:
            return json.dumps(
                {
                    "ticket_id": "",
                    "error": f"Board API request timed out after {timeout}s",
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.warning(
                "file_ticket direct path failed for %r: %s",
                title[:80],
                exc,
            )
            return json.dumps(
                {"ticket_id": "", "error": str(exc)},
                ensure_ascii=False,
            )

    return [file_ticket]


def build_resolve_repo_tool(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``resolve_repo`` tool.

    Maps a mill ``repo_id`` (e.g. ``"robotsix-central-deploy"``) to the
    GitHub ``owner/repo`` full name using the mill's ``GET /repos``
    registry — the entry's ``forge_remote_url`` (older mills: ``git_url``)
    is parsed for its last two path components.  Never guesses an owner:
    when the registry has no match the tool says so and lists the known
    repo ids.

    Returns ``[]`` when the board API is not configured.
    """
    conn = _board_connection(settings, component_request)
    if conn is None:
        return []
    board_client = BoardClient(settings.direct_repo)

    async def _registry() -> list[dict[str, Any]] | None:
        if component_request is not None:
            resp = await component_request("mill", "GET", "/repos")
            if isinstance(resp, str) and resp.startswith("HTTP "):
                try:
                    status_code = int(resp.split(maxsplit=2)[1])
                    body_str = resp[resp.index("\n") + 1 :]
                except IndexError, ValueError:
                    status_code, body_str = 0, ""
                if status_code and status_code < 400:
                    rows, parse_error = _parse_json_body(body_str)
                    if not parse_error and isinstance(rows, list):
                        return [r for r in rows if isinstance(r, dict)]
        return await board_client.list_repos()

    async def resolve_repo(repo_id: str = "", query: str = "") -> str:
        """Map a mill ticket ``repo_id`` to its GitHub ``owner/repo`` full name.

        Use this BEFORE calling any GitHub tool (``list_open_prs``,
        ``fetch_repo_for_study``, PR inspection …) with a repository you
        only know by its mill ``repo_id`` (the ``repo_id`` / ``board_id``
        field on a ticket, e.g. ``"robotsix-central-deploy"``).  The GitHub
        account is NOT an organisation named after the fleet — never guess
        ``"<fleet>/<repo>"``; read the owner from this tool's answer.

        Args:
            repo_id: A mill repo id (``"robotsix-chat"``), a board id, or an
                already-qualified ``owner/repo`` (returned as-is).
            query: Alias for ``repo_id`` — pass one of the two.

        Returns:
            A JSON string: ``{"repo_id": ..., "full_name": "owner/repo",
            "owner": ..., "repo": ..., "forge_remote_url": ..., "error": ""}``
            — or ``full_name: null`` with an ``error`` and the list of
            ``known_repo_ids`` when the id is not in the mill registry.

        """
        # ``query`` is what agents guess when they haven't seen the schema
        # (live incident 2026-09-05); accept it instead of burning a turn.
        repo_id = repo_id or query
        if not repo_id:
            return json.dumps(
                {
                    "repo_id": "",
                    "full_name": None,
                    "error": "pass the mill repo id as repo_id",
                },
                ensure_ascii=False,
            )
        wanted = repo_id.strip()
        if "/" in wanted or "://" in wanted or ":" in wanted:
            full = parse_owner_repo(wanted)
            if full is None:
                return json.dumps(
                    {
                        "repo_id": repo_id,
                        "full_name": None,
                        "error": f"Could not parse an owner/repo from {repo_id!r}",
                    },
                    ensure_ascii=False,
                )
            owner, name = full.split("/", 1)
            return json.dumps(
                {
                    "repo_id": repo_id,
                    "full_name": full,
                    "owner": owner,
                    "repo": name,
                    "forge_remote_url": None,
                    "error": "",
                },
                ensure_ascii=False,
            )

        rows = await _registry()
        if rows is None:
            return json.dumps(
                {
                    "repo_id": repo_id,
                    "full_name": None,
                    "error": "Mill repo registry (GET /repos) unreachable",
                },
                ensure_ascii=False,
            )
        known: list[str] = []
        match: dict[str, Any] | None = None
        by_name: list[dict[str, Any]] = []
        for row in rows:
            rid = str(row.get("repo_id", ""))
            if rid:
                known.append(rid)
            url = row.get("forge_remote_url") or row.get("git_url")
            full = parse_owner_repo(url if isinstance(url, str) else None)
            if full is None:
                continue
            if rid.lower() == wanted.lower() or (
                str(row.get("board_id", "")).lower() == wanted.lower()
            ):
                match = {**row, "_full": full}
                break
            if full.rsplit("/", 1)[1].lower() == wanted.lower():
                by_name.append({**row, "_full": full})
        if match is None and len(by_name) == 1:
            match = by_name[0]
        if match is None:
            return json.dumps(
                {
                    "repo_id": repo_id,
                    "full_name": None,
                    "known_repo_ids": sorted(known),
                    "error": (
                        f"{repo_id!r} is not a registered mill repo id; "
                        "pass one of known_repo_ids or an explicit owner/repo"
                    ),
                },
                ensure_ascii=False,
            )
        full = str(match["_full"])
        owner, name = full.split("/", 1)
        return json.dumps(
            {
                "repo_id": str(match.get("repo_id", repo_id)),
                "full_name": full,
                "owner": owner,
                "repo": name,
                "forge_remote_url": match.get("forge_remote_url")
                or match.get("git_url"),
                "error": "",
            },
            ensure_ascii=False,
        )

    return [resolve_repo]


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

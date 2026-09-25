"""Pure private helper functions for the ticket-poll tools.

These behaviour-preserving helpers were extracted verbatim from
:mod:`robotsix_chat.ticket_poll` to keep the package's ``__init__`` module
manageable.  They perform no I/O beyond parsing/inspecting already-fetched
JSON payloads and computing board connection parameters, so they are safe to
import without triggering side effects.  ``__init__`` re-imports every name
defined here, so all existing internal references keep resolving unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from robotsix_chat.ticket_poll.mill_states import (
    ACTIVE_WORK_STATES,
    MERGE_STATES,
    TERMINAL_STATES,
    normalize_state,
)

if TYPE_CHECKING:
    from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)


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


def _response_status(resp: str) -> int | None:
    """Return the HTTP status code from a ``"HTTP <code> ..."`` response string."""
    match = re.match(r"\s*HTTP\s+(\d{3})", resp or "")
    return int(match.group(1)) if match else None


def _is_classifying_conflict(resp: str) -> bool:
    """Return whether *resp* is mill's ``409 classifying -> ready`` refusal."""
    return _response_status(resp) == 409 and "classifying" in (resp or "").lower()


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


def _match_candidates(
    candidate_ids: list[str], all_ids: list[str]
) -> dict[str, str | None]:
    """Match each candidate against *all_ids* (exact → hash suffix → slug)."""
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

        result[cid] = None

    return result


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

"""State-mutating ticket_poll tools: merge, transition, mark ready/done, file."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
from robotsix_http import RetryClient

from robotsix_chat.ticket_poll._internal import (
    _CLASSIFY_POLL_SECONDS,
    _CLASSIFY_WAIT_SECONDS,
    _TICKET_POLL_RETRY_CONFIG,
    _fetch_board_repo_ids,
    _resolve_ticket_ids,
)
from robotsix_chat.ticket_poll.helpers import (
    _board_connection,
    _component_response_is_error,
    _extract_ingested_ticket_id,
    _is_classifying_conflict,
)

if TYPE_CHECKING:
    from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)


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

    The tool calls the mill board's ``POST /tickets/{id}/transition``
    endpoint with ``{"state": "ready"}`` to force a ticket out of ``draft``
    / ``human_issue_approval`` into the ``ready`` state — the manual nudge
    for tickets whose drafting/approval worker never picked them up.  It
    routes through *component_request* (roster-based connectivity) when
    available, falling back to the direct ``board_api_base_url`` otherwise.

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
        note: str = "",
    ) -> str:
        """Force a stalled draft ticket forward into the ``ready`` state.

        Calls the mill board's generic transition endpoint (state
        ``ready``) to move the ticket out of ``draft`` /
        ``human_issue_approval``.  Use this when
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
                transition, recorded as the transition ``note`` on the
                ticket's history for auditability.
            note: Alias of ``justification`` — the name the mill API and
                the drain prompts use; models pass it habitually (3 times per
                drain run on 2026-09-14) and the claude_sdk schema check
                rejected the call before the body ran.  When both are
                given, ``justification`` wins.

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

        # Mill has no ``mark-ready`` route (every call 404'd — 3 wasted turns
        # per approval on 2026-09-07 alone). The real primitive is the generic
        # state transition: ``draft`` and ``human_issue_approval`` both allow
        # ``ready``, and the route enqueues the ticket for implement.
        path = f"/tickets/{effective_id}/transition"
        json_body: dict[str, str] | None = {
            "state": "ready",
            "note": justification
            or note
            or "marked ready via chat (mark_ticket_ready)",
        }
        headers: dict[str, str] = {"Accept": "application/json"}
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"

        async def _send_transition() -> str:
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
                if _is_classifying_conflict(str(resp)):
                    # A real answer from mill, not a connectivity failure —
                    # the direct path would only repeat the same 409.
                    return str(resp)
                logger.info(
                    "mark_ticket_ready: roster path failed for %s; "
                    "falling back to direct board API",
                    effective_id,
                )

            # Direct fallback via board API.
            url = f"{board_url}{path}"
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
                    response = await retry_client.post(
                        url, headers=headers, json=json_body
                    )
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

        async def _current_state() -> str | None:
            """Return the ticket's current state, or ``None`` when unreadable."""
            ticket_path = f"/tickets/{effective_id}"
            raw: str | None = None
            if component_request is not None:
                resp = await component_request("mill", "GET", ticket_path)
                if not _component_response_is_error(resp):
                    raw = str(resp)
            if raw is None:
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        response = await client.get(
                            f"{board_url}{ticket_path}", headers=headers
                        )
                        if response.status_code == 200:
                            raw = response.text
                except Exception as exc:
                    logger.debug(
                        "mark_ticket_ready: state read failed for %s: %s",
                        effective_id,
                        exc,
                    )
                    return None
            if raw is None:
                return None
            brace = raw.find("{")
            try:
                data = json.loads(raw[brace:] if brace >= 0 else raw)
            except ValueError, TypeError:
                return None
            state = data.get("state") if isinstance(data, dict) else None
            return str(state).lower() if state else None

        resp = await _send_transition()
        if not _is_classifying_conflict(resp):
            return resp

        # Freshly filed ticket, still being classified: wait (bounded) for
        # the worker to move it to draft, then retry once.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CLASSIFY_WAIT_SECONDS
        logger.info(
            "mark_ticket_ready: %s is still classifying — waiting up to %.0fs",
            effective_id,
            _CLASSIFY_WAIT_SECONDS,
        )
        while loop.time() < deadline:
            await asyncio.sleep(_CLASSIFY_POLL_SECONDS)
            state = await _current_state()
            if state is not None and state != "classifying":
                return await _send_transition()
        return (
            f"Ticket {effective_id} is still in mill's `classifying` state after "
            f"{_CLASSIFY_WAIT_SECONDS:.0f}s (the worker is busy). Mill only allows "
            "classifying -> draft; `ready` is legal from draft / "
            "human_issue_approval, so the transition cannot be forced yet. Do "
            "not retry in this turn: the ticket's wait_for_event monitor wakes "
            "when classification completes — have it call mark_ticket_ready "
            "then (spawn one with that instruction if none is tracking the "
            "ticket)."
        )

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

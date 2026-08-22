"""Epic decomposition tool for the chat agent.

Provides :func:`build_decompose_epic_tool` — a factory returning the
``decompose_epic`` LLM tool.  The tool fetches epic ticket data, its
history/events, and existing children from the mill board API, then
returns a structured analysis that the agent can use to create
appropriately scoped, dependency-ordered child tickets.

Also exposes :func:`load_epic_skill` which returns the component
skill markdown for injection into the agent instruction.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from robotsix_http import RetryClient, RetryConfig

from robotsix_chat.repo.direct.board_client import BoardClient

if TYPE_CHECKING:
    from robotsix_chat.config import Settings

__all__ = [
    "build_decompose_epic_tool",
    "load_epic_skill",
]

logger = logging.getLogger(__name__)

_EPIC_RETRY_CONFIG = RetryConfig(
    max_retries=2,
    backoff_base=1.0,
    backoff_cap=10.0,
    jitter_factor=0.5,
)


def load_epic_skill() -> str:
    """Return the epic-decomposition component skill markdown.

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
    """Return ``(board_url, board_token, timeout)`` or ``None`` if unavailable."""
    board_url = settings.direct_repo.board_api_base_url.strip()
    if not component_request and not board_url:
        return None
    board_url = board_url.rstrip("/") if board_url else ""
    board_token = settings.direct_repo.board_api_token.get_secret_value()
    timeout = settings.direct_repo.timeout
    return board_url, board_token, timeout


def build_decompose_epic_tool(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``decompose_epic`` tool.

    The tool fetches an epic ticket's data, history, and existing children
    from the mill board API, then returns a structured analysis so the
    agent can create appropriately scoped, dependency-ordered child tickets.

    Routes through *component_request* (roster-based connectivity) when
    available, falling back to the direct ``board_api_base_url`` otherwise.

    Args:
        settings: Full application settings.
        component_request: The roster-based request callable, or ``None``
            when the component roster is unavailable.

    Returns:
        A one-element list containing the ``decompose_epic`` async callable,
        or ``[]`` when neither *component_request* nor ``board_api_base_url``
        are available.

    """
    conn = _board_connection(settings, component_request)
    if conn is None:
        return []
    board_url, board_token, timeout = conn

    board_client = BoardClient(settings.direct_repo)

    async def _list_epic_children(epic_id: str) -> tuple[list[dict[str, Any]], str]:
        """Fetch tickets whose ``parent_id`` matches *epic_id*.

        Calls ``GET /tickets/{epic_id}/children`` on the board API via
        the roster-first path, falling back to the direct board API.
        """
        if component_request is not None:
            path = f"/tickets/{epic_id}/children"
            resp = await component_request("mill", "GET", path)
            if not resp.startswith("Error:"):
                try:
                    newline = resp.index("\n")
                    status_line = resp[:newline]
                    body_str = resp[newline + 1 :]
                except ValueError:
                    pass
                else:
                    if status_line.startswith("HTTP "):
                        try:
                            status_code = int(status_line.split()[1])
                        except IndexError, ValueError:
                            pass
                        else:
                            if status_code < 400:
                                try:
                                    data = json.loads(body_str)
                                except json.JSONDecodeError:
                                    pass
                                else:
                                    if isinstance(data, list):
                                        return data, ""
                                    if isinstance(data, dict):
                                        tickets = data.get("tickets", [])
                                        if isinstance(tickets, list):
                                            return tickets, ""
            logger.info(
                "decompose_epic: roster path for children failed; "
                "falling back to direct board API"
            )

        url = f"{board_url}/tickets/{epic_id}/children"
        headers: dict[str, str] = {"Accept": "application/json"}
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                retry_client = RetryClient(client, config=_EPIC_RETRY_CONFIG)
                response = await retry_client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning(
                "decompose_epic: failed to list children for epic %s: %s",
                epic_id,
                exc,
            )
            return [], f"Failed to list epic children: {exc}"

        if isinstance(data, list):
            return data, ""
        if isinstance(data, dict):
            tickets = data.get("tickets", [])
            if isinstance(tickets, list):
                return tickets, ""
        return [], ""

    async def decompose_epic(epic_ticket_id: str) -> str:
        """Analyze an epic and return a decomposition plan for child tickets.

        Fetches the epic ticket data, its event history, implement-cycle
        counts, and existing child tickets from the mill board API.  Returns
        a structured JSON analysis that includes:

        - The epic's title, description, state, and event history summary.
        - How many implement cycles ran (and whether the spawn limit was
          exhausted).
        - Existing child tickets (id, title, state).
        - A ``decomposition_plan`` section listing the concrete, dependency-
          ordered child tickets that should be created, each with a title,
          one-sentence scope, a suggested ``kind`` (``"task"`` or
          ``"investigation"``), and a dependency note.

        Use this tool when a monitor reports that the implement agent
        exhausted its spawn attempts on an epic — it replaces the manual
        "list options and wait for approval" loop with a concrete,
        actionable decomposition plan.  After calling this tool, create
        the child tickets via ``component_request("mill", "POST",
        "/tickets/ingest", ...)`` in the order given by the plan.

        Args:
            epic_ticket_id: The epic ticket identifier (full ID, e.g.
                ``"20250101T120000Z-my-epic-a1b2"``).

        Returns:
            A JSON string with ``epic_ticket_id``, ``epic_title``,
            ``epic_state``, ``epic_events_summary``,
            ``implement_cycles``, ``spawn_exhausted`` (boolean),
            ``existing_children`` (list of ``{id, title, state}``),
            ``decomposition_plan`` (list of suggested child tickets —
            may be empty when the epic already has children covering
            its scope), and ``error`` (empty on success).

        """
        # 1. Fetch epic ticket data.
        epic_data, fetch_error = await board_client._fetch_ticket(
            epic_ticket_id, "epic data"
        )
        if epic_data is None:
            return json.dumps(
                {
                    "epic_ticket_id": epic_ticket_id,
                    "error": fetch_error or "Failed to fetch epic ticket data",
                },
                ensure_ascii=False,
            )

        epic_title = epic_data.get("title", "(untitled)")
        epic_state = epic_data.get("state", "unknown")
        epic_kind = epic_data.get("kind", "")
        epic_description = epic_data.get("description", "")

        # Warn if the ticket is not actually an epic.
        if epic_kind.lower() != "epic":
            logger.info(
                "decompose_epic: ticket %s has kind=%r, not 'epic'",
                epic_ticket_id,
                epic_kind,
            )

        # 2. Extract event history.
        events = epic_data.get("events", [])
        if not isinstance(events, list):
            events = []
        events_summary = _summarize_events(events)

        # 3. Count implement cycles.
        from robotsix_chat.repo.direct.client import _count_cycles_from_data

        implement_cycles = _count_cycles_from_data(epic_data)
        spawn_exhausted = implement_cycles >= 3

        # 4. List existing children.
        children, children_error = await _list_epic_children(epic_ticket_id)
        existing_children = [
            {
                "id": child.get("ticket_id", child.get("id", "")),
                "title": child.get("title", "(untitled)"),
                "state": child.get("state", "unknown"),
            }
            for child in children
            if isinstance(child, dict)
        ]

        # 5. Build the decomposition plan.
        #
        # The plan is a skeleton — the LLM (chat agent) is expected to
        # read the epic description, event history, and existing children,
        # then populate concrete child tickets.  We provide structural
        # guidance (one child per acceptance criterion / subsystem) but
        # leave the creative work to the LLM.
        plan: list[dict[str, str]] = []
        if not existing_children and spawn_exhausted and epic_description:
            plan.append(
                {
                    "title": "[TITLE] — scope this child to ONE acceptance criterion",
                    "scope": (
                        "Extract the first self-contained deliverable from "
                        "the epic description.  Keep it small enough that a "
                        "single implement cycle (≤ 3 spawn attempts) can "
                        "complete it."
                    ),
                    "kind": "task",
                    "depends_on": "(none — first child)",
                }
            )
            plan.append(
                {
                    "title": "[TITLE] — scope this child to the NEXT criterion",
                    "scope": (
                        "Extract the second deliverable.  If it depends on "
                        "the first child completing, note that in depends_on."
                    ),
                    "kind": "task",
                    "depends_on": "(fill in the first child's title)",
                }
            )

        result: dict[str, Any] = {
            "epic_ticket_id": epic_ticket_id,
            "epic_title": epic_title,
            "epic_state": epic_state,
            "epic_kind": epic_kind,
            "epic_description": epic_description[:2000] if epic_description else "",
            "epic_events_summary": events_summary,
            "implement_cycles": implement_cycles,
            "spawn_exhausted": spawn_exhausted,
            "existing_children": existing_children,
            "existing_children_error": children_error,
            "decomposition_plan": plan,
            "error": "",
        }

        return json.dumps(result, ensure_ascii=False)

    return [decompose_epic]


def _summarize_events(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Extract a compact summary of the most relevant events.

    Returns a list of ``{type, timestamp, detail}`` dicts, limited to
    the last 20 events and focused on implement/spawn/state-transition
    events.
    """
    relevant: list[dict[str, str]] = []
    for ev in events[-20:]:
        if not isinstance(ev, dict):
            continue
        ev_type = ev.get("type", ev.get("action", ev.get("event", "")))
        if not isinstance(ev_type, str):
            ev_type = str(ev_type)
        ts = ev.get("timestamp", ev.get("created_at", ev.get("ts", "")))
        if not isinstance(ts, str):
            ts = str(ts)
        detail = ""
        # Capture state transitions.
        if "state" in ev:
            detail = f"state={ev['state']}"
        elif "from_state" in ev and "to_state" in ev:
            detail = f"{ev['from_state']} → {ev['to_state']}"
        # Capture implement/spawn exhaustion signals.
        for key in ("reason", "message", "error", "note"):
            if key in ev and isinstance(ev[key], str) and ev[key].strip():
                snippet = ev[key].strip()[:120]
                detail = f"{detail}; {key}={snippet}" if detail else snippet
                break
        relevant.append(
            {
                "type": ev_type,
                "timestamp": ts,
                "detail": detail,
            }
        )
    return relevant

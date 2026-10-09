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
tool that calls ``POST /tickets/{id}/transition`` (state ``ready``) to force a
stalled ticket out of ``draft`` / ``human_issue_approval`` into ``ready``.

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

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robotsix_chat.repo.direct.board_client import BoardClient, parse_owner_repo
from robotsix_chat.ticket_poll.helpers import (
    _board_connection,
    _parse_json_body,
)
from robotsix_chat.ticket_poll.helpers import (
    _check_unexpected_terminal as _check_unexpected_terminal,
)
from robotsix_chat.ticket_poll.helpers import (
    _delivery_fields as _delivery_fields,
)
from robotsix_chat.ticket_poll.ticket_poll_queue import (
    build_list_stale_ready_tickets_tool,
    build_prioritize_all_open_tickets_tool,
)
from robotsix_chat.ticket_poll.ticket_poll_read import build_ticket_poll_tools
from robotsix_chat.ticket_poll.ticket_poll_write import (
    build_file_ticket_tool,
    build_mark_ticket_done_tool,
    build_mark_ticket_ready_tool,
    build_merge_pull_request_tool,
    build_transition_ticket_tool,
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


# Keywords in an event ``type`` / ``action`` / ``note`` that signal the
# ticket was worked on before it reached its terminal state.
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

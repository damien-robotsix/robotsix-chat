"""Poll-based BLOCKED state transition detection and diagnostic capture.

:class:`DiagnosticCapture` polls the board for tickets that have transitioned
to BLOCKED, diffs against previously-known states, and records diagnostic
bundles in the :class:`DiagnosticStore`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.board_reader.client import BoardReader
    from robotsix_chat.diagnostics.store import DiagnosticRecord, DiagnosticStore

logger = logging.getLogger(__name__)

# Pattern to extract Langfuse trace URLs from ticket history comments.
# Example: 🔍 [Trace: abc123](https://langfuse.robotsix.net/trace/abc123)
_TRACE_PATTERN = re.compile(
    r"\[Trace:\s*([^\]]+)\]\(https://langfuse\.robotsix\.net[^)]*\)"
)


class DiagnosticCapture:
    """Poll the board for BLOCKED ticket transitions and capture diagnostics.

    Compares each ticket's current state against the *last-known state* stored
    in the :class:`DiagnosticStore`.  When a ticket transitions **to** BLOCKED,
    the capture fetches full ticket details, extracts diagnostic data, and
    creates a :class:`DiagnosticRecord`.
    """

    def __init__(
        self,
        board_reader: BoardReader,
        store: DiagnosticStore,
        *,
        repo_id: str = "",
    ) -> None:
        """Create a diagnostic capture instance.

        Args:
            board_reader: The board API client used to list and read tickets.
            store: The diagnostic store for persisting records and known states.
            repo_id: Board repo identifier to scope polling to. Empty string
                means all repos.

        """
        self._board = board_reader
        self._store = store
        self._repo_id = repo_id

    async def poll(self) -> list[DiagnosticRecord]:
        """Poll the board for newly-BLOCKED tickets and capture diagnostics.

        Returns the list of newly-created :class:`DiagnosticRecord` entries
        (empty when no new BLOCKED transitions are detected).
        """
        # 1. Fetch all BLOCKED tickets
        raw = await self._board.list_tickets(
            repo_id=self._repo_id or "all",
            include_closed=False,
            state="blocked",
        )
        blocked_tickets = self._parse_ticket_list(raw)

        # 2. Check each BLOCKED ticket against known state
        new_records: list[DiagnosticRecord] = []
        for ticket_summary in blocked_tickets:
            ticket_id = ticket_summary.get("id", "")
            if not ticket_id:
                continue

            # Already captured? Skip.
            if self._store.has_ticket(ticket_id):
                continue

            # Has the state actually changed to BLOCKED?
            current_state = ticket_summary.get("state", "").upper()
            last_state = self._store.get_known_state(ticket_id)
            if last_state is not None and last_state.upper() == current_state:
                # No transition — already known BLOCKED but no record yet?
                # Capture anyway (first detection fallback).
                pass

            # 3. Fetch full ticket details
            record = await self._capture_ticket(ticket_id)
            if record is not None:
                self._store.add(record)
                new_records.append(record)

            # 4. Update known state
            self._store.set_known_state(ticket_id, current_state)

        # 5. Also update known states for non-BLOCKED tickets we're tracking
        # so we detect future transitions. Fetch all open tickets.
        raw_open = await self._board.list_tickets(
            repo_id=self._repo_id or "all",
            include_closed=False,
        )
        open_tickets = self._parse_ticket_list(raw_open)
        for ticket_summary in open_tickets:
            ticket_id = ticket_summary.get("id", "")
            state = ticket_summary.get("state", "")
            if ticket_id and state:
                self._store.set_known_state(ticket_id, state)

        return new_records

    async def _capture_ticket(self, ticket_id: str) -> DiagnosticRecord | None:
        """Fetch full ticket details and build a :class:`DiagnosticRecord`."""
        from robotsix_chat.diagnostics.store import DiagnosticRecord

        raw = await self._board.get_ticket(ticket_id)
        ticket_data = self._parse_json(raw)

        if ticket_data is None or not isinstance(ticket_data, dict):
            logger.warning("Could not parse ticket data for %s", ticket_id)
            return None

        # Extract the five diagnostic fields
        block_reason = self._extract_block_reason(ticket_data)
        langfuse_trace = self._extract_langfuse_trace(ticket_data)
        ticket_history = json.dumps(ticket_data, indent=2)
        branch_pr_links = self._extract_branch_pr_links(ticket_data)
        clone_repo_info = self._extract_clone_repo_info(ticket_data)

        now = datetime.now(UTC).isoformat()
        return DiagnosticRecord(
            ticket_id=ticket_id,
            block_reason=block_reason,
            langfuse_trace=langfuse_trace,
            ticket_history=ticket_history,
            branch_pr_links=branch_pr_links,
            clone_repo_info=clone_repo_info,
            captured_at=now,
        )

    # ------------------------------------------------------------------
    # extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_block_reason(ticket_data: dict[str, Any]) -> str:
        """Extract the block reason from ticket data.

        Looks in state-transition metadata, the most recent comment with a
        block reason, or the ticket description.
        """
        # Check state-transition events for a block reason
        events = ticket_data.get("events", [])
        if isinstance(events, list):
            for event in reversed(events):
                if not isinstance(event, dict):
                    continue
                if event.get("to_state", "").upper() == "BLOCKED":
                    reason = str(event.get("comment", ""))
                    if reason:
                        return reason
                    break

        # Fall back to the most recent comment
        comments = ticket_data.get("comments", [])
        if isinstance(comments, list):
            for comment in reversed(comments):
                if not isinstance(comment, dict):
                    continue
                body = str(comment.get("body", ""))
                if body and "block" in body.lower():
                    return body

        # Ultimate fallback: the description
        return str(ticket_data.get("description", ""))

    @staticmethod
    def _extract_langfuse_trace(ticket_data: dict[str, Any]) -> str:
        """Search ticket history for Langfuse trace references.

        Looks for patterns like ``🔍 [Trace: <id>](<url>)`` in comments
        and event bodies.
        """
        # Search comments
        comments = ticket_data.get("comments", [])
        if isinstance(comments, list):
            for comment in reversed(comments):
                if not isinstance(comment, dict):
                    continue
                body = str(comment.get("body", ""))
                if body:
                    match = _TRACE_PATTERN.search(body)
                    if match:
                        return match.group(0)

        # Search events
        events = ticket_data.get("events", [])
        if isinstance(events, list):
            for event in reversed(events):
                if not isinstance(event, dict):
                    continue
                comment = str(event.get("comment", ""))
                if comment:
                    match = _TRACE_PATTERN.search(comment)
                    if match:
                        return match.group(0)

        # Search description
        desc = str(ticket_data.get("description", ""))
        if desc:
            match = _TRACE_PATTERN.search(desc)
            if match:
                return match.group(0)

        return ""

    @staticmethod
    def _extract_branch_pr_links(ticket_data: dict[str, Any]) -> str:
        """Extract branch/PR/CI links from ticket data.

        Searches description and comments for GitHub PR/issue URLs and
        branch references.
        """
        links: list[str] = []

        # GitHub PR / issue URL pattern
        gh_pattern = re.compile(
            r"https?://github\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/\d+"
        )

        def _scan(text: str) -> None:
            for m in gh_pattern.finditer(text):
                link = m.group(0)
                if link not in links:
                    links.append(link)

        _scan(str(ticket_data.get("description", "")))

        comments = ticket_data.get("comments", [])
        if isinstance(comments, list):
            for comment in comments:
                if isinstance(comment, dict):
                    _scan(str(comment.get("body", "")))

        return "\n".join(links)

    @staticmethod
    def _extract_clone_repo_info(ticket_data: dict[str, Any]) -> str:
        """Extract clone-target / repo-mapping info from ticket metadata.

        Looks for repo_id, repos.yaml references, registration status fields
        in the ticket's metadata or events.
        """
        parts: list[str] = []

        # repo_id from ticket
        repo_id = ticket_data.get("repo_id", "")
        if repo_id:
            parts.append(f"repo_id: {repo_id}")

        # Check metadata fields
        metadata = ticket_data.get("metadata", {})
        if isinstance(metadata, dict):
            for key in ("clone_target", "target_repo", "repo_url", "registration"):
                val = metadata.get(key)
                if val:
                    parts.append(f"{key}: {val}")

        # Check events for clone/registration info
        events = ticket_data.get("events", [])
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                comment = event.get("comment", "")
                if comment and any(
                    kw in comment.lower()
                    for kw in ("clone", "repos.yaml", "registration", "repo_id")
                ):
                    parts.append(comment)

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ticket_list(raw: str) -> list[dict[str, Any]]:
        """Parse the board's JSON list response into a list of ticket dicts."""
        parsed = DiagnosticCapture._parse_json(raw)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        return []

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any] | list[Any] | None:
        """Parse a raw JSON string, returning None on failure."""
        try:
            return json.loads(raw)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, TypeError):
            return None

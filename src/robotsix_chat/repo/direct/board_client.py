"""Board API client — talks to the mill board API for ticket-state verification.

Provides a standalone ``BoardClient`` that queries ticket state, fetches
ticket data, and resumes blocked tickets via the board's REST API.  It is
independent of the GitHub App authentication used by ``DirectRepoClient``
— board API calls use ``board_api_token`` bearer auth.

Used by both ``repo/direct`` (as a fallback path for ticket-state
verification) and ``ticket_poll`` (which already owns board-API concern).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

from robotsix_chat.common.http import safe_http_request

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

logger = logging.getLogger(__name__)


class BoardClient:
    """Mill board API client for ticket-state verification and lifecycle ops.

    Talks to the board's ``/tickets/{id}`` and ``/tickets/{id}/resume-blocked``
    endpoints using ``board_api_token`` bearer auth.  All errors are logged
    as warnings and surfaced as ``None`` / ``False`` returns — callers are
    responsible for formatting user-facing messages.
    """

    def __init__(self, settings: DirectRepoSettings) -> None:
        """Store settings; board API calls use ``board_api_token`` auth."""
        self._s = settings
        self._board_url = settings.board_api_base_url.rstrip("/")

    async def _fetch_ticket_field(
        self, ticket_id: str, label_suffix: str, field: str | None = None
    ) -> Any:
        """Fetch a ticket from the board API and return *field* or the full dict.

        When *field* is provided (e.g. ``"state"``), returns ``data.get(field)``;
        when ``None``, returns the full parsed JSON dict.  Returns ``None`` on
        any error (logged as a warning).
        """
        url = f"{self._board_url}/tickets/{ticket_id}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._s.board_api_token.get_secret_value():
            headers["Authorization"] = (
                f"Bearer {self._s.board_api_token.get_secret_value()}"
            )
        label = f"Board API (ticket {label_suffix})"
        result = await safe_http_request(
            "GET", url, headers=headers, timeout=self._s.timeout, label=label
        )
        if result.error:
            logger.warning(
                "Failed to fetch ticket %s %s: %s",
                ticket_id,
                label_suffix,
                result.error,
            )
            return None
        try:
            data = json.loads(result.text or "")
        except json.JSONDecodeError, TypeError:
            logger.warning(
                "Non-JSON response for ticket %s: %s",
                ticket_id,
                (result.text or "")[:200],
            )
            return None
        if field is not None:
            return data.get(field)
        return data

    async def get_ticket_state(self, ticket_id: str) -> str | None:
        """Return the ticket's state (e.g. ``"BLOCKED"``), or ``None`` on failure.

        Calls the board API directly — the same endpoint the browser UI uses.
        """
        return cast(
            "str | None",
            await self._fetch_ticket_field(ticket_id, "state", field="state"),
        )

    async def resume_blocked_ticket(self, ticket_id: str, justification: str) -> bool:
        """Resume a blocked ticket via the board API.

        Sends ``POST /tickets/{ticket_id}/resume-blocked`` with a JSON
        body containing *justification*.  Returns ``True`` on success
        (HTTP 2xx), ``False`` on any error (logged as a warning).
        """
        url = f"{self._board_url}/tickets/{ticket_id}/resume-blocked"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._s.board_api_token.get_secret_value():
            headers["Authorization"] = (
                f"Bearer {self._s.board_api_token.get_secret_value()}"
            )
        result = await safe_http_request(
            "POST",
            url,
            headers=headers,
            json_body={"justification": justification},
            timeout=self._s.timeout,
            label=f"Board API (resume-blocked {ticket_id})",
        )
        if result.error:
            logger.warning(
                "Failed to resume blocked ticket %s: %s",
                ticket_id,
                result.error,
            )
            return False
        if result.status_code and result.status_code >= 400:
            logger.warning(
                "Board API returned %d for resume-blocked on ticket %s",
                result.status_code,
                ticket_id,
            )
            return False
        return True

    async def get_ticket_data(self, ticket_id: str) -> dict[str, Any] | None:
        """Return the full ticket JSON from the board API, or None on failure.

        Calls ``GET /tickets/{ticket_id}`` on the board API and returns the
        parsed JSON body.  The response includes ``state``, ``events`` (state
        transitions), and other ticket metadata.
        """
        return cast(
            "dict[str, Any] | None",
            await self._fetch_ticket_field(ticket_id, "data"),
        )

    async def count_implement_cycles(self, ticket_id: str) -> int | None:
        """Return the number of implement cycles for *ticket_id*, or None on failure.

        Inspects the ticket's ``events`` array (from the board API) and counts
        events whose ``type`` or ``action`` field contains the substring
        ``"implement"`` (case-insensitive).  Falls back to counting state
        transitions through ``"implement_complete"`` if no events array is
        present.
        """
        from robotsix_chat.repo.direct.client import _count_cycles_from_data

        data = await self.get_ticket_data(ticket_id)
        if data is None:
            return None

        cycles = _count_cycles_from_data(data)
        if cycles == 0:
            logger.info(
                "Ticket %s has no events/history/cycle_count — "
                "assuming 0 implement cycles.",
                ticket_id,
            )
        return cycles

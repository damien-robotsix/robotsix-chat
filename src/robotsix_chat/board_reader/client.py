"""Thin async HTTP client for the mill's board API.

Talks directly to the board's FastAPI app (``GET /tickets``,
``GET /tickets/{id}``, ``POST /tickets``) over HTTP — no broker
indirection, no NL reinterpretation.  Degrades gracefully:
HTTP/timeout/parse errors become short strings the assistant can
relay to the user.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from robotsix_chat.common.http import safe_http_request

if TYPE_CHECKING:
    from robotsix_chat.config import BoardReaderSettings

logger = logging.getLogger(__name__)


class BoardReader:
    """Read-only HTTP client for the mill's board API."""

    def __init__(self, settings: BoardReaderSettings) -> None:
        """Store the board API URL, auth token, timeout, and TTL cache."""
        self._base_url = settings.api_base_url.rstrip("/")
        self._token = settings.api_token
        self._timeout = settings.timeout
        self._cache_ttl = settings.cache_ttl
        self._list_cache: dict[tuple[str, bool, str], tuple[float, str]] = {}
        self._ticket_cache: dict[str, tuple[float, str]] = {}
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._headers = headers

    async def list_tickets(
        self,
        *,
        repo_id: str,
        include_closed: bool = False,
        state: str = "",
    ) -> str:
        """Call ``GET /tickets`` and return the raw JSON body as text.

        Never raises — HTTP/parse errors become a message the assistant
        can relay.
        """
        cache_key = (repo_id, include_closed, state)
        now = time.monotonic()
        if cache_key in self._list_cache:
            ts, cached = self._list_cache[cache_key]
            if now - ts < self._cache_ttl:
                logger.debug("Board list cache hit (repo_id=%s)", repo_id)
                return cached
        params: dict[str, str] = {"repo_id": repo_id}
        if include_closed:
            params["include_closed"] = "true"
        if state:
            params["state"] = state
        result = await self._get("/tickets", params=params)
        if not result.startswith(("Board API error", "Board API request")):
            self._list_cache[cache_key] = (time.monotonic(), result)
        return result

    async def get_ticket(self, ticket_id: str) -> str:
        """Call ``GET /tickets/{ticket_id}`` and return the raw JSON body as text.

        Never raises.
        """
        now = time.monotonic()
        if ticket_id in self._ticket_cache:
            ts, cached = self._ticket_cache[ticket_id]
            if now - ts < self._cache_ttl:
                logger.debug("Board ticket cache hit (ticket_id=%s)", ticket_id)
                return cached
        result = await self._get(f"/tickets/{ticket_id}")
        if not result.startswith(("Board API error", "Board API request")):
            self._ticket_cache[ticket_id] = (time.monotonic(), result)
        return result

    async def create_ticket(
        self,
        *,
        title: str,
        description: str,
        repo_id: str,
        kind: str = "",
    ) -> str:
        """Call ``POST /tickets`` and return the raw JSON body as text.

        Creates a new ticket on the board.  This is a direct synchronous
        call — no broker indirection, no background sub-agent.

        Never raises — HTTP/parse errors become a message the assistant
        can relay.

        Args:
            title: Short title for the ticket.
            description: Full description / body of the ticket.
            repo_id: The board repo identifier (e.g. ``"robotsix-chat"``).
            kind: Optional ticket kind (e.g. ``"task"``, ``"bug"``).
                Empty string = board default.

        """
        payload: dict[str, str] = {
            "title": title,
            "description": description,
            "repo_id": repo_id,
        }
        if kind:
            payload["kind"] = kind
        result = await self._post("/tickets", json=payload)
        self._list_cache.clear()
        return result

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> str:
        """Perform a GET request and return the text body.

        On any error (timeout, connection refused, non-2xx, parse failure)
        returns a short diagnostic string — never raises into the chat path.
        """
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "GET",
            url,
            headers=self._headers,
            timeout=self._timeout,
            params=params,
            label="Board API",
        )
        if result.error:
            return result.error
        # Return the raw body as text so the LLM can inspect it.
        # We intentionally do NOT parse JSON here — the LLM is
        # better at summarising raw structured text than we are at
        # picking which fields to excerpt.
        return result.text  # type: ignore[return-value]

    async def _post(
        self,
        path: str,
        *,
        json: dict[str, str] | None = None,
    ) -> str:
        """Perform a POST request and return the text body.

        On any error (timeout, connection refused, non-2xx, parse failure)
        returns a short diagnostic string — never raises into the chat path.
        """
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "POST",
            url,
            headers=self._headers,
            timeout=self._timeout,
            json_body=json,
            label="Board API",
        )
        if result.error:
            return result.error
        return result.text  # type: ignore[return-value]

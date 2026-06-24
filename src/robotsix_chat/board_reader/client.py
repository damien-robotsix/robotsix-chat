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

import httpx

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
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    url,
                    headers=self._headers,
                    params=params,
                )
                response.raise_for_status()
                # Return the raw body as text so the LLM can inspect it.
                # We intentionally do NOT parse JSON here — the LLM is
                # better at summarising raw structured text than we are at
                # picking which fields to excerpt.
                return response.text
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Board API returned %d for %s", exc.response.status_code, url
            )
            body = exc.response.text[:500] if exc.response.text else "(empty body)"
            status = exc.response.status_code
            req_method = getattr(exc.request, "method", "GET")
            req_url = getattr(exc.request, "url", url)
            return f"Board API error {status} for {req_method} {req_url}: {body}"
        except httpx.TimeoutException:
            logger.warning("Board API timed out for %s", url)
            return f"Board API request timed out after {self._timeout}s: {url}"
        except Exception as exc:  # noqa: BLE001 — surface as text, never crash chat
            logger.warning("Board API request failed for %s: %s", url, exc)
            return f"Board API request failed: {exc}"

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
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    url,
                    headers=self._headers,
                    json=json,
                )
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Board API returned %d for %s", exc.response.status_code, url
            )
            body = exc.response.text[:500] if exc.response.text else "(empty body)"
            status = exc.response.status_code
            req_method = getattr(exc.request, "method", "POST")
            req_url = getattr(exc.request, "url", url)
            return f"Board API error {status} for {req_method} {req_url}: {body}"
        except httpx.TimeoutException:
            logger.warning("Board API timed out for %s", url)
            return f"Board API request timed out after {self._timeout}s: {url}"
        except Exception as exc:  # noqa: BLE001 — surface as text, never crash chat
            logger.warning("Board API request failed for %s: %s", url, exc)
            return f"Board API request failed: {exc}"

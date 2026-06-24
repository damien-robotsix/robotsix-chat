"""Thin async HTTP client for the mill's board read API.

Talks directly to the board's FastAPI app (``GET /tickets``,
``GET /tickets/{id}``) over HTTP — no broker indirection, no NL
reinterpretation.  Degrades gracefully: HTTP/timeout/parse errors
become short strings the assistant can relay to the user.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from robotsix_chat.config import BoardReaderSettings

logger = logging.getLogger(__name__)


class BoardReader:
    """Read-only HTTP client for the mill's board API."""

    def __init__(self, settings: BoardReaderSettings) -> None:
        """Store the board API URL, auth token, and timeout."""
        self._base_url = settings.api_base_url.rstrip("/")
        self._token = settings.api_token
        self._timeout = settings.timeout
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
        params: dict[str, str] = {"repo_id": repo_id}
        if include_closed:
            params["include_closed"] = "true"
        if state:
            params["state"] = state
        return await self._get("/tickets", params=params)

    async def get_ticket(self, ticket_id: str) -> str:
        """Call ``GET /tickets/{ticket_id}`` and return the raw JSON body as text.

        Never raises.
        """
        return await self._get(f"/tickets/{ticket_id}")

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
            return (
                f"Board API error {status} for "
                f"{req_method} {req_url}: {body}"
            )
        except httpx.TimeoutException:
            logger.warning("Board API timed out for %s", url)
            return f"Board API request timed out after {self._timeout}s: {url}"
        except Exception as exc:  # noqa: BLE001 — surface as text, never crash chat
            logger.warning("Board API request failed for %s: %s", url, exc)
            return f"Board API request failed: {exc}"

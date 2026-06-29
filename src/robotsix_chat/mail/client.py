"""MailClient — direct HTTP client for the auto-mail board server.

Talks directly to the auto-mail board HTTP API (``GET /board-content``,
``GET /email/{id}/status``, ``POST /move``, ``POST /delete``,
``POST /archive``, ``POST /run-triage``) over HTTP — no broker indirection,
no NL reinterpretation.  Degrades gracefully: HTTP/timeout errors become
short strings the assistant can relay to the user.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import quote

from robotsix_chat.common.http import safe_http_request

if TYPE_CHECKING:
    from robotsix_chat.config import MailSettings

logger = logging.getLogger(__name__)

_VALID_TRIAGE_ACTIONS = frozenset(
    [
        "INBOX",
        "HUMAN_TRIAGE",
        "PENDING_ACTION",
        "TO_ARCHIVE",
        "TO_DELETE",
        "TO_CALENDAR",
        "TO_ANSWER",
        "DRAFT_READY",
    ]
)


class MailClient:
    """Direct HTTP client for the auto-mail board server."""

    def __init__(self, settings: MailSettings) -> None:
        """Store the board API URL, auth token, and timeout."""
        self._base_url = settings.api_base_url.rstrip("/")
        self._token = settings.api_token
        self._timeout = settings.timeout
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._headers = headers

    async def board_content(self) -> str:
        """Call ``GET /board-content`` and return the JSON body as text.

        Never raises — errors become a diagnostic string.
        """
        result = await self._get("/board-content")
        return result

    async def email_status(self, message_id: str) -> str:
        """Call ``GET /email/{message_id}/status`` and return the triage column name.

        Never raises — errors become a diagnostic string.
        """
        path = f"/email/{quote(message_id, safe='')}/status"
        result = await self._get(path)
        return result

    async def move_email(self, message_id: str, triage_action: str) -> str:
        """Call ``POST /move`` with form-encoded *message_id* and *triage_action*.

        Returns a success message on 3xx, or the error body on 4xx.
        Never raises.
        """
        if triage_action not in _VALID_TRIAGE_ACTIONS:
            return (
                f"Invalid triage_action {triage_action!r}. "
                f"Valid: {', '.join(sorted(_VALID_TRIAGE_ACTIONS))}"
            )
        data = f"message_id={quote(message_id)}&triage_action={quote(triage_action)}"
        return await self._post_form("/move", data)

    async def delete_email(self, message_id: str) -> str:
        """Call ``POST /delete`` with form-encoded *message_id*.

        Returns a success message on 3xx, or the error body on 4xx.
        Never raises.
        """
        data = f"message_id={quote(message_id)}"
        return await self._post_form("/delete", data)

    async def archive_email(self, message_id: str) -> str:
        """Call ``POST /archive`` with form-encoded *message_id*.

        Returns a success message on 3xx, or the error body on 4xx.
        Never raises.
        """
        data = f"message_id={quote(message_id)}"
        return await self._post_form("/archive", data)

    async def run_triage(self) -> str:
        """Call ``POST /run-triage`` with an empty form body.

        Returns a success message on 3xx, or the error body on 4xx.
        Never raises.
        """
        return await self._post_form("/run-triage", "")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str) -> str:
        """Perform a GET request and return the text body or error string."""
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "GET",
            url,
            headers=self._headers,
            timeout=self._timeout,
            label="Mail API",
        )
        if result.error:
            return result.error
        return result.text  # type: ignore[return-value]

    async def _post_form(self, path: str, data: str) -> str:
        """Perform a POST with form-encoded body, treating 3xx as success."""
        url = f"{self._base_url}{path}"
        headers = {**self._headers, "Content-Type": "application/x-www-form-urlencoded"}
        result = await safe_http_request(
            "POST",
            url,
            headers=headers,
            timeout=self._timeout,
            content=data,
            follow_redirects=False,
            label="Mail API",
        )
        if result.error:
            return result.error
        if result.status_code and 300 <= result.status_code < 400:
            return f"OK (status {result.status_code})"
        return result.text  # type: ignore[return-value]

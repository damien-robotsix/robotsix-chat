"""MailClient — direct HTTP client for the auto-mail board server.

Talks directly to the auto-mail board HTTP API (``GET /board-content``,
``GET /email/{id}/status``, ``POST /move``, ``POST /delete``,
``POST /archive``, ``POST /run-triage``, ``POST /archive-delete``) over
HTTP — no broker indirection, no NL reinterpretation.  Degrades
gracefully: HTTP/timeout errors become short strings the assistant can
relay to the user.
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
        self._token = settings.api_token.get_secret_value()
        self._timeout = settings.timeout
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._headers = headers

    async def board_content(self, account_id: str | None = None) -> str:
        """Call ``GET /board-content`` and return the JSON body as text.

        Args:
            account_id: Optional account identifier to scope the board to.
                When omitted, the server's default account is used.

        Never raises — errors become a diagnostic string.

        """
        params: dict[str, str] | None = None
        if account_id:
            params = {"account_id": account_id}
        result = await self._get("/board-content", params=params)
        return result

    async def list_accounts(self) -> str:
        """Call ``GET /list-accounts`` and return the JSON body as text.

        Never raises — errors become a diagnostic string.
        """
        return await self._get("/list-accounts")

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

    async def delete_email(self, message_id: str, account: str) -> str:
        """Call ``POST /delete`` with form-encoded *message_id* and *account*.

        Returns a success message on 3xx, or the error body on 4xx.
        Never raises.
        """
        data = f"message_id={quote(message_id)}"
        return await self._post_form("/delete", data, params={"account": account})

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

    async def archive_folders(
        self,
        account: str | None = None,
        *,
        include_unmapped: bool = False,
    ) -> str:
        """Call ``GET /archive-folders`` and return the JSON body as text.

        Args:
            account: Optional IMAP account to list folders for.  When
                omitted, the server lists folders for its default account.
            include_unmapped: When ``True``, return every IMAP folder
                (top-level folders and siblings of the archive root), not
                just the subfolders under the resolved archive root.

        Returns a JSON object with ``delimiter`` and ``folders`` keys.
        Never raises — errors become a diagnostic string.

        """
        params: dict[str, str] = {}
        if account is not None:
            params["account"] = account
        if include_unmapped:
            params["include_unmapped"] = "true"
        return await self._get("/archive-folders", params=params)

    async def archive_messages(self, folder: str, limit: int | None = None) -> str:
        """Call ``GET /archive/<folder>/messages`` and return the JSON body as text.

        Args:
            folder: The archive subfolder to browse (URL-path-encoded).
            limit: Optional max number of messages to return (1-2000).

        Returns:
            JSON object with ``messages`` and ``folder`` keys.

        Never raises — errors become a diagnostic string.

        """
        path = f"/archive/{quote(folder, safe='')}/messages"
        params: dict[str, str] = {}
        if limit is not None:
            params["limit"] = str(limit)
        return await self._get(path, params=params)

    async def archive_move(
        self,
        message_id: str,
        source_folder: str,
        target_subfolder: str,
        *,
        create_folders: bool = False,
    ) -> str:
        """Call ``POST /archive-move`` with a JSON body.

        Args:
            message_id: The Message-ID header of the mail to move.
            source_folder: The current archive subfolder path.
            target_subfolder: The destination archive subfolder.
            create_folders: Whether to create missing subfolders on the
                target path.  Defaults to ``False`` (lazy creation) — the
                server will only create the target folder hierarchy when
                an email is actually archived into a new folder.

        Returns:
            JSON success/error object as text.

        Never raises — errors become a diagnostic string.

        """
        url = f"{self._base_url}/archive-move"
        json_body: dict[str, object] = {
            "message_id": message_id,
            "source_folder": source_folder,
            "target_subfolder": target_subfolder,
        }
        if create_folders:
            json_body["create_folders"] = True
        result = await safe_http_request(
            "POST",
            url,
            headers=self._headers,
            timeout=self._timeout,
            json_body=json_body,
            label="Mail API",
        )
        if result.error:
            return result.error
        return result.text  # type: ignore[return-value]

    async def archive_cleanup_empty(self) -> str:
        """Call ``POST /archive-cleanup-empty`` and return the JSON body as text.

        Removes empty archive subfolders from the IMAP server so the
        archive hierarchy stays clean.  Only folders with zero messages
        are removed; non-empty folders are left untouched.

        Returns:
            JSON object with a list of removed folder paths.

        Never raises — errors become a diagnostic string.

        """
        url = f"{self._base_url}/archive-cleanup-empty"
        result = await safe_http_request(
            "POST",
            url,
            headers=self._headers,
            timeout=self._timeout,
            label="Mail API",
        )
        if result.error:
            return result.error
        return result.text  # type: ignore[return-value]

    async def archive_delete(self, folder: str, *, force: bool = False) -> str:
        """Call ``POST /archive-delete`` with a JSON body.

        Deletes an archive subfolder from the IMAP server.  By default
        only empty folders (zero messages) can be deleted; pass
        ``force=True`` to delete a non-empty folder.

        Client-side path-escape protection rejects *folder* values that
        contain ``..``, null bytes, or absolute paths before the
        request is sent.

        Args:
            folder: The archive subfolder path to delete (e.g.
                ``"Projects/Old"``).
            force: When ``True``, allow deletion of non-empty folders.
                Defaults to ``False`` (empty-only).

        Returns:
            JSON success/error object as text.

        Never raises — errors become a diagnostic string.

        """
        # Client-side path-escape protection: reject traversal attempts.
        if not folder or "\x00" in folder:
            return (
                "error: invalid folder path — path must not be empty "
                "or contain null bytes"
            )
        if folder.startswith("/"):
            return (
                "error: invalid folder path — absolute paths are not "
                "allowed (must be relative to the archive root)"
            )
        if ".." in folder.split("/"):
            return (
                "error: invalid folder path — '..' traversal is not "
                "allowed (must be under the archive root)"
            )

        url = f"{self._base_url}/archive-delete"
        json_body: dict[str, object] = {"folder": folder}
        if force:
            json_body["force"] = True
        result = await safe_http_request(
            "POST",
            url,
            headers=self._headers,
            timeout=self._timeout,
            json_body=json_body,
            label="Mail API",
        )
        if result.error:
            return result.error
        return result.text  # type: ignore[return-value]

    async def archive_rename_folder(self, old_path: str, new_path: str) -> str:
        """Call ``POST /archive-rename-folder`` with a JSON body.

        Renames an archive subfolder in-place on the IMAP server,
        preserving all messages.  This is a single atomic operation —
        no message moves, no temporary folders, no risk of data loss.

        Args:
            old_path: The current archive subfolder path to rename.
            new_path: The destination archive subfolder path.

        Returns:
            JSON success/error object as text.

        Never raises — errors become a diagnostic string.

        """
        url = f"{self._base_url}/archive-rename-folder"
        json_body: dict[str, object] = {
            "old_path": old_path,
            "new_path": new_path,
        }
        result = await safe_http_request(
            "POST",
            url,
            headers=self._headers,
            timeout=self._timeout,
            json_body=json_body,
            label="Mail API",
        )
        if result.error:
            return result.error
        return result.text  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, str] | None = None) -> str:
        """Perform a GET request and return the text body or error string."""
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "GET",
            url,
            headers=self._headers,
            timeout=self._timeout,
            params=params,
            label="Mail API",
        )
        if result.error:
            return result.error
        return result.text  # type: ignore[return-value]

    async def _post_form(
        self, path: str, data: str, params: dict[str, str] | None = None
    ) -> str:
        """Perform a POST with form-encoded body, treating 3xx as success."""
        url = f"{self._base_url}{path}"
        headers = {**self._headers, "Content-Type": "application/x-www-form-urlencoded"}
        result = await safe_http_request(
            "POST",
            url,
            headers=headers,
            timeout=self._timeout,
            content=data,
            params=params,
            follow_redirects=False,
            label="Mail API",
        )
        if result.error:
            return result.error
        if result.status_code and 300 <= result.status_code < 400:
            return f"OK (status {result.status_code})"
        return result.text  # type: ignore[return-value]

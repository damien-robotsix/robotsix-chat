"""robotsix-auto-mail integration over direct HTTP.

Exposes :func:`build_mail_tools` — a factory returning discrete LLM tools
that call the auto-mail board HTTP API directly (no broker indirection, no
NL reinterpretation). Returns no tools when mail integration is disabled,
so the chat runs exactly as before.

Each tool is a plain async callable; robotsix-llmio converts it into a tool
for the underlying agent (the claude-sdk tool loop, or pydantic-ai function
tools).

Also exposes :func:`load_mail_skill` — reads ``skill.md`` from this package
so the agent prompt includes mail-tool usage instructions.
"""

from __future__ import annotations

import importlib.resources
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import MailSettings

__all__ = ["build_mail_tools", "load_mail_skill"]


def load_mail_skill() -> str:
    """Return the ``skill.md`` content for mail tools.

    Returns an empty string when the file is missing or unreadable
    so a missing skill doc never breaks the agent prompt.
    """
    try:
        return (
            importlib.resources.files("robotsix_chat.mail")
            .joinpath("skill.md")
            .read_text()
        )
    except Exception:
        return ""


def build_mail_tools(settings: MailSettings) -> list[Callable[..., Any]]:
    """Return the mail tool(s) for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    from .client import MailClient

    client = MailClient(settings)

    async def get_mail_board() -> str:
        """Get the full auto-mail board content (columns + cards).

        Returns the board state as JSON text — each column lists its
        contained emails.  Use this to see the current triage state.

        Never raises — errors become a diagnostic string.

        """
        return await client.board_content()

    async def get_mail_email_status(message_id: str) -> str:
        """Get the triage column name for a specific email.

        Args:
            message_id: The email's unique message identifier.

        Returns:
            The triage column name (plain text), or an error string.

        Never raises.

        """
        return await client.email_status(message_id)

    async def move_mail_email(message_id: str, triage_action: str) -> str:
        """Move an email to a different triage column.

        Args:
            message_id: The email's unique message identifier.
            triage_action: The target column — one of INBOX, HUMAN_TRIAGE,
                PENDING_ACTION, TO_ARCHIVE, TO_DELETE, TO_CALENDAR,
                TO_ANSWER, DRAFT_READY.

        Returns:
            A success or error message.

        Never raises.

        """
        return await client.move_email(message_id, triage_action)

    async def delete_mail_email(message_id: str) -> str:
        """Delete an email from the board permanently.

        Args:
            message_id: The email's unique message identifier.

        Returns:
            A success or error message.

        Never raises.

        """
        return await client.delete_email(message_id)

    async def archive_mail_email(message_id: str) -> str:
        """Archive an email (mark it as processed without deleting).

        Args:
            message_id: The email's unique message identifier.

        Returns:
            A success or error message.

        Never raises.

        """
        return await client.archive_email(message_id)

    async def run_mail_triage() -> str:
        """Trigger the auto-mail triage engine to re-classify the inbox.

        This applies the configured triage rules to all unprocessed
        emails in the inbox.

        Returns:
            A success or error message.

        Never raises.

        """
        return await client.run_triage()

    async def list_archive_folders() -> str:
        """List all archive subfolders on the mail server.

        Returns a JSON object with ``delimiter`` (the hierarchy separator)
        and ``folders`` (a flat list of subfolder paths relative to the
        archive root).  Use this to discover which archive subfolders
        exist before browsing or moving messages.

        Returns:
            JSON text with delimiter and folder list.

        Never raises — errors become a diagnostic string.

        """
        return await client.archive_folders()

    async def browse_archive_folder(folder: str, limit: int | None = None) -> str:
        """List messages inside a specific archive subfolder.

        Args:
            folder: The archive subfolder path (e.g. "Projects/Acme").
                Must be under the archive root — path traversal sequences
                like ``..`` are rejected server-side.
            limit: Optional cap on the number of messages returned
                (default 500, max 2000).

        Returns:
            JSON text with message envelope metadata (sender, subject,
            date) for every message in the folder, or an empty list when
            the folder is empty or does not exist.

        Never raises — errors become a diagnostic string.

        """
        return await client.archive_messages(folder, limit=limit)

    async def move_archive_mail(
        message_id: str,
        source_folder: str,
        target_subfolder: str,
    ) -> str:
        """Move a mail between archive subfolders.

        **This is a confirmation-gated mutation.**  You MUST obtain
        explicit operator approval before calling this function.  State:

        * The exact message identifier (subject / sender / date).
        * The current archive subfolder it lives in.
        * The target archive subfolder it will move to.

        Wait for a clear confirmation reply (e.g. "yes", "proceed",
        "go ahead") from the operator before proceeding.  Silently
        moving mail without consent is prohibited.

        Target folders are created lazily — the server only creates
        the folder hierarchy when a message is actually moved into a
        new folder.  Empty folders are not created in advance.

        Args:
            message_id: The Message-ID header of the mail to move.
            source_folder: The current archive subfolder path
                (as listed by ``list_archive_folders``).
            target_subfolder: The destination archive subfolder path.

        Returns:
            JSON text with a success confirmation or error detail.

        Never raises — errors become a diagnostic string.

        """
        return await client.archive_move(message_id, source_folder, target_subfolder)

    async def cleanup_empty_archive_folders() -> str:
        """Remove empty archive subfolders from the IMAP server.

        This cleans up the archive hierarchy by deleting subfolders
        that contain zero messages.  Non-empty folders are left
        untouched.  Use this periodically to keep the archive view
        clean after moving or archiving messages.

        Returns:
            JSON text with a list of removed folder paths.

        Never raises — errors become a diagnostic string.

        """
        return await client.archive_cleanup_empty()

    async def delete_archive_folder(folder: str, *, force: bool = False) -> str:
        """Delete an archive subfolder from the IMAP server.

        **This is a confirmation-gated mutation.**  You MUST obtain
        explicit operator approval before calling this function.  State:

        * The exact archive subfolder path to delete.
        * Whether the folder is empty (list its messages first with
          ``browse_archive_folder``).
        * Whether you are using ``force`` mode to delete a non-empty
          folder.

        Wait for a clear confirmation reply (e.g. "yes", "proceed",
        "go ahead") from the operator before proceeding.  Silently
        deleting folders without consent is prohibited.

        By default, only empty folders (zero messages) can be deleted.
        To delete a non-empty folder, pass ``force=True`` — but obtain
        operator confirmation first.

        Args:
            folder: The archive subfolder path to delete (e.g.
                ``"Projects/Old"``).
            force: When ``True``, allow deletion of non-empty folders.
                Defaults to ``False`` (empty-only).

        Returns:
            JSON text with a success confirmation or error detail.

        Never raises — errors become a diagnostic string.

        """
        return await client.archive_delete(folder, force=force)

    return [
        get_mail_board,
        get_mail_email_status,
        move_mail_email,
        delete_mail_email,
        archive_mail_email,
        run_mail_triage,
        list_archive_folders,
        browse_archive_folder,
        move_archive_mail,
        cleanup_empty_archive_folders,
        delete_archive_folder,
    ]

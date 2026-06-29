"""robotsix-auto-mail integration over direct HTTP.

Exposes :func:`build_mail_tools` — a factory returning discrete LLM tools
that call the auto-mail board HTTP API directly (no broker indirection, no
NL reinterpretation). Returns no tools when mail integration is disabled,
so the chat runs exactly as before.

Each tool is a plain async callable; robotsix-llmio converts it into a tool
for the underlying agent (the claude-sdk tool loop, or pydantic-ai function
tools).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import MailSettings

__all__ = ["build_mail_tools"]


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

    return [
        get_mail_board,
        get_mail_email_status,
        move_mail_email,
        delete_mail_email,
        archive_mail_email,
        run_mail_triage,
    ]

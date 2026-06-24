"""MailClient — broker-mediated connector to the auto-mail board manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from robotsix_chat.broker_client import BaseBrokeredClient

if TYPE_CHECKING:
    from robotsix_chat.config import MailSettings


class MailClient(BaseBrokeredClient):
    """Forwards natural-language requests to the auto-mail board manager."""

    def __init__(self, settings: MailSettings) -> None:
        """Store the mail broker settings, build a brokered requester."""
        super().__init__(
            settings,
            target_agent_id=settings.board_manager_id,
            default_reply="The auto-mail board manager returned no reply.",
        )

    async def consult(
        self,
        request: str,
        *,
        empty_reply: str = "",
        error_label: str = "",
        **extra_payload: object,
    ) -> str:
        """Send *request* to the board manager and return its reply as text.

        Never raises: broker/timeout/board errors become a short message the
        calling LLM can relay to the user.
        """
        return await super().consult(
            request,
            empty_reply="No request was provided to send to the mail board.",
            error_label="mail",
            **extra_payload,
        )

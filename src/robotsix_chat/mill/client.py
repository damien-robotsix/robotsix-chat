"""Talk to robotsix-mill's board manager over the agent-comm broker.

Thin subclass of :class:`~robotsix_chat.broker_client.BaseBrokeredClient` that
wires the mill-specific target agent ID, default reply, and the optional
``repo_id`` payload key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robotsix_chat.broker_client import BaseBrokeredClient

if TYPE_CHECKING:
    from robotsix_chat.config import MillSettings


class MillClient(BaseBrokeredClient):
    """Forwards natural-language requests to the mill's board manager."""

    def __init__(self, settings: MillSettings) -> None:
        """Store the mill broker settings, build a brokered requester."""
        super().__init__(
            settings,
            target_agent_id=settings.board_manager_id,
            default_reply="The mill board manager returned no reply.",
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
        extra: dict[str, str] = {}
        if self._s.repo_id:
            extra["repo_id"] = self._s.repo_id
        return await super().consult(
            request,
            empty_reply="No request was provided to send to the mill.",
            error_label="mill",
            **extra,
        )

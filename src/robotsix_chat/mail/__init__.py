"""robotsix-auto-mail integration over the agent-comm broker.

Exposes :func:`build_mail_tools` — a factory returning the LLM tool(s) that let
the chat agent consult the auto-mail board manager (view/list/triage/comment on
mail-agent tickets) in natural language. Returns no tools when mail integration
is disabled or when the ``broker`` extra (robotsix-agent-comm) is absent, so the
chat runs exactly as before.

The tool is a plain async callable; robotsix-llmio converts it into a tool for
the underlying agent (the claude-sdk tool loop, or pydantic-ai function tools).
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import MailSettings

logger = logging.getLogger(__name__)

__all__ = ["build_mail_tools"]


def build_mail_tools(settings: MailSettings) -> list[Callable[..., Any]]:
    """Return the mail tool(s) for the agent, or ``[]`` when unavailable."""
    if not settings.enabled:
        return []
    if importlib.util.find_spec("robotsix_agent_comm") is None:
        logger.warning(
            "mail.enabled is true but the 'broker' extra (robotsix-agent-comm) is "
            "not installed — the mail tool is unavailable. Install "
            "robotsix-chat[broker]."
        )
        return []

    from .client import MailClient

    client = MailClient(settings)

    async def consult_mail(request: str) -> str:
        """Consult the robotsix auto-mail board manager.

        Use this when the user wants to view, list, triage, or comment on
        mail-agent tickets on the auto-mail board. Pass a clear, self-contained
        natural-language description of what the user wants; the board manager
        handles the ticket operation and replies with the outcome.

        Do NOT use this to send emails — this tool manages the mail board's
        ticket queue, not email delivery.

        Args:
            request: A natural-language description of the request for the
                mail board manager.

        Returns:
            The board manager's reply.

        """
        return await client.consult(request)

    return [consult_mail]

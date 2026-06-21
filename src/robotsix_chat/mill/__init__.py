"""robotsix-mill integration over the agent-comm broker.

Exposes :func:`build_mill_tools` — a factory returning the LLM tool(s) that let
the chat agent consult the mill's board manager (create/triage tickets, ask
about work) in natural language. Returns no tools when mill integration is
disabled or when the ``broker`` extra (robotsix-agent-comm) is absent, so the
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
    from robotsix_chat.config import MillSettings

logger = logging.getLogger(__name__)

__all__ = ["build_mill_tools"]


def build_mill_tools(settings: MillSettings) -> list[Callable[..., Any]]:
    """Return the mill tool(s) for the agent, or ``[]`` when unavailable."""
    if not settings.enabled:
        return []
    if importlib.util.find_spec("robotsix_agent_comm") is None:
        logger.warning(
            "mill.enabled is true but the 'broker' extra (robotsix-agent-comm) is "
            "not installed — the mill tool is unavailable. Install "
            "robotsix-chat[broker]."
        )
        return []

    from .client import MillClient

    client = MillClient(settings)

    async def consult_mill(request: str) -> str:
        """Consult the robotsix mill's board manager.

        Use this whenever the user wants development work tracked or carried out
        by the mill — e.g. create or triage a ticket to implement a feature or
        fix a bug — or asks about the status of mill tickets or ongoing work.
        Pass a clear, self-contained natural-language description of what the
        user wants; the board manager decides the target repository and the
        board action, and replies with the outcome.

        Args:
            request: A natural-language description of the request for the mill.

        Returns:
            The board manager's reply.

        """
        return await client.consult(request)

    return [consult_mill]

"""robotsix-mill integration over the agent-comm broker.

Exposes :func:`build_mill_tools` — a factory returning the LLM tool(s) that let
the chat agent consult the mill's board manager (create/triage tickets, ask
about work) in natural language. Returns no tools when mill integration is
disabled or when the ``broker`` extra (robotsix-agent-comm) is absent, so the
chat runs exactly as before.

The tools are plain async callables; robotsix-llmio converts them into tools for
the underlying agent (the claude-sdk tool loop, or pydantic-ai function tools).
"""

from __future__ import annotations

import contextvars
import importlib.util
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import MillSettings

logger = logging.getLogger(__name__)

# Per-turn cache for consult_mill results, keyed by request string.
# Reset at the start of each agent stream() invocation (see llm/agent.py).
# Cached results avoid redundant broker round-trips when the LLM re-reads
# the same board data within a single turn/tick.
_mill_cache: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "mill_cache"
)

__all__ = ["_mill_cache", "build_mill_tools"]


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

    from robotsix_chat.broker_client import BrokerUnavailableError

    from .client import MillClient
    from .retry_queue import BoardWriteRetryQueue

    client = MillClient(settings)

    async def _raw_consult(request: str) -> str:
        return await client.consult(request)

    retry_queue = BoardWriteRetryQueue(consult_fn=_raw_consult)
    # Start drain loop eagerly if there are entries persisted from a prior run
    if retry_queue._entries:
        retry_queue._ensure_started()

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
        try:
            cache = _mill_cache.get()
        except LookupError:
            cache = {}
        if request in cache:
            logger.debug("consult_mill: returning cached result for %r", request)
            return cache[request]
        try:
            result = await _raw_consult(request)
        except BrokerUnavailableError:
            return retry_queue.enqueue(request)
        cache[request] = result
        return result

    async def get_board_write_queue_status() -> str:
        """Return the current state of the board-write retry queue.

        Shows any board writes that are pending retry after a broker-unavailability
        failure, including their queue id, request preview, attempt count,
        last error, and scheduled next-retry time. Returns a short message when
        the queue is empty.
        """
        retry_queue._ensure_started()  # resume drain loop if needed after restart
        return retry_queue.status()

    return [consult_mill, get_board_write_queue_status]

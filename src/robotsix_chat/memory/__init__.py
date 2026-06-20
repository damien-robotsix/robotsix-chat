"""Persistent agent memory for robotsix-chat.

The chat agent is otherwise stateless (one independent query per message). This
package adds an *optional* long-term memory the agent recalls from before each
reply and writes back to afterwards, so knowledge accumulates across
conversations.

The backend is `cognee <https://www.cognee.ai/>`_ (an embedded knowledge-graph
memory) wired to a remote OpenAI-compatible embedding server and an OpenRouter
extraction LLM — see :class:`~robotsix_chat.memory.cognee.CogneeMemory`. Memory
is **disabled by default**; when off (or when the ``memory`` extra is not
installed) a :class:`NullMemory` no-op is used and the agent behaves exactly as
before.

The public surface is the :class:`ChatMemory` protocol — ``setup`` / ``recall``
/ ``remember`` — so the agent depends on the interface, never on cognee.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import ChatMemory, NullMemory

if TYPE_CHECKING:
    from robotsix_chat.config import MemorySettings

__all__ = ["ChatMemory", "NullMemory", "build_memory"]


def build_memory(settings: MemorySettings) -> ChatMemory:
    """Return a :class:`ChatMemory` for the given ``MemorySettings``.

    Returns a :class:`NullMemory` when memory is disabled or the cognee extra
    is not importable; otherwise a configured
    :class:`~robotsix_chat.memory.cognee.CogneeMemory`. Importing cognee is
    deferred to here so the base package never requires the heavy extra.
    """
    if not settings.enabled:
        return NullMemory()

    import importlib.util

    if importlib.util.find_spec("cognee") is None:
        # The `memory` extra (cognee) is not installed — degrade to no-op
        # rather than crash the server.
        import logging

        logging.getLogger(__name__).warning(
            "memory.enabled is true but the 'memory' extra (cognee) is not "
            "installed — running without memory. Install robotsix-chat[memory]."
        )
        return NullMemory()

    from .cognee import CogneeMemory

    return CogneeMemory(settings)

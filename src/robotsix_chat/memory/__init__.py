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

from .base import ChatMemory, NullMemory, ReadOnlyMemory

if TYPE_CHECKING:
    from robotsix_chat.config import LangfuseSettings, MemorySettings

__all__ = [
    "ChatMemory",
    "NullMemory",
    "ReadOnlyMemory",
    "build_memory",
    "reset_build_memory_cache",
]


# Process-wide cache of CogneeMemory backends, keyed by their configuration.
# cognee's stores and config are process-global, so distinct instances over the
# same settings buy nothing — but each one pays cognee's cold start (measured
# 47-105 s live) on its first recall. The server builds a memory per agent (the
# main chat agent plus every background agent and runtime-spawned subsession);
# sharing the backend means the single startup warm-up covers all of them,
# including agents that don't exist yet at warm-up time.
_MEMORY_CACHE: dict[str, ChatMemory] = {}


def reset_build_memory_cache() -> None:
    """Drop all cached backends (test isolation only).

    Each pytest case runs on a fresh event loop; a cached backend holds asyncio
    primitives bound to the loop it was first awaited on, so leaking one across
    tests raises "bound to a different event loop" in whichever test comes
    second.
    """
    _MEMORY_CACHE.clear()


def build_memory(
    settings: MemorySettings, langfuse: LangfuseSettings | None = None
) -> ChatMemory:
    """Return the :class:`ChatMemory` for the given ``MemorySettings``.

    Returns a :class:`NullMemory` when memory is disabled or the cognee extra
    is not importable; otherwise a configured
    :class:`~robotsix_chat.memory.cognee.CogneeMemory`. Importing cognee is
    deferred to here so the base package never requires the heavy extra.

    Calls with equal configuration return the **same** backend instance —
    see ``_MEMORY_CACHE``. The cache key is the settings' JSON dump, in which
    pydantic masks secrets; configurations differing *only* in a secret value
    therefore share a backend. Within one process that cannot happen — every
    caller passes the same loaded config.

    Args:
        settings: Memory configuration.
        langfuse: The component's canonical Langfuse credential block, from
            which cognee resolves its own project (``langfuse_project``).
            Omitted means cognee LLM calls are not traced.

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

    key = (
        settings.model_dump_json()
        + "|"
        + (langfuse.model_dump_json() if langfuse is not None else "")
    )
    cached = _MEMORY_CACHE.get(key)
    if cached is not None:
        return cached

    from .cognee import CogneeMemory

    memory = CogneeMemory(settings, langfuse)
    _MEMORY_CACHE[key] = memory
    return memory

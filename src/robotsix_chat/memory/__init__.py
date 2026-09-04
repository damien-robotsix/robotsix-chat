"""Persistent agent memory for robotsix-chat.

The chat agent is otherwise stateless (one independent query per message).
This package adds long-term memory the agent recalls from before each reply,
so knowledge accumulates across conversations.

The backend is the **robotsix-memory component** (a fleet service wrapping a
Hindsight memory engine) — see
:class:`~robotsix_chat.memory.component.ComponentMemory`. Recall is a cheap
HTTP lookup rendered at the end of the agent prompt; writes belong to the
evergoing summary pipeline (:mod:`robotsix_chat.memory_push`) and the
agent's explicit skill calls. When the component is disabled a
:class:`NullMemory` no-op is used and the agent behaves statelessly.

The public surface is the :class:`ChatMemory` protocol — ``setup`` /
``recall`` / ``remember`` — so the agent depends on the interface, never on
the engine. (The previous in-process cognee backend was removed 2026-09-04
after repeated storage-engine failures; see the robotsix-memory component.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import ChatMemory, NullMemory, ReadOnlyMemory

if TYPE_CHECKING:
    from robotsix_chat.config.models import MemoryComponentSettings

__all__ = [
    "ChatMemory",
    "NullMemory",
    "ReadOnlyMemory",
    "build_memory",
    "reset_build_memory_cache",
]


# Process-wide cache of memory backends, keyed by their configuration, so
# every agent (main chat, background agents, runtime-spawned subsessions)
# shares one client per configuration.
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
    memory_component: MemoryComponentSettings | Any = None,
) -> ChatMemory:
    """Return the :class:`ChatMemory` for the given component settings.

    Returns a :class:`~robotsix_chat.memory.component.ComponentMemory` when
    the robotsix-memory component is enabled, a :class:`NullMemory` no-op
    otherwise. Calls with equal configuration return the **same** backend
    instance — see ``_MEMORY_CACHE``.

    Args:
        memory_component:
            :class:`~robotsix_chat.config.models.MemoryComponentSettings`
            (or ``None`` to disable memory).

    """
    if memory_component is None or not getattr(memory_component, "enabled", False):
        return NullMemory()

    key = "component|" + memory_component.model_dump_json()
    cached = _MEMORY_CACHE.get(key)
    if cached is not None:
        return cached

    from .component import ComponentMemory

    component_memory = ComponentMemory(
        memory_component.url,
        timeout_seconds=memory_component.timeout_seconds,
    )
    _MEMORY_CACHE[key] = component_memory
    return component_memory

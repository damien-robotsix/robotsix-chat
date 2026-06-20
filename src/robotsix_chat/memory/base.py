"""The :class:`ChatMemory` protocol and the :class:`NullMemory` no-op."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChatMemory(Protocol):
    """Interface the chat agent uses to recall and persist memory.

    Implementations must be *safe by construction*: ``recall`` and ``remember``
    must never raise into the chat request path — a memory backend that is
    misconfigured or unreachable degrades to "no memory", never a failed reply.
    """

    async def setup(self) -> None:
        """Initialise the backend once (idempotent). Safe to call repeatedly."""
        ...

    async def recall(self, query: str) -> str:
        """Return memory relevant to *query* as a context string (``""`` if none)."""
        ...

    async def remember(self, user_message: str, assistant_message: str) -> None:
        """Persist a completed exchange into long-term memory."""
        ...


class NullMemory:
    """A :class:`ChatMemory` that stores nothing and recalls nothing.

    Used when memory is disabled or the backend is unavailable, so the agent
    keeps working with zero memory behaviour and no extra dependencies.
    """

    async def setup(self) -> None:
        return None

    async def recall(self, query: str) -> str:
        return ""

    async def remember(self, user_message: str, assistant_message: str) -> None:
        return None

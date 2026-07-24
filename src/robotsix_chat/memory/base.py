"""The :class:`ChatMemory` protocol and the :class:`NullMemory` no-op."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

# A zero-arg async callable that triggers out-of-band recovery (a service
# self-restart) and returns a human-readable result string.  Injected into a
# memory backend so it can recover a frozen store without a hard dependency on
# the deploy-lifecycle client.
RecoverCallback = Callable[[], Awaitable[str]]


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

    async def recall(self, query: str, *, session_id: str | None = None) -> str:
        """Return memory relevant to *query* as a context string (``""`` if none).

        *session_id* scopes the recall to one conversation so that
        session-level guidance (goals, rules, preferences) is isolated
        across concurrent windows.
        """
        ...

    async def remember(
        self,
        user_message: str,
        assistant_message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Persist a completed exchange into long-term memory.

        *session_id* scopes the write to one conversation so that
        session-level guidance stays per-window.
        """
        ...

    def status(self) -> dict[str, Any]:
        """Return a small health snapshot (``{"degraded": bool, ...}``).

        Read by ``GET /health`` so a frozen store is externally visible.
        Must never raise.
        """
        ...

    def set_recovery_callback(self, callback: RecoverCallback | None) -> None:
        """Register (or clear) the out-of-band recovery callback.

        A backend that can detect a persistent freeze uses this to trigger a
        self-restart.  Backends with no recovery path may ignore it.
        """
        ...


class NullMemory:
    """A :class:`ChatMemory` that stores nothing and recalls nothing.

    Used when memory is disabled or the backend is unavailable, so the agent
    keeps working with zero memory behaviour and no extra dependencies.
    """

    async def setup(self) -> None:
        """No-op: nothing to initialise."""
        return None

    async def recall(self, query: str, *, session_id: str | None = None) -> str:
        """Return an empty string (no memory stored)."""
        return ""

    async def remember(
        self,
        user_message: str,
        assistant_message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Discard the exchange (no memory backend)."""
        return None

    def status(self) -> dict[str, Any]:
        """Report a non-degraded no-op backend."""
        return {"backend": "null", "degraded": False}

    def set_recovery_callback(self, callback: RecoverCallback | None) -> None:
        """No-op: a null backend has nothing to recover."""
        return None

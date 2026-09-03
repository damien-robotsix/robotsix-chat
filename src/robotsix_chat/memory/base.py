"""The :class:`ChatMemory` protocol and the :class:`NullMemory` no-op."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

# A zero-arg async callable that triggers out-of-band recovery (a service
# self-restart) and returns a human-readable result string.  Injected into a
# memory backend so it can recover a frozen store without a hard dependency on
# the deploy-lifecycle client.
RecoverCallback = Callable[[], Awaitable[str]]

# A ``(title, body)`` async callable that escalates a store fault to the user
# (wired to ``notify_user`` by the server).  Injected into a memory backend so
# it can surface a fault auto-recovery cannot safely heal without a hard
# dependency on the notification/EventBus layer.
NotifyCallback = Callable[[str, str], Awaitable[None]]


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

    def set_notify_callback(self, callback: NotifyCallback | None) -> None:
        """Register (or clear) the user-facing escalation callback.

        A backend that detects a fault auto-recovery cannot safely heal uses
        this to escalate (``notify_user``).  Backends with no escalation path
        may ignore it.
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

    def set_notify_callback(self, callback: NotifyCallback | None) -> None:
        """No-op: a null backend has nothing to escalate."""
        return None


class ReadOnlyMemory:
    """Wraps a :class:`ChatMemory` so it can recall but never write.

    Recall and cognify have wildly different costs. Recall is a retrieval-only
    vector lookup (~0.4 s warm, no LLM call); ``remember`` runs cognee's
    multi-minute LLM extraction pipeline and contends with every concurrent
    recall for the same stores.

    Background agents — subsessions and periodic session turns —
    run unattended around the clock, so letting them cognify every turn is
    what produced the ~$22/day cognee bill and the write contention that
    slows interactive chat. But there is no reason to deny them *reading*
    what the main conversation has already learned.

    This wrapper is that middle setting: full recall (including the deep
    ``search_memory`` tool, which is forwarded), writes silently dropped.
    """

    def __init__(self, inner: ChatMemory) -> None:
        """Wrap *inner*, exposing its reads and discarding its writes."""
        self._inner = inner

    async def setup(self) -> None:
        """Initialise the wrapped backend."""
        await self._inner.setup()

    async def recall(self, query: str, *, session_id: str | None = None) -> str:
        """Delegate to the wrapped backend — reads are the whole point."""
        return await self._inner.recall(query, session_id=session_id)

    def __getattr__(self, name: str) -> Any:
        """Forward unknown attributes (notably ``recall_deep``) to the backend.

        Dynamic rather than an explicit ``recall_deep`` method so the
        attribute is *absent* when the wrapped backend lacks it — that is
        exactly what ``build_memory_tools`` probes to decide whether to offer
        the ``search_memory`` tool. An explicit method would always be
        present and would advertise a tool that then raises.

        Only reached for names not defined on this class, so the write-side
        overrides above can never be bypassed.
        """
        return getattr(self._inner, name)

    async def remember(
        self,
        user_message: str,
        assistant_message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Discard the exchange — background agents must not cognify."""
        return None

    def status(self) -> dict[str, Any]:
        """Report the wrapped backend's health, flagged read-only."""
        inner_status = dict(self._inner.status())
        inner_status["read_only"] = True
        return inner_status

    def set_recovery_callback(self, callback: RecoverCallback | None) -> None:
        """No-op: recovery is driven by the writing (main-chat) agent."""
        return None

    def set_notify_callback(self, callback: NotifyCallback | None) -> None:
        """No-op: escalation is driven by the writing (main-chat) agent."""
        return None

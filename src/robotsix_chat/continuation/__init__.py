"""Post-restart continuation tools for the agent.

Interrupted sessions now auto-continue on boot — no explicit arming call is
required.  Exposes :func:`build_continuation_tools` — a factory returning the
LLM tools that let the chat agent inspect or cancel a pending continuation
(``cancel_continuation`` / ``get_continuation_status``).  The explicit-arm
``schedule_continuation`` tool has been removed.

Also exposes :func:`load_continuation_skill` which returns the component
skill markdown describing the continuation API surface.  Inject this into
the agent's system prompt so the LLM knows when and how to use it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config.models import ContinuationSettings

__all__ = ["build_continuation_tools", "load_continuation_skill"]


def load_continuation_skill() -> str:
    """Return the continuation component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.
    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_continuation_tools(
    settings: ContinuationSettings,
    continuation_store: Any = None,
) -> list[Callable[..., Any]]:
    """Return the continuation tool(s) for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    # Late import so the module loads even when the store module is absent.
    from robotsix_chat.continuation.store import ContinuationStore

    store: ContinuationStore = (
        continuation_store
        if continuation_store is not None
        else ContinuationStore(
            max_consecutive=settings.max_consecutive,
        )
    )

    async def cancel_continuation() -> str:
        """Cancel any pending scheduled continuation.

        Use this when the work that was going to be continued is no longer
        needed or when the operator manually took over.

        Returns:
            Confirmation or a note that nothing was pending.

        """
        return store.cancel()

    async def get_continuation_status() -> str:
        """Check whether a continuation is currently pending.

        Returns a summary including whether a continuation is armed, which
        session it targets, a preview of the prompt, and the current
        consecutive auto-continuation count versus the guardrail limit.

        Returns:
            A human-readable status summary.

        """
        import json

        return json.dumps(store.pending_info(), indent=2)

    return [
        cancel_continuation,
        get_continuation_status,
    ]

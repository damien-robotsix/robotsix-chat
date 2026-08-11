"""Post-restart continuation tools for the agent.

Exposes :func:`build_continuation_tools` — a factory returning an LLM tool
that lets the chat agent arm a continuation that fires automatically after
the next restart, so work-in-progress resumes without human intervention.

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
            path=settings.store_path,
            max_consecutive=settings.max_consecutive,
        )
    )

    async def schedule_continuation(session_id: str, prompt: str) -> str:
        """Schedule a continuation that fires automatically after the next restart.

        Use this BEFORE calling ``self_restart`` so the current work resumes
        without human intervention.  The stored prompt is injected into the
        conversation as if the operator had sent it, so the agent picks up
        right where it left off.

        Only ONE continuation can be pending at a time — calling this again
        overwrites any previously scheduled continuation.

        The continuation is one-shot: it fires once on the next boot and is
        then consumed.  A guardrail blocks automatic firing after
        ``max_consecutive`` consecutive auto-continuations to prevent restart
        loops.

        Args:
            session_id: The session ID to continue after restart.
            prompt: The prompt to inject as if the operator sent it (e.g.
                "resume: finish deploying component X and verify").

        Returns:
            Confirmation or error message.

        """
        return store.schedule(session_id, prompt)

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
        schedule_continuation,
        cancel_continuation,
        get_continuation_status,
    ]

"""Per-session model escalation for the chat agent.

The chat agent runs every session at the server's configured level.
When it judges a task beyond that tier it calls
``escalate_model``, which pins *this session only* to the frontier tier.
The pin is persisted on the session and sticky for its
lifetime: a conversation that needed the stronger model usually keeps needing
it, and switching back and forth would rebuild the provider's prompt cache
each time.

Escalation takes effect on the **next** turn.  The provider for the current
turn is built before any tool runs, so the model cannot swap itself out
mid-reply — the tool says so in its result, and the agent is instructed to
tell the user rather than silently promising a better answer it cannot give
until the next message.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from robotsix_chat.chat.events import EventSink, session_model_frame
from robotsix_chat.config.constants import FRONTIER_MODEL_LEVEL, level_display_name

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robotsix_chat.chat.conversation import ConversationStore

__all__ = ["build_escalation_tools"]


def build_escalation_tools(
    *,
    conversation_store: ConversationStore | None,
    session_id: str,
    configured_level: int,
    event_sink: EventSink | None = None,
    tier_config: Any | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``escalate_model`` tool for *session_id*.

    Returns ``[]`` when there is no store to persist the pin to, or when the
    session already runs at or above the frontier tier — exposing a tool that
    can only report "nothing to do" wastes a tool slot and invites the model
    to call it.
    """
    if conversation_store is None:
        return []

    current = conversation_store.get_model_level(session_id) or configured_level
    if current >= FRONTIER_MODEL_LEVEL:
        return []

    async def escalate_model(reason: str) -> str:
        """Switch this conversation to the strongest available model.

        Call this when you have genuinely tried and cannot solve the user's
        problem at your current capability — a reasoning step you cannot
        complete, an analysis you keep getting wrong, or a task you have
        already attempted and failed. The switch is permanent for this
        conversation and the stronger model costs substantially more, so it
        is not a shortcut to reach for at the first sign of difficulty.

        Do NOT call it for: work that is merely long or tedious, anything a
        tool call would answer, a task you have not yet attempted, or because
        the user asked a hard-sounding question. Try first.

        The switch applies from the user's NEXT message — it cannot change
        the model mid-reply. After calling it, finish the current turn as
        best you can and tell the user plainly that you have switched and why,
        so they can re-ask if your current answer falls short.

        Args:
            reason: One sentence on what you could not do at the current
                capability. Shown to the user, so make it specific and
                honest ("cannot resolve the lock-ordering interaction in
                this scheduler"), not vague ("task is complex").

        Returns:
            A confirmation naming the model that serves the next turn.

        """
        if not conversation_store.set_model_level(session_id, FRONTIER_MODEL_LEVEL):
            return (
                "Escalation failed: this session is no longer known to the "
                "server. Continue at the current model."
            )

        name = level_display_name(FRONTIER_MODEL_LEVEL, tier_config)
        if event_sink is not None:
            event_sink.publish(
                session_id,
                session_model_frame(
                    session_id=session_id,
                    model_level=FRONTIER_MODEL_LEVEL,
                    model_name=name,
                    escalated=True,
                    reason=reason,
                ),
            )
        return (
            f"Escalated to {name} for the rest of this conversation. It serves "
            "the NEXT message, not this one — finish this turn as best you can "
            "and tell the user you have switched, and why."
        )

    return [escalate_model]

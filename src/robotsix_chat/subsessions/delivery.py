"""Parent delivery — routes subsession summaries/results to their parent.

Replaces the old ``ConversationDeliveryChannel``.  Two destinations:

* **Parent is the main chat session** (``parent_id is None``): the main
  agent runs a real turn reacting to the outcome (see
  :meth:`ParentDelivery._react_in_main_chat`) — under the owner's
  :class:`~robotsix_chat.chat.server.routes.RunSerializer` lock so it never
  interleaves with a ``/chat`` run's read-history/record window. The reply
  is recorded to history and, when an event sink is wired, pushed live to a
  connected browser as an ``agent_message`` frame (there is no open
  ``/chat`` request to carry it). Until :meth:`ParentDelivery.set_agent` has
  been called, or if the reaction turn itself fails, this degrades to the
  old passive record (the outcome as a synthetic turn) so it is never lost.

* **Parent is another subsession**: the summary is enqueued into the
  parent's inbox (role ``"parent"``) and shows up at its next turn
  boundary.  When the parent is no longer active, delivery degrades to
  the main-chat path so the outcome is never lost.

Subsession agents carry the full tool suite themselves, so a *nested*
parent's summary is still delivered as data, not by re-running a second
agent — only the main-chat-parent case gets a live reaction turn, since
that is the one a human is actually watching.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from robotsix_chat.chat.events import agent_message_frame

from .models import SubsessionInfo

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.events import EventSink
    from robotsix_chat.chat.server.routes import ChatAgent, RunSerializer

    from .registry import SubsessionRegistry

logger = logging.getLogger(__name__)

_REACT_PROMPT_TEMPLATE = (
    "[System notice] Subsession {sub_id} ({kind}) '{title}' {reason} while "
    "you were not actively conversing with the user. Outcome:\n\n{outcome}\n\n"
    "React to this for the user now — comment on it, continue the work if "
    "appropriate, or just acknowledge it briefly. This is a real turn: your "
    "reply will be shown to the user."
)


class ParentDelivery:
    """Deliver subsession outcomes to the conversation that spawned them."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        registry: SubsessionRegistry,
        run_serializer: RunSerializer,
        event_sink: EventSink | None = None,
    ) -> None:
        """Wire the store, registry, per-owner run serializer, and event sink.

        *event_sink*, when given, receives an ``agent_message`` frame each
        time a main-chat-parent reaction turn (see :meth:`_react_in_main_chat`)
        produces a reply, so a connected browser can show it live instead of
        only picking it up on the next ``GET /history``.
        """
        self._store = conversation_store
        self._registry = registry
        self._run_serializer = run_serializer
        self._event_sink = event_sink
        # Set after construction via set_agent(): the main ChatAgent is built
        # from a SubsessionEnv that itself needs this ParentDelivery, so the
        # two can't be constructed in agent-first order (see set_agent).
        self._agent: ChatAgent | None = None

    def set_agent(self, agent: ChatAgent) -> None:
        """Wire the main chat agent used to react to subsession outcomes.

        Call once, after both this ``ParentDelivery`` and the main agent
        exist — the constructor can't take *agent* directly because
        building the main agent requires a ``SubsessionEnv`` that itself
        embeds this ``ParentDelivery`` (chicken-and-egg). Until this is
        called, main-chat-parent delivery degrades to a passive history
        record instead of a live reaction turn.
        """
        self._agent = agent

    async def deliver_summary(
        self, info: SubsessionInfo, summary: str, reason: str
    ) -> None:
        """Deliver a terminal *summary* to *info*'s parent (see module doc).

        Best-effort: failures are logged, never raised back into a worker.
        """
        label = (
            f"[Subsession {info.id[:8]} ({info.kind.value}) '{info.title}' {reason}]"
        )
        try:
            if info.parent_id is not None and self._registry.enqueue_message(
                info.parent_id, "parent", f"{label} {summary}"
            ):
                return
            # Main-chat parent, or nested parent already terminal → degrade
            # to the owning session so the outcome is never lost.
            await self._react_in_main_chat(info, summary, reason, label)
        except Exception:
            logger.exception(
                "Failed to deliver subsession %s summary to its parent", info.id
            )

    async def deliver_result(self, info: SubsessionInfo, run: int, text: str) -> None:
        """Deliver one non-suppressed periodic run result to the parent.

        Same routing as :meth:`deliver_summary`; the UI additionally gets
        a ``subsession_result`` frame from the worker (via the registry's
        event sink) for the notification bubble.
        """
        label = f"[Subsession {info.id[:8]} '{info.title}' run {run}]"
        try:
            if info.parent_id is not None and self._registry.enqueue_message(
                info.parent_id, "parent", f"{label} {text}"
            ):
                return
            await self._react_in_main_chat(info, text, f"run {run}", label)
        except Exception:
            logger.exception("Failed to deliver subsession %s run result", info.id)

    async def _react_in_main_chat(
        self, info: SubsessionInfo, outcome: str, reason: str, label: str
    ) -> None:
        """Have the main agent react to *outcome* in its own session.

        Runs a real agent turn (not just a passive history record) so the
        agent actually processes what happened and can comment on or
        continue from it, then pushes the reply live to a connected browser
        via ``agent_message_frame`` — there is no open ``/chat`` request to
        carry it, since this isn't a live user turn.

        Degrades to the old passive record (*label* as the "user" turn,
        *outcome* as the "assistant" reply) when no agent is wired yet (see
        :meth:`set_agent`) or the reaction turn itself fails — the outcome
        must never be silently lost either way.
        """
        session_id = info.owner_session_id
        if self._agent is None:
            async with self._run_serializer.for_owner(session_id):
                self._store.record_for_session(session_id, label, outcome)
            return

        prompt = _REACT_PROMPT_TEMPLATE.format(
            sub_id=info.id[:8],
            kind=info.kind.value,
            title=info.title,
            reason=reason,
            outcome=outcome,
        )
        async with self._run_serializer.for_owner(session_id):
            history = self._store.history(session_id)
            try:
                parts = [
                    chunk
                    async for chunk in self._agent.stream(
                        prompt,
                        history=history or None,
                        session_id=session_id,
                        client_id=session_id,
                        trace_metadata={"subsession_id": info.id},
                    )
                ]
            except Exception:
                logger.exception(
                    "Reaction turn failed for subsession %s (session %s)",
                    info.id,
                    session_id,
                )
                self._store.record_for_session(session_id, label, outcome)
                return
            reply = "".join(parts)
            self._store.record_for_session(session_id, prompt, reply)
            if reply and self._event_sink is not None:
                self._event_sink.publish(
                    session_id, agent_message_frame(reply, time.time())
                )

"""Parent delivery — routes subsession summaries/results to their parent.

Replaces the old ``ConversationDeliveryChannel``.  Two destinations:

* **Parent is the main chat session** (``parent_id is None``): the
  summary is recorded as a synthetic turn into the exact owning session
  via ``ConversationStore.record_for_session`` — under the owner's
  :class:`~robotsix_chat.chat.server.routes.RunSerializer` lock so it
  never interleaves with a ``/chat`` run's read-history/record window.
  The browser learns about it from the ``subsession_closed`` /
  ``subsession_result`` SSE frame the registry publishes; ``GET
  /history`` re-sync covers a closed browser.

* **Parent is another subsession**: the summary is enqueued into the
  parent's inbox (role ``"parent"``) and shows up at its next turn
  boundary.  When the parent is no longer active, delivery degrades to
  the main-chat path so the outcome is never lost.

Unlike the old channel there is no tick-triggered foreground agent run:
subsession agents carry the full tool suite themselves, so results are
delivered as data, not by re-running a second agent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .models import SubsessionInfo

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.server.routes import RunSerializer

    from .registry import SubsessionRegistry

logger = logging.getLogger(__name__)


class ParentDelivery:
    """Deliver subsession outcomes to the conversation that spawned them."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        registry: SubsessionRegistry,
        run_serializer: RunSerializer,
    ) -> None:
        """Wire the store, registry, and per-owner run serializer."""
        self._store = conversation_store
        self._registry = registry
        self._run_serializer = run_serializer

    async def deliver_summary(
        self, info: SubsessionInfo, summary: str, reason: str
    ) -> None:
        """Deliver a terminal *summary* to *info*'s parent (see module doc).

        Best-effort: failures are logged, never raised back into a worker.
        """
        label = (
            f"[Subsession {info.id[:8]} ({info.kind.value}) "
            f"'{info.title}' {reason}]"
        )
        try:
            if info.parent_id is not None and self._registry.enqueue_message(
                info.parent_id, "parent", f"{label} {summary}"
            ):
                return
            # Main-chat parent, or nested parent already terminal → degrade
            # to the owning session so the outcome is never lost.
            async with self._run_serializer.for_owner(info.owner_session_id):
                self._store.record_for_session(
                    info.owner_session_id, label, summary
                )
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
            async with self._run_serializer.for_owner(info.owner_session_id):
                self._store.record_for_session(info.owner_session_id, label, text)
        except Exception:
            logger.exception(
                "Failed to deliver subsession %s run result", info.id
            )

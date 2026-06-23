"""In-memory multi-turn conversation tracking for the chat server.

The chat agent is stateless per call, so on its own it treats every message as a
brand-new conversation. :class:`ConversationStore` adds short-lived continuity:
it keys conversations by a per-browser ``client_id`` and, for messages that
arrive within an idle window, replays the prior turns to the agent and keeps
them under one trace *session id*. After the idle window a fresh conversation
starts — a new session id with empty history — so an abandoned chat that's
resumed 30 minutes later is correctly a new conversation (and a new trace),
exactly as the agent would treat it anyway.

The store is process-local and unsynchronised: it is sized for the single-worker
``uvicorn.run`` the server uses. Running multiple workers would split a client's
conversation across processes — acceptable degradation (each worker just sees
fewer turns), never corruption.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

# A single exchanged turn: ``(user_message, assistant_reply)``.
Turn = tuple[str, str]


@dataclass
class _Conversation:
    """One client's live conversation: session id, recent turns, last-activity."""

    session_id: str
    last_activity: float
    turns: list[Turn] = field(default_factory=list)


class ConversationStore:
    """Track per-client conversation history with idle-based reset.

    Conversations are keyed by an opaque ``client_id`` (supplied by the
    browser). A conversation resets — new session id, empty history — when more
    than ``idle_reset_seconds`` elapse between messages. History is capped at
    ``max_history_turns`` recent turns and the number of tracked clients at
    ``max_conversations`` (LRU eviction), so the store stays bounded.
    """

    def __init__(
        self,
        *,
        idle_reset_seconds: float = 1800.0,
        max_history_turns: int = 20,
        max_conversations: int = 1000,
        clock: Callable[[], float] = time.monotonic,
        session_factory: Callable[[], str] | None = None,
    ) -> None:
        """Configure idle/size bounds and the clock + session-id factory."""
        self._idle_reset_seconds = idle_reset_seconds
        self._max_history_turns = max_history_turns
        self._max_conversations = max_conversations
        self._clock = clock
        self._session_factory = session_factory or (lambda: uuid.uuid4().hex)
        # Insertion-ordered so the oldest-touched conversation is evicted first.
        self._conversations: OrderedDict[str, _Conversation] = OrderedDict()

    def new_session_id(self) -> str:
        """Return a fresh trace session id (for untracked / ephemeral callers)."""
        return self._session_factory()

    def begin(self, client_id: str) -> tuple[str, list[Turn]]:
        """Start handling a message for *client_id*.

        Returns the conversation's current ``(session_id, history)`` where
        *history* is a snapshot of the recent turns to replay to the agent.
        When the client is new, or has been idle longer than the configured
        window, a new conversation is started (fresh session id, empty history).
        """
        now = self._clock()
        conversation = self._conversations.get(client_id)
        if conversation is None or self._is_expired(conversation, now):
            conversation = _Conversation(
                session_id=self._session_factory(), last_activity=now
            )
            self._conversations[client_id] = conversation
        else:
            conversation.last_activity = now

        self._conversations.move_to_end(client_id)
        self._evict_overflow()
        # Return a copy so the caller can't mutate stored state by reference.
        return conversation.session_id, list(conversation.turns)

    def record(self, client_id: str, user_message: str, assistant_reply: str) -> None:
        """Append a completed exchange to *client_id*'s conversation.

        Called after the agent's reply is fully streamed. Trims history to the
        configured cap. If the client was evicted between :meth:`begin` and now,
        the turn is dropped (its session simply won't accumulate) rather than
        resurrecting an evicted conversation.
        """
        conversation = self._conversations.get(client_id)
        if conversation is None:
            return
        conversation.turns.append((user_message, assistant_reply))
        if len(conversation.turns) > self._max_history_turns:
            del conversation.turns[: -self._max_history_turns]
        conversation.last_activity = self._clock()
        self._conversations.move_to_end(client_id)

    def history(self, client_id: str) -> list[Turn]:
        """Return a snapshot copy of *client_id*'s recorded turns.

        Read-only: does not update last-activity, LRU order, or reset an
        expired conversation. Returns an empty list for unknown clients.
        """
        conversation = self._conversations.get(client_id)
        if conversation is None:
            return []
        return list(conversation.turns)

    def _is_expired(self, conversation: _Conversation, now: float) -> bool:
        return now - conversation.last_activity > self._idle_reset_seconds

    def _evict_overflow(self) -> None:
        while len(self._conversations) > self._max_conversations:
            self._conversations.popitem(last=False)

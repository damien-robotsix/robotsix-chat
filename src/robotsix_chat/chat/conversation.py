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

import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

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
        max_history_turns: int = 50,
        max_conversations: int = 1000,
        clock: Callable[[], float] = time.monotonic,
        session_factory: Callable[[], str] | None = None,
        persist_path: Path | None = None,
    ) -> None:
        """Configure store bounds, clock, session factory, and optional disk persist.

        When *persist_path* is given, the store loads any previously-saved
        conversations on startup and writes the full state to that JSON file
        after every ``record()`` — so chat history survives a container
        restart when the path is on a persistent volume mount (e.g.
        ``.data/conversations.json``).
        """
        self._idle_reset_seconds = idle_reset_seconds
        self._max_history_turns = max_history_turns
        self._max_conversations = max_conversations
        self._clock = clock
        self._session_factory = session_factory or (lambda: uuid.uuid4().hex)
        self._persist_path = persist_path
        # Insertion-ordered so the oldest-touched conversation is evicted first.
        self._conversations: OrderedDict[str, _Conversation] = OrderedDict()
        if self._persist_path is not None:
            self._load_from_disk()

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

        When a *persist_path* was configured, writes the full store state to
        disk after recording.
        """
        conversation = self._conversations.get(client_id)
        if conversation is None:
            return
        conversation.turns.append((user_message, assistant_reply))
        if len(conversation.turns) > self._max_history_turns:
            del conversation.turns[: -self._max_history_turns]
        conversation.last_activity = self._clock()
        self._conversations.move_to_end(client_id)
        if self._persist_path is not None:
            self._persist()

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

    # -- on-disk persistence ----------------------------------------------

    def _load_from_disk(self) -> None:
        """Restore conversations from the persist file (best-effort)."""
        if self._persist_path is None:  # only called when configured
            return
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return  # first run — no saved state yet
        except (OSError, json.JSONDecodeError):
            logger.exception(
                "Failed to load conversation history from %s", self._persist_path
            )
            return

        now = self._clock()
        if not isinstance(raw, dict):
            return
        for client_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            turns_raw = entry.get("turns")
            if not isinstance(turns_raw, list):
                continue
            turns: list[Turn] = []
            for t in turns_raw:
                if isinstance(t, list) and len(t) == 2:
                    turns.append((str(t[0]), str(t[1])))
            if not turns:
                continue
            # Cap to the configured limit on restore (the limit may have
            # been lowered since the data was saved).
            if len(turns) > self._max_history_turns:
                turns = turns[-self._max_history_turns :]
            self._conversations[client_id] = _Conversation(
                session_id=str(entry.get("session_id", self._session_factory())),
                last_activity=now,  # reset activity clock on load
                turns=turns,
            )

    def _persist(self) -> None:
        """Write the full conversation state to the persist file."""
        if self._persist_path is None:
            return
        data: dict[str, dict[str, object]] = {}
        for client_id, conv in self._conversations.items():
            data[client_id] = {
                "session_id": conv.session_id,
                "turns": [list(t) for t in conv.turns],
            }
        try:
            self._persist_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            logger.exception(
                "Failed to persist conversation history to %s", self._persist_path
            )

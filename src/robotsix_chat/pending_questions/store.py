"""In-memory per-session pending-questions store.

Tracks every agent-raised question the user hasn't answered yet.
Mutations publish lifecycle frames through the shared EventBus so
connected browsers update in real time.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robotsix_chat.chat.events import EventBus


@dataclass
class PendingQuestion:
    """One pending question the agent is waiting on the user to answer."""

    question_id: str
    session_id: str
    text: str
    detail: str = ""
    status: str = "pending"
    created_at: float = 0.0


class PendingQuestionsStore:
    """In-memory, per-session store of pending questions.

    Questions are keyed by ``(session_id, question_id)``.  Every
    add / update / remove publishes an SSE frame via the optional
    *event_bus* so the frontend panel stays live.

    When *event_bus* is ``None``, mutations are silent — useful
    for testing without a running event loop.
    """

    def __init__(self, event_bus: EventBus | None = None) -> None:
        """Create a store, optionally wired to *event_bus* for live SSE broadcasts."""
        self._event_bus = event_bus
        # session_id → {question_id → PendingQuestion}
        self._questions: dict[str, dict[str, PendingQuestion]] = defaultdict(dict)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def add(
        self,
        session_id: str,
        text: str,
        detail: str = "",
        *,
        wall_clock: float | None = None,
    ) -> PendingQuestion:
        """Create a new pending question and return it.

        Raises ``ValueError`` when *text* is empty.
        """
        import time

        if not text.strip():
            raise ValueError("question text must not be empty")

        question_id = uuid.uuid4().hex[:12]
        entry = PendingQuestion(
            question_id=question_id,
            session_id=session_id,
            text=text.strip(),
            detail=detail.strip(),
            status="pending",
            created_at=wall_clock if wall_clock is not None else time.time(),
        )
        self._questions[session_id][question_id] = entry
        self._publish(_added_frame(entry))
        return entry

    def update(
        self,
        question_id: str,
        *,
        text: str | None = None,
        detail: str | None = None,
        status: str | None = None,
    ) -> PendingQuestion | None:
        """Update fields on an existing pending question by id.

        Only supplied fields are changed; ``None`` means "leave unchanged".
        Returns the updated entry, or ``None`` when *question_id* is unknown.
        """
        entry = self._find(question_id)
        if entry is None:
            return None
        if text is not None:
            entry.text = text.strip()
        if detail is not None:
            entry.detail = detail.strip()
        if status is not None:
            entry.status = status
        self._publish(_updated_frame(entry))
        return entry

    def remove(self, question_id: str) -> PendingQuestion | None:
        """Remove a pending question by id.

        Returns the removed entry (so callers can inspect it), or ``None``
        when *question_id* is unknown.
        """
        entry = self._find(question_id)
        if entry is None:
            return None
        del self._questions[entry.session_id][question_id]
        self._publish(_removed_frame(entry))
        return entry

    def list_for_session(self, session_id: str) -> list[PendingQuestion]:
        """Return a snapshot of all pending questions for *session_id*.

        The list is a copy — mutating it does not affect the store.
        Most-recently-added questions come last.
        """
        return list(self._questions.get(session_id, {}).values())

    def get(self, question_id: str) -> PendingQuestion | None:
        """Look up a single question by id."""
        return self._find(question_id)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _find(self, question_id: str) -> PendingQuestion | None:
        """Linear scan across all sessions — cheap at expected scale."""
        for session_entries in self._questions.values():
            entry = session_entries.get(question_id)
            if entry is not None:
                return entry
        return None

    def _publish(self, frame: dict[str, object]) -> None:
        if self._event_bus is None:
            return
        session_id = str(frame.get("session_id", ""))
        if session_id:
            self._event_bus.publish(session_id, frame)


# ------------------------------------------------------------------
# frame builders
# ------------------------------------------------------------------


def _added_frame(entry: PendingQuestion) -> dict[str, object]:
    return {
        "type": "pending_question_added",
        "question_id": entry.question_id,
        "session_id": entry.session_id,
        "text": entry.text,
        "detail": entry.detail,
        "status": entry.status,
        "created_at": entry.created_at,
    }


def _updated_frame(entry: PendingQuestion) -> dict[str, object]:
    return {
        "type": "pending_question_updated",
        "question_id": entry.question_id,
        "session_id": entry.session_id,
        "text": entry.text,
        "detail": entry.detail,
        "status": entry.status,
        "created_at": entry.created_at,
    }


def _removed_frame(entry: PendingQuestion) -> dict[str, object]:
    return {
        "type": "pending_question_removed",
        "question_id": entry.question_id,
        "session_id": entry.session_id,
    }

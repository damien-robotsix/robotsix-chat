"""Local, durable knowledge store for agent-authored operational notes.

A :class:`KnowledgeStore` persists structured notes to a single JSON file on
disk — default ``/data/knowledge.json`` — with best-effort atomic-ish writes.
On load it tolerates a missing, empty, or corrupt file by starting empty,
and forward-compatibly defaults missing keys to ``None``.

This is the backing store for the agent's deliberate, explicit note-taking;
it is independent of the cognee ``memory/`` package and of the human-governed
system prompt.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from robotsix_chat.common.json_store import JsonStoreBase

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeEntry:
    """A single agent-authored note."""

    id: str
    topic: str
    content: str
    created_at: str
    updated_at: str


class KnowledgeStore(JsonStoreBase[KnowledgeEntry]):
    """Persist agent-authored notes to ``/data/knowledge.json`` (or custom path).

    Construct with an overridable ``path`` and ``clock`` injectable (defaults
    to ``datetime.now(timezone.utc)``) so tests can pin timestamps.

    Methods that reference a non-existent ``note_id`` return a clear error
    string — they never raise an unhandled exception that would bubble to the
    agent.  ``add`` / ``append`` / ``update`` raise only on fundamental issues
    (e.g. disk full, permissions), which the tool layer wraps.
    """

    _store_name = "knowledge store"
    _default_path = "/data/knowledge.json"

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def add(self, topic: str, content: str) -> KnowledgeEntry:
        """Create a new note; returns the entry (its ``id`` is ``uuid4().hex``)."""
        now = self._clock().isoformat()
        entry = KnowledgeEntry(
            id=uuid.uuid4().hex,
            topic=topic,
            content=content,
            created_at=now,
            updated_at=now,
        )
        self._items[entry.id] = entry
        self._persist()
        return entry

    def append(self, note_id: str, content: str) -> KnowledgeEntry:
        """Concatenate *content* to the existing note's content.

        Returns the updated entry.  Returns a **plain ``KnowledgeEntry`` with
        ``id="error"`` and an error message in ``content``** when *note_id* is
        unknown — callers should check ``entry.id`` before using it.
        """
        entry = self._items.get(note_id)
        if entry is None:
            return KnowledgeEntry(
                id="error",
                topic="",
                content=f"Error: no knowledge note found with id '{note_id}'",
                created_at="",
                updated_at="",
            )
        entry.content = entry.content + content
        entry.updated_at = self._clock().isoformat()
        self._persist()
        return entry

    def update(self, note_id: str, content: str) -> KnowledgeEntry:
        """Replace the existing note's content entirely.

        Returns the updated entry or an error entry when *note_id* is unknown.
        """
        entry = self._items.get(note_id)
        if entry is None:
            return KnowledgeEntry(
                id="error",
                topic="",
                content=f"Error: no knowledge note found with id '{note_id}'",
                created_at="",
                updated_at="",
            )
        entry.content = content
        entry.updated_at = self._clock().isoformat()
        self._persist()
        return entry

    def list(self, topic: str = "") -> list[KnowledgeEntry]:
        """Return all notes, optionally filtered by *topic* (case-insensitive)."""
        if not topic:
            return list(self._items.values())
        t = topic.strip().lower()
        return [e for e in self._items.values() if e.topic.strip().lower() == t]

    def get(self, note_id: str) -> KnowledgeEntry | None:
        """Return the entry for *note_id*, or ``None`` if unknown."""
        return self._items.get(note_id)
# append (line 78)
if entry is None:
    return KnowledgeEntry(
        id="error",
        topic="",
        content=f"Error: no knowledge note found with id '{note_id}'",
        created_at="",
        updated_at="",
    )

# update (line 95) — identical block
if entry is None:
    return KnowledgeEntry(
        id="error",
        topic="",
        content=f"Error: no knowledge note found with id '{note_id}'",
        created_at="",
        updated_at="",
    )
# append (line 78)
if entry is None:
    return KnowledgeEntry(
        id="error",
        topic="",
        content=f"Error: no knowledge note found with id '{note_id}'",
        created_at="",
        updated_at="",
    )

# update (line 95) — identical block
if entry is None:
    return KnowledgeEntry(
        id="error",
        topic="",
        content=f"Error: no knowledge note found with id '{note_id}'",
        created_at="",
        updated_at="",
    )
# append (line 78)
if entry is None:
    return KnowledgeEntry(
        id="error",
        topic="",
        content=f"Error: no knowledge note found with id '{note_id}'",
        created_at="",
        updated_at="",
    )

# update (line 95) — identical block
if entry is None:
    return KnowledgeEntry(
        id="error",
        topic="",
        content=f"Error: no knowledge note found with id '{note_id}'",
        created_at="",
        updated_at="",
    )

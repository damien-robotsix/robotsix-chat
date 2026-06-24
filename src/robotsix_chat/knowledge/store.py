"""Local, durable knowledge store for agent-authored operational notes.

A :class:`KnowledgeStore` persists structured notes to a single JSON file on
disk — default ``.data/knowledge.json`` — with best-effort atomic-ish writes.
On load it tolerates a missing, empty, or corrupt file by starting empty,
and forward-compatibly defaults missing keys to ``None``.

This is the backing store for the agent's deliberate, explicit note-taking;
it is independent of the cognee ``memory/`` package and of the human-governed
system prompt.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeEntry:
    """A single agent-authored note."""

    id: str
    topic: str
    content: str
    created_at: str
    updated_at: str


class KnowledgeStore:
    """Persist agent-authored notes to ``.data/knowledge.json`` (or custom path).

    Construct with an overridable ``path`` and ``clock`` injectable (defaults
    to ``datetime.now(timezone.utc)``) so tests can pin timestamps.

    Methods that reference a non-existent ``note_id`` return a clear error
    string — they never raise an unhandled exception that would bubble to the
    agent.  ``add`` / ``append`` / ``update`` raise only on fundamental issues
    (e.g. disk full, permissions), which the tool layer wraps.
    """

    _STORE_DIR = ".data"

    def __init__(
        self,
        path: str | Path = ".data/knowledge.json",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a store persisting to *path*.

        *clock* overrides the timestamp source (default ``datetime.now(UTC)``)
        so tests can pin time deterministically.
        """
        self._path = Path(path)
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._entries: dict[str, KnowledgeEntry] = {}
        self._load()

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
        self._entries[entry.id] = entry
        self._persist()
        return entry

    def append(self, note_id: str, content: str) -> KnowledgeEntry:
        """Concatenate *content* to the existing note's content.

        Returns the updated entry.  Returns a **plain ``KnowledgeEntry`` with
        ``id="error"`` and an error message in ``content``** when *note_id* is
        unknown — callers should check ``entry.id`` before using it.
        """
        entry = self._entries.get(note_id)
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
        entry = self._entries.get(note_id)
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
            return list(self._entries.values())
        t = topic.strip().lower()
        return [e for e in self._entries.values() if e.topic.strip().lower() == t]

    def get(self, note_id: str) -> KnowledgeEntry | None:
        """Return the entry for *note_id*, or ``None`` if unknown."""
        return self._entries.get(note_id)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Write all entries to the JSON store (best-effort atomic)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create parent dir for %s", self._path)
            return

        entries = [
            {
                "id": e.id,
                "topic": e.topic,
                "content": e.content,
                "created_at": e.created_at,
                "updated_at": e.updated_at,
            }
            for e in self._entries.values()
        ]
        # Write to a temp file then rename for atomic-ish behaviour.
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            tmp_path.replace(self._path)
        except OSError:
            logger.exception("Failed to persist knowledge store to %s", self._path)

    def _load(self) -> None:
        """Load entries from disk; tolerate missing/empty/corrupt file."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "Could not read knowledge store file %s; starting empty", self._path
            )
            return

        if not isinstance(raw, list):
            return

        for item in raw:
            if not isinstance(item, dict):
                continue
            entry = KnowledgeEntry(
                id=item.get("id", ""),
                topic=item.get("topic", ""),
                content=item.get("content", ""),
                created_at=item.get("created_at", ""),
                updated_at=item.get("updated_at", ""),
            )
            if entry.id:
                self._entries[entry.id] = entry

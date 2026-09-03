"""Writable knowledge-base tools for the agent.

Exposes :func:`build_knowledge_tools` — a factory that returns **six**
plain undecorated ``async def`` tools the agent can call to add, append to,
update, list, search, and read back durable notes across sessions.  Returns
no tools when knowledge is disabled.

This is the agent's **deliberate, explicit, curated** store of operational
notes and lessons — entirely local JSON, no embeddings, no external service,
always-on.  It is independent of both the **human-governed system prompt**
(which the agent must never modify) and the **long-term memory component**
(which automatically recalls whole conversations by similarity — see
``src/robotsix_chat/memory/``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import KnowledgeSettings

    from .store import KnowledgeStore

logger = logging.getLogger(__name__)

__all__ = ["build_knowledge_tools"]

# Maximum content length returned by list_knowledge_notes snippets.
_LIST_SNIPPET_LENGTH = 200


def _format_entries(entries: list[Any]) -> str:
    """Format a list of KnowledgeEntry objects as a multi-line summary string."""
    lines: list[str] = []
    for e in entries:
        snippet = e.content.replace("\n", " ")
        if len(snippet) > _LIST_SNIPPET_LENGTH:
            snippet = snippet[:_LIST_SNIPPET_LENGTH].rstrip() + "…"
        lines.append(
            f"[{e.id}] {e.topic}\n"
            f"  created: {e.created_at}  updated: {e.updated_at}\n"
            f"  snippet: {snippet}"
        )
    return "\n".join(lines)


def build_knowledge_tools(
    settings: KnowledgeSettings,
    *,
    store: KnowledgeStore | None = None,
) -> list[Callable[..., Any]]:
    """Return the five knowledge-note tools, or ``[]`` when disabled.

    When *store* is given, it is used directly; otherwise a new
    :class:`KnowledgeStore` is created from *settings.path*.
    """
    if not settings.enabled:
        return []

    if store is None:
        from .store import KnowledgeStore

        store = KnowledgeStore(settings.path)

    # ------------------------------------------------------------------
    # tools (plain async def → str, no decorators)
    # ------------------------------------------------------------------

    async def add_knowledge_note(topic: str, content: str) -> str:
        """Create a new durable note in your writable knowledge base.

        Use this to record an operational lesson, discovered fact, or other
        finding you want to recall in future sessions.

        Args:
            topic: Short category label (e.g. ``"config"``, ``"python-pattern"``).
            content: The full note text.

        Returns:
            The new note's ``id`` (keep it for later ``append/update/read``).

        Boundary: this is your deliberate, self-authored operational-note
        store — distinct from the stable, human-governed system prompt
        (which you must not modify) AND distinct from the automatic long-term
        conversation memory (which fuzzily recalls entire past exchanges by
        similarity).  Long-term memory = "what was said before"; this KB = "notes I
        explicitly write and address by id."

        """
        entry = store.add(topic, content)
        return f"Created knowledge note {entry.id} (topic: {entry.topic})"

    async def append_to_knowledge_note(note_id: str, content: str) -> str:
        """Append text to an existing knowledge note.

        Args:
            note_id: The id returned by ``add_knowledge_note`` or
                ``list_knowledge_notes``.
            content: Text to concatenate to the existing content.

        Returns:
            Confirmation with the note id and updated timestamp, or an error
            string when the id is unknown.

        """
        entry = store.append(note_id, content)
        if entry.id == "error":
            return entry.content
        return f"Appended to knowledge note {entry.id} (updated: {entry.updated_at})"

    async def update_knowledge_note(note_id: str, content: str) -> str:
        """Replace the content of an existing knowledge note.

        Args:
            note_id: The id of the note to overwrite.
            content: The new full content for the note.

        Returns:
            Confirmation with the note id and updated timestamp, or an error
            string when the id is unknown.

        """
        entry = store.update(note_id, content)
        if entry.id == "error":
            return entry.content
        return f"Updated knowledge note {entry.id} (updated: {entry.updated_at})"

    async def list_knowledge_notes(topic: str = "") -> str:
        """List knowledge notes, optionally filtered by topic.

        Args:
            topic: Optional category filter. Omit or pass ``""`` to list all.

        Returns:
            A formatted listing with id, topic, timestamps and a content
            snippet for each note, or a message when no notes exist.

        """
        entries = store.list(topic)
        if not entries:
            return "No knowledge notes found." + (f" (topic: {topic})" if topic else "")

        return _format_entries(entries)

    async def search_knowledge_notes(query: str) -> str:
        """Search your knowledge base for notes matching a query.

        Searches both topic and content (case-insensitive).  Results are
        ranked: exact topic match first, then topic contains, then content
        contains.  Use this when you cannot recall the exact note id or
        topic — it finds notes by their actual content.

        Args:
            query: The search term to look for.

        Returns:
            A formatted listing of matching notes with id, topic,
            timestamps and a content snippet, or a message when no matches
            exist.

        """
        entries = store.search(query)
        if not entries:
            return f"No knowledge notes found matching '{query}'."

        return _format_entries(entries)

    async def read_knowledge_note(note_id: str) -> str:
        """Read the full content of a knowledge note by id.

        Args:
            note_id: The id of the note to read.

        Returns:
            The full note content, or an error string when the id is unknown.

        """
        entry = store.get(note_id)
        if entry is None:
            return f"Error: no knowledge note found with id '{note_id}'"
        return entry.content

    return [
        add_knowledge_note,
        append_to_knowledge_note,
        update_knowledge_note,
        list_knowledge_notes,
        search_knowledge_notes,
        read_knowledge_note,
    ]

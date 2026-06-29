"""Pending-questions tool for the agent.

Exposes :func:`build_pending_questions_tools` — a factory returning
LLM tool(s) that let the chat agent manage a real-time "Pending Questions"
panel: add, update, and remove entries the user sees above the chat input.

*session_id* is captured lexically in the returned tool closures so the
tools survive the claude_sdk / MCP boundary — the agent does not need to
pass it as a parameter.

Returns no tools when disabled, so the chat runs exactly as before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import PendingQuestionsSettings
    from robotsix_chat.pending_questions.store import PendingQuestionsStore

logger = logging.getLogger(__name__)

__all__ = ["build_pending_questions_tools"]


def build_pending_questions_tools(
    settings: PendingQuestionsSettings,
    store: PendingQuestionsStore,
    *,
    session_id: str = "",
) -> list[Callable[..., Any]]:
    """Return the pending-questions tool(s) for the agent, or ``[]`` when disabled.

    *session_id* is captured in the returned closures so the agent's tools
    automatically route questions to the correct session.
    """
    if not settings.enabled:
        return []

    sid = session_id

    async def add_pending_question(
        text: str,
        detail: str = "",
    ) -> str:
        """Add a question to the user's Pending Questions panel.

        Use this whenever you need information from the user and want to
        surface it prominently above the chat input so they can answer it
        inline.  Each question gets a real-time entry the user can see
        and respond to.

        Args:
            text: The full question text (required).  This is the primary
                display text the user sees.
            detail: Optional extra context or detail shown alongside the
                question (e.g. why you're asking, what format you need).

        Returns:
            The id of the newly-created pending question.

        """
        if not text.strip():
            return "Error: question text must not be empty"
        entry = store.add(sid, text, detail)
        logger.debug("Added pending question %s", entry.question_id)
        return entry.question_id

    async def update_pending_question(
        question_id: str,
        text: str = "",
        detail: str = "",
        status: str = "",
    ) -> str:
        """Update an existing pending question in the user's panel.

        Use this when you have new information about a question you
        previously raised — e.g. you've partially answered it yourself,
        or you need to revise what you're asking.

        Args:
            question_id: The id returned by ``add_pending_question``.
            text: New question text (leave empty to keep current text).
            detail: New detail / context (leave empty to keep current).
            status: New status string (leave empty to keep current).

        Returns:
            Confirmation message or an error if the id is unknown.

        """
        updates: dict[str, str | None] = {}
        if text:
            updates["text"] = text
        if detail:
            updates["detail"] = detail
        if status:
            updates["status"] = status
        if not updates:
            return (
                "No fields to update — supply at least one of text, detail, or status."
            )

        entry = store.update(question_id, **updates)
        if entry is None:
            return f"Unknown question id: {question_id!r}"
        return f"Updated question {question_id!r}."

    async def remove_pending_question(question_id: str) -> str:
        """Dismiss a pending question — ONLY when the user explicitly asks to.

        Do NOT call this merely because you answered the question: pending
        questions are ongoing mini-chat threads and the user may keep
        replying.  Removing a question deletes its thread and breaks all
        further replies (later thread messages 404).  Only remove it when
        the user clearly signals they are done with it / want it dismissed.
        This permanently removes the question from the panel.

        Args:
            question_id: The id returned by ``add_pending_question``.

        Returns:
            Confirmation message or an error if the id is unknown.

        """
        entry = store.remove(question_id)
        if entry is None:
            return f"Unknown question id: {question_id!r}"
        return f"Removed question {question_id!r}."

    async def list_pending_questions() -> str:
        """List all current pending questions in the user's panel.

        Returns every pending question — including its id, text, status,
        detail, and creation timestamp — so you can pick one to update,
        remove, or re-read with ``get_pending_question``.

        Returns:
            A formatted list of pending questions, or a message that
            there are none.

        """
        entries = store.list_for_session(sid)
        if not entries:
            return "No pending questions."
        lines = [
            f"{e.question_id}  [{e.status}]  {e.text}"
            + (f"  ({e.detail})" if e.detail else "")
            for e in entries
        ]
        return "\n".join(lines)

    async def get_pending_question(question_id: str) -> str:
        """Read a single pending question by its id.

        Use this when you have a question id (e.g. from ``list_pending_questions``)
        and want to inspect its full record — including any answer the user has
        already submitted — before updating or closing it.

        Args:
            question_id: The id returned by ``add_pending_question``.

        Returns:
            A formatted summary of the question including text, detail, status,
            answer (if answered), and timestamps, or an error if the id is
            unknown.

        """
        entry = store.get(question_id)
        if entry is None:
            return f"Unknown question id: {question_id!r}"
        parts = [
            f"id: {entry.question_id}",
            f"status: {entry.status}",
            f"text: {entry.text}",
            f"detail: {entry.detail}",
        ]
        if entry.status == "answered" or entry.answer:
            parts.append(f"answer: {entry.answer}")
        if entry.answered_at:
            parts.append(f"answered_at: {entry.answered_at}")
        parts.append(f"created_at: {entry.created_at}")
        return "\n".join(parts)

    async def append_to_pending_question_thread(
        question_id: str,
        text: str,
    ) -> str:
        """Append a message to the thread of an existing pending question.

        Use this to have a back-and-forth discussion with the user about a
        specific question — ask follow-ups, give clarifying context, or nudge
        the user for a decision.  Messages appear inline in the Pending
        Questions panel.

        Args:
            question_id: The id returned by ``add_pending_question``.
            text: The message text to append (as the assistant).

        Returns:
            Confirmation message or an error if the id is unknown.

        """
        if not text.strip():
            return "Error: thread message must not be empty"
        entry = store.append_to_thread(question_id, "assistant", text)
        if entry is None:
            return f"Unknown question id: {question_id!r}"
        return f"Appended message to thread of question {question_id!r}."

    async def get_pending_question_thread(question_id: str) -> str:
        """Read the full conversation thread of a pending question.

        Use this before appending to the thread so you can see what has
        already been discussed — all messages (user and assistant) in
        chronological order.

        Args:
            question_id: The id returned by ``add_pending_question``.

        Returns:
            A formatted list of thread messages with role, text, and
            timestamps, or an error if the id is unknown.

        """
        thread = store.get_thread(question_id)
        if thread is None:
            return f"Unknown question id: {question_id!r}"
        if not thread:
            return f"No thread messages for question {question_id!r}."
        lines = []
        for msg in thread:
            role_label = msg.role.upper()
            lines.append(f"[{role_label}] {msg.text}")
            if msg.timestamp:
                lines.append(f"  at: {msg.timestamp}")
        return "\n".join(lines)

    return [
        add_pending_question,
        update_pending_question,
        remove_pending_question,
        list_pending_questions,
        get_pending_question,
        append_to_pending_question_thread,
        get_pending_question_thread,
    ]

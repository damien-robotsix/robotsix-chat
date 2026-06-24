"""Factory for the ``read_recent_activity`` agent tool."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.config import SelfReviewSettings

logger = logging.getLogger(__name__)

__all__ = ["build_recent_activity_tools"]


def build_recent_activity_tools(
    settings: SelfReviewSettings,
    store: ConversationStore | None,
) -> list[Callable[..., Any]]:
    """Return the self-review tool(s), or ``[]`` when disabled.

    Returns exactly one async tool — ``read_recent_activity`` — when
    *settings* is enabled and *store* is not ``None``; otherwise ``[]``.
    """
    if settings is None or not settings.enabled or store is None:
        return []

    async def read_recent_activity(limit: int = 20) -> str:
        """Return a digest of recent cross-session conversation activity.

        Reads the live, in-process conversation store — the short-lived
        per-``client_id`` conversation turns — and returns a human-readable
        summary of the most recent activity across all clients and sessions.

        This is **not** the cognee long-term memory. cognee automatically
        recalls past exchanges by similarity; this tool reads the current
        live conversation store directly and is only available when the
        ``self_review`` feature is explicitly enabled in config.

        Args:
            limit: Maximum number of conversations to include (clamped to
                the configured ``recent_activity_limit``).

        Returns:
            A formatted multi-paragraph digest string with per-conversation
            headers (``client_id`` / ``session_id``) and recent turns, or a
            note when no activity is present.

        """
        effective_limit = min(limit, settings.recent_activity_limit)
        entries = store.recent_activity(
            limit=effective_limit,
            max_turns=min(settings.recent_activity_limit, 10),
        )

        if not entries:
            return "No recent conversation activity."

        parts: list[str] = []
        for entry in entries:
            header = f"## {entry['client_id']}  (session: {entry['session_id']})"
            turns: list[tuple[str, str]] = entry["turns"]
            if not turns:
                parts.append(f"{header}\n(no turns)")
                continue
            lines = [header]
            for i, turn in enumerate(turns, 1):
                user_msg, assistant_reply = turn
                user_short = _truncate(user_msg, 120)
                assistant_short = _truncate(assistant_reply, 200)
                lines.append(f"  Turn {i}:")
                lines.append(f"    User: {user_short}")
                lines.append(f"    Assistant: {assistant_short}")
            parts.append("\n".join(lines))

        return "\n\n".join(parts)

    return [read_recent_activity]


def _truncate(text: str, max_chars: int) -> str:
    """Return *text* collapsed to at most *max_chars* characters."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[:max_chars].rstrip() + "\u2026"

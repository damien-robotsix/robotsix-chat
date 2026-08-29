"""Agent-facing cross-session tools for the evergoing session.

:func:`build_cross_session_tools` returns the tool callables that let the
evergoing chat agent become aware of, spawn, and close the operator's other
sessions — the agent-facing wrappers over the session HTTP endpoints
(``GET/POST /sessions``, ``POST /sessions/{id}/close``).  Exposed tools:

* ``list_sessions`` — enumerate the caller-owner's sessions.
* ``create_session`` — spawn a new independent session under the same owner.
* ``close_session`` — close (not delete) an existing session by id.

The caller's owner scope is resolved from the caller's ``session_id``, which
is captured lexically in the closures — tool calls cross the claude_sdk/MCP
boundary where ambient request context does not survive, so identity must be
baked into the closure at build time.

Also exposes :func:`load_cross_session_skill`, which returns the component
skill markdown (``skill.md``) for injection into the agent instruction.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore

__all__ = ["build_cross_session_tools", "load_cross_session_skill"]

logger = logging.getLogger(__name__)


def load_cross_session_skill() -> str:
    """Return the evergoing cross-session component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns an
    empty string when the file is missing, so a missing skill document never
    prevents the agent from starting.
    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_cross_session_tools(
    *,
    conversation_store: ConversationStore,
    session_id: str,
) -> list[Callable[..., Any]]:
    """Build the cross-session awareness tools bound to *session_id*'s owner.

    *session_id* is the caller (evergoing) session; its owner scope is
    resolved lazily on each call via
    :meth:`ConversationStore.owner_for_session`, so a session registered
    after this factory ran is still handled correctly.
    """
    store = conversation_store
    caller_session_id = session_id

    def _owner_id() -> str | None:
        return store.owner_for_session(caller_session_id)

    async def list_sessions() -> str:
        """List every chat session owned by you (the evergoing operator).

        Enumerates all sessions under your owner scope so you can become
        aware of parallel conversations before spawning or closing any.

        Returns:
            A JSON object with ``sessions`` (a list of session-metadata
            dicts — ``session_id``, ``title``, ``last_active``,
            ``turn_count``, ``closed``, ...), ``active_session_id`` (the
            owner's currently-active session), and ``caller_session_id``
            (this evergoing session).  Returns an ``error`` field when the
            caller session has no resolvable owner.

        """
        owner_id = _owner_id()
        if owner_id is None:
            return json.dumps(
                {"error": "caller session has no resolvable owner", "sessions": []}
            )
        sessions, active_id = store.list_sessions(owner_id, create_default=False)
        return json.dumps(
            {
                "sessions": sessions,
                "active_session_id": active_id,
                "caller_session_id": caller_session_id,
            }
        )

    async def create_session() -> str:
        """Spawn a new, independent empty chat session under your owner scope.

        The new session becomes the owner's active session (matching the
        ``POST /sessions`` endpoint).  Use it to start a parallel line of
        work that is tracked separately from this evergoing session.

        Returns:
            A JSON object ``{"created": true, "session": {...}}`` with the
            new session's metadata, or an ``error`` field when the caller
            session has no resolvable owner.

        """
        owner_id = _owner_id()
        if owner_id is None:
            return json.dumps({"error": "caller session has no resolvable owner"})
        meta = store.create_session(owner_id)
        logger.info("cross-session: created session %s", meta.get("session_id"))
        return json.dumps({"created": True, "session": meta})

    async def close_session(target_session_id: str) -> str:
        """Close (not delete) one of your existing sessions by id.

        A closed session keeps its history but can no longer spawn new
        background work.  Closing this evergoing session (the one you are
        running in) is refused.

        Args:
            target_session_id: The id of the session to close (from
                ``list_sessions``).

        Returns:
            A JSON object ``{"closed": true}`` on success, or
            ``{"closed": false, "reason": "..."}`` when the session is not
            found, not owned by you, or is this evergoing session.

        """
        owner_id = _owner_id()
        if owner_id is None:
            return json.dumps(
                {"closed": False, "reason": "caller session has no resolvable owner"}
            )
        if target_session_id == caller_session_id:
            return json.dumps(
                {
                    "closed": False,
                    "reason": (
                        "refusing to close the evergoing session you are running in"
                    ),
                }
            )
        result = store.close_session(owner_id, target_session_id)
        logger.info("cross-session: close_session(%s) -> %s", target_session_id, result)
        return json.dumps(result)

    return [list_sessions, create_session, close_session]

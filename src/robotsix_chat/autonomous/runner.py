"""Autonomous session runner — manages the autonomous lifecycle loop."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from robotsix_chat.autonomous.models import AutonomousState

if TYPE_CHECKING:
    from collections.abc import Callable

    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.events import EventBus
    from robotsix_chat.chat.server.routes.chat import ChatAgent

logger = logging.getLogger(__name__)


class AutonomousRunner:
    """Manages autonomous session lifecycle and auto-cycling.

    Watches autonomous sessions for state transitions and handles the
    auto-respawn loop: when an autonomous session completes, close it
    and create a new autonomous session.
    """

    def __init__(
        self,
        conversation_store: ConversationStore,
        event_bus: EventBus,
        agent_factory: Callable[[], ChatAgent],
    ) -> None:
        """Initialise with store, event bus, and agent factory."""
        self._store = conversation_store
        self._event_bus = event_bus
        self._agent_factory = agent_factory
        self._active_loops: dict[str, asyncio.Task[None]] = {}

    def transition_state(
        self, session_id: str, new_state: AutonomousState
    ) -> bool:
        """Transition an autonomous session to *new_state*.

        Publishes an SSE event on state change. Returns False if the
        session doesn't exist or isn't autonomous.
        """
        session = self._store.get_session(session_id)
        if session is None:
            return False
        if getattr(session, "kind", "chat") != "autonomous":
            return False

        old_state = getattr(session, "autonomous_state", None)
        session.autonomous_state = new_state.value

        # Publish state change event
        self._event_bus.publish(
            session_id,
            {
                "event": "autonomous_state_changed",
                "session_id": session_id,
                "old_state": old_state,
                "new_state": new_state.value,
            },
        )

        logger.info(
            "Autonomous session %s: %s → %s",
            session_id,
            old_state,
            new_state.value,
        )

        # Auto-close + respawn on completion
        if new_state == AutonomousState.COMPLETED:
            self._schedule_completion(session_id)

        return True

    def _schedule_completion(self, session_id: str) -> None:
        """Schedule auto-close and respawn for a completed session."""
        task = asyncio.create_task(
            self._handle_completion(session_id)
        )
        self._active_loops[session_id] = task

    async def _handle_completion(self, session_id: str) -> None:
        """Close the completed session and spawn a new autonomous session."""
        session = self._store.get_session(session_id)
        if session is None:
            return

        owner_id = None
        # Find the owner
        for oid, owner in self._store._owners.items():
            if session_id in owner.session_ids:
                owner_id = oid
                break

        if owner_id is None:
            return

        # Close the completed session
        self._store.close_session(owner_id, session_id)

        # Spawn a new autonomous session
        new_session = self._store.create_session(owner_id, kind="autonomous")
        new_sid: str = str(new_session["session_id"])

        logger.info(
            "Autonomous cycle: closed %s, spawned %s",
            session_id,
            new_sid,
        )

        # Publish respawn event on the new session
        self._event_bus.publish(
            new_sid,
            {
                "event": "autonomous_respawned",
                "session_id": new_sid,
                "previous_session_id": session_id,
            },
        )

        self._active_loops.pop(session_id, None)

    def resume_autonomous_sessions(self) -> None:
        """Resume active autonomous sessions after server restart.

        Sessions in AWAITING_APPROVAL or EXECUTING state are re-published
        so the UI can reconnect.  COMPLETED sessions are auto-closed.
        """
        for session in self._store._sessions.values():
            if getattr(session, "kind", "chat") != "autonomous":
                continue
            state = getattr(session, "autonomous_state", None)
            sid: str = str(session.session_id)
            if state == AutonomousState.COMPLETED.value:
                # Auto-close completed sessions on restart
                self._schedule_completion(sid)
            elif state in (
                AutonomousState.AWAITING_APPROVAL.value,
                AutonomousState.EXECUTING.value,
            ):
                # Re-publish state so UI can reconnect
                self._event_bus.publish(
                    sid,
                    {
                        "event": "autonomous_state_changed",
                        "session_id": sid,
                        "old_state": state,
                        "new_state": state,
                    },
                )

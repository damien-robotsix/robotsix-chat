"""Autonomous session runner — state machine, marker detection, auto-cycling."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from robotsix_chat.autonomous.models import AutonomousSession, AutonomousState

if TYPE_CHECKING:
    from collections.abc import Callable

    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.server.routes import ChatAgent, RunSerializer
    from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)


class AutonomousRunner:
    """Owns the autonomous-session state machine and drives auto-continue loops."""

    def __init__(
        self,
        settings: Settings,
        conversation_store: ConversationStore,
        agent_factory: Callable[[], ChatAgent],
        run_serializer: RunSerializer,
    ) -> None:
        """Create a runner from settings, store, agent factory, and serializer."""
        self._settings = settings
        self._store = conversation_store
        self._agent_factory = agent_factory
        self._run_serializer = run_serializer
        self._sessions: dict[str, AutonomousSession] = {}
        # Strong references to in-flight auto-continue tasks (see asyncio
        # docs warning on create_task and weak references).
        self._auto_tasks: set[asyncio.Task[None]] = set()

    # -- session registry ---------------------------------------------------

    def create_session(
        self,
        owner_id: str,
        session_id: str | None = None,
    ) -> AutonomousSession:
        """Register a new autonomous session, creating a store session if needed."""
        if session_id is None:
            session_id = self._store.new_session_id()
        # Ensure the store has this session.
        self._store.begin(session_id)
        aq = AutonomousSession(
            session_id=session_id,
            owner_id=owner_id,
            state=AutonomousState.selecting_subject,
        )
        self._sessions[session_id] = aq
        return aq

    def is_autonomous(self, session_id: str) -> bool:
        """Return ``True`` when *session_id* is a tracked autonomous session."""
        return session_id in self._sessions

    def get_state(self, session_id: str) -> AutonomousState | None:
        """Return the current state of *session_id*, or ``None`` if not tracked."""
        aq = self._sessions.get(session_id)
        return aq.state if aq else None

    def get_session(self, session_id: str) -> AutonomousSession | None:
        """Return the :class:`AutonomousSession` for *session_id*, or ``None``."""
        return self._sessions.get(session_id)

    def owner_for_session(self, session_id: str) -> str | None:
        """Return the owner_id for *session_id*, or ``None`` if not autonomous."""
        aq = self._sessions.get(session_id)
        return aq.owner_id if aq else None

    # -- marker detection ---------------------------------------------------

    def check_reply_for_markers(
        self,
        session_id: str,
        reply_text: str,
    ) -> AutonomousState | None:
        """Scan *reply_text* for lifecycle markers; transition state on match.

        Returns the new state when a transition occurred, ``None`` otherwise.
        """
        aq = self._sessions.get(session_id)
        if aq is None:
            return None

        approval_marker = self._settings.autonomous.approval_marker
        completion_marker = self._settings.autonomous.completion_marker

        # Check completion first (it terminates the session).
        if completion_marker in reply_text:
            aq.state = AutonomousState.completed
            logger.info(
                "Autonomous session %s completed",
                session_id,
            )
            return AutonomousState.completed

        # Check approval marker.
        if approval_marker in reply_text:
            # Plan text is everything before the marker.
            idx = reply_text.index(approval_marker)
            aq.plan_text = reply_text[:idx].strip()
            aq.state = AutonomousState.awaiting_approval
            logger.info(
                "Autonomous session %s awaiting approval (plan %d chars)",
                session_id,
                len(aq.plan_text),
            )
            return AutonomousState.awaiting_approval

        return None

    # -- approval gate ------------------------------------------------------

    def approve(self, owner_id: str, session_id: str) -> tuple[bool, str]:
        """Approve the plan for *session_id*.

        Returns ``(True, "")`` on success; ``(False, reason)`` on failure
        (unknown session, wrong owner, or wrong state).
        """
        aq = self._sessions.get(session_id)
        if aq is None:
            return False, "session not found"
        if aq.owner_id != owner_id:
            return False, "owner_id mismatch"
        if aq.state is not AutonomousState.awaiting_approval:
            return False, f"session is in state {aq.state.value}, not awaiting_approval"

        aq.state = AutonomousState.executing
        aq.auto_turn_count = 0

        # Schedule auto-continue as a background task.
        task = asyncio.create_task(self._auto_continue(session_id))
        self._auto_tasks.add(task)
        task.add_done_callback(self._auto_tasks.discard)

        logger.info("Autonomous session %s approved — starting execution", session_id)
        return True, ""

    def reject(self, owner_id: str, session_id: str) -> tuple[bool, str]:
        """Reject the plan for *session_id*; reset to subject selection.

        Returns ``(True, "")`` on success; ``(False, reason)`` on failure.
        """
        aq = self._sessions.get(session_id)
        if aq is None:
            return False, "session not found"
        if aq.owner_id != owner_id:
            return False, "owner_id mismatch"
        if aq.state is not AutonomousState.awaiting_approval:
            return False, f"session is in state {aq.state.value}, not awaiting_approval"

        aq.state = AutonomousState.selecting_subject
        aq.plan_text = ""
        logger.info(
            "Autonomous session %s rejected — reset to subject selection",
            session_id,
        )
        return True, ""

    # -- auto-continue loop -------------------------------------------------

    async def _auto_continue(self, session_id: str) -> None:
        """Drive execution turns until completion, re-approval, or turn cap."""
        aq = self._sessions.get(session_id)
        if aq is None:
            return

        owner_id = aq.owner_id
        max_turns = self._settings.autonomous.max_auto_turns

        try:
            while True:
                aq = self._sessions.get(session_id)
                if aq is None or aq.state is not AutonomousState.executing:
                    return

                # Enforce max_auto_turns.
                if aq.auto_turn_count >= max_turns:
                    logger.warning(
                        "Autonomous session %s hit max_auto_turns (%d) — "
                        "reverting to awaiting_approval",
                        session_id,
                        max_turns,
                    )
                    aq.state = AutonomousState.awaiting_approval
                    return

                # Acquire the per-owner run lock.
                async with self._run_serializer.for_owner(owner_id):
                    agent = self._agent_factory()
                    history = self._store.agent_history(session_id)

                    # First turn after approval: explicit proceed message.
                    if aq.auto_turn_count == 0:
                        message = "Proceed with the approved plan."
                    else:
                        message = "Continue."

                    # Stream the agent reply.
                    reply_parts: list[str] = []
                    try:
                        async for token in agent.stream(
                            message,
                            history=history,
                            session_id=session_id,
                            client_id=session_id,
                        ):
                            reply_parts.append(token)
                    except Exception:
                        logger.exception(
                            "Agent stream error in autonomous session %s",
                            session_id,
                        )
                        return

                    full_reply = "".join(reply_parts)

                    # Record the exchange so history accumulates.
                    self._store.record(session_id, owner_id, message, full_reply)

                    aq.auto_turn_count += 1

                    # Check for lifecycle markers in the reply.
                    new_state = self.check_reply_for_markers(session_id, full_reply)
                    if new_state is AutonomousState.completed:
                        await self._close_and_respawn(session_id)
                        return
                    if new_state is AutonomousState.awaiting_approval:
                        # Agent hit a blocker — wait for operator.
                        return
                    # Otherwise continue the loop.

        except asyncio.CancelledError:
            logger.debug("Auto-continue task cancelled for session %s", session_id)
        except Exception:
            logger.exception(
                "Auto-continue loop error in autonomous session %s",
                session_id,
            )

    # -- completion & respawn -----------------------------------------------

    async def _close_and_respawn(self, session_id: str) -> None:
        """Close the completed autonomous session and spawn a new one."""
        aq = self._sessions.get(session_id)
        if aq is None:
            return

        owner_id = aq.owner_id
        logger.info(
            "Autonomous session %s completed after %d auto-turns — "
            "closing and spawning next",
            session_id,
            aq.auto_turn_count,
        )

        # Close the completed session.
        self._store.close_session(owner_id, session_id)

        # Spawn a new autonomous session.
        new_sid = self._store.new_session_id()
        self._store.begin(new_sid)
        self.create_session(owner_id, session_id=new_sid)

        # Kick off the new session with an initial prompt so the agent
        # starts subject selection without waiting for a user message.
        try:
            async with self._run_serializer.for_owner(owner_id):
                agent = self._agent_factory()
                reply_parts: list[str] = []
                async for token in agent.stream(
                    "Begin a new autonomous session. Pick a subject and draft a plan.",
                    history=[],
                    session_id=new_sid,
                    client_id=new_sid,
                ):
                    reply_parts.append(token)
                full_reply = "".join(reply_parts)
                self._store.record(
                    new_sid,
                    owner_id,
                    "Begin a new autonomous session.",
                    full_reply,
                )
                self.check_reply_for_markers(new_sid, full_reply)
        except Exception:
            logger.exception(
                "Failed to kick off new autonomous session for owner %s",
                owner_id,
            )

    # -- resume on restart --------------------------------------------------

    async def resume_sessions(self) -> None:
        """Handle autonomous sessions on server restart.

        - Sessions in ``completed`` state: auto-close and respawn.
        - Sessions in ``executing`` state: resume auto-continue.
        """
        for session_id in list(self._sessions):
            aq = self._sessions.get(session_id)
            if aq is None:
                continue

            if aq.state is AutonomousState.completed:
                logger.info(
                    "Resuming: auto-closing completed autonomous session %s",
                    session_id,
                )
                try:
                    await self._close_and_respawn(session_id)
                except Exception:
                    logger.exception(
                        "Failed to close completed session %s on resume",
                        session_id,
                    )

            elif aq.state is AutonomousState.executing:
                logger.info(
                    "Resuming: restarting auto-continue for session %s",
                    session_id,
                )
                task = asyncio.create_task(self._auto_continue(session_id))
                self._auto_tasks.add(task)
                task.add_done_callback(self._auto_tasks.discard)

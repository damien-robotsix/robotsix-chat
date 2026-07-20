"""Autonomous session runner — manages the autonomous lifecycle loop."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from robotsix_chat.autonomous.models import AutonomousState
from robotsix_chat.chat.events import (
    agent_message_frame,
    autonomous_approval_required_frame,
    autonomous_respawned_frame,
    autonomous_state_changed_frame,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.events import EventBus
    from robotsix_chat.chat.server.routes.chat import ChatAgent
    from robotsix_chat.config.models import AutonomousSettings

logger = logging.getLogger(__name__)


# Prompt sent to the agent after approval to trigger plan execution.
_AUTO_CONTINUE_PROMPT = (
    "Your plan has been approved. Execute it now, step by step. "
    "Work autonomously with minimal back-and-forth."
)


class AutonomousRunner:
    """Manages autonomous session lifecycle and auto-cycling.

    Watches autonomous sessions for state transitions and handles the
    auto-respawn loop: when an autonomous session completes, close it
    and create a new autonomous session.  After approval, auto-continues
    the conversation so the agent can execute its plan without waiting
    for another user message.

    Turn counting per execution phase is tracked; when ``max_auto_turns``
    is exceeded the session transitions back to ``awaiting_approval``.
    """

    def __init__(
        self,
        conversation_store: ConversationStore,
        event_bus: EventBus,
        agent_factory: Callable[[], ChatAgent],
        settings: AutonomousSettings | None = None,
        run_serializer: Any = None,
    ) -> None:
        """Initialise with store, event bus, agent factory, and settings.

        *settings* provides ``max_auto_turns``, ``completion_marker``,
        and ``approval_marker``.  When ``None``, defaults are used.

        *run_serializer* (optional) is the per-owner
        :class:`~robotsix_chat.chat.server.routes.RunSerializer` shared
        with the chat endpoint — auto-continue turns acquire this lock
        so they never race a concurrent user message.
        """
        self._store = conversation_store
        self._event_bus = event_bus
        self._agent_factory = agent_factory
        self._settings = settings
        self._run_serializer = run_serializer

        # Per-session turn counter for the current execution phase.
        self._execution_turns: dict[str, int] = {}

        # Active background tasks (auto-continue, completion).
        self._active_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @property
    def _approval_marker(self) -> str:
        if self._settings is not None:
            return self._settings.approval_marker
        return "---AWAITING APPROVAL---"

    @property
    def _completion_marker(self) -> str:
        if self._settings is not None:
            return self._settings.completion_marker
        return "---AUTONOMOUS COMPLETE---"

    @property
    def _max_auto_turns(self) -> int:
        if self._settings is not None:
            return self._settings.max_auto_turns
        return 20

    # ------------------------------------------------------------------
    # Marker detection — called after every agent reply
    # ------------------------------------------------------------------

    def check_reply_for_markers(
        self,
        session_id: str,
        reply_text: str,
    ) -> AutonomousState | None:
        """Inspect *reply_text* for approval/completion markers.

        Transitions state when a marker is found.  Returns the new state
        when a transition occurred, ``None`` otherwise.
        """
        session = self._store.get_session(session_id)
        if session is None:
            return None
        if getattr(session, "kind", "chat") != "autonomous":
            return None

        current_state = getattr(session, "autonomous_state", None)

        # Completion marker — only valid in EXECUTING state.
        if self._completion_marker in reply_text:
            if current_state == AutonomousState.EXECUTING.value:
                self.transition_state(session_id, AutonomousState.COMPLETED)
                return AutonomousState.COMPLETED
            logger.debug(
                "Completion marker found in session %s (state=%s) — ignored",
                session_id,
                current_state,
            )
            return None

        # Approval marker — valid in SELECTING_SUBJECT or None (initial).
        if self._approval_marker in reply_text:
            if current_state in (
                AutonomousState.SELECTING_SUBJECT.value,
                None,
            ):
                # Store the plan text (everything before the marker, stripped).
                plan_end = reply_text.index(self._approval_marker)
                plan_text = reply_text[:plan_end].strip()
                if plan_text:
                    session.autonomous_plan = plan_text
                self.transition_state(session_id, AutonomousState.AWAITING_APPROVAL)
                return AutonomousState.AWAITING_APPROVAL
            logger.debug(
                "Approval marker found in session %s (state=%s) — ignored",
                session_id,
                current_state,
            )
            return None

        return None

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def transition_state(
        self, session_id: str, new_state: AutonomousState
    ) -> bool:
        """Transition an autonomous session to *new_state*.

        Publishes an SSE event on state change.  When transitioning to
        ``EXECUTING``, schedules an auto-continue turn so the agent starts
        executing without waiting for a user message.  When transitioning
        to ``COMPLETED``, schedules auto-close + respawn.

        Returns ``False`` if the session doesn't exist or isn't autonomous.
        """
        session = self._store.get_session(session_id)
        if session is None:
            return False
        if getattr(session, "kind", "chat") != "autonomous":
            return False

        old_state = getattr(session, "autonomous_state", None)
        session.autonomous_state = new_state.value

        # Publish state change event.
        self._event_bus.publish(
            session_id,
            autonomous_state_changed_frame(
                session_id, old_state, new_state.value
            ),
        )

        logger.info(
            "Autonomous session %s: %s → %s",
            session_id,
            old_state,
            new_state.value,
        )

        # Reset execution turn counter on entering EXECUTING.
        if new_state == AutonomousState.EXECUTING:
            self._execution_turns[session_id] = 0
            self._schedule_auto_continue(session_id)

        # Publish approval frame when entering AWAITING_APPROVAL.
        if new_state == AutonomousState.AWAITING_APPROVAL:
            plan_summary = getattr(session, "autonomous_plan", None) or ""
            self._event_bus.publish(
                session_id,
                autonomous_approval_required_frame(session_id, plan_summary),
            )

        # Auto-close + respawn on completion.
        if new_state == AutonomousState.COMPLETED:
            self._schedule_completion(session_id)

        return True

    # ------------------------------------------------------------------
    # Turn counting (call after each agent turn in EXECUTING state)
    # ------------------------------------------------------------------

    def count_execution_turn(self, session_id: str) -> bool:
        """Increment the execution turn counter; return True if limit exceeded.

        When ``max_auto_turns`` is exceeded the session transitions back
        to ``awaiting_approval``.  Returns ``True`` when the limit was hit.
        """
        session = self._store.get_session(session_id)
        if session is None:
            return False
        current_state = getattr(session, "autonomous_state", None)
        if current_state != AutonomousState.EXECUTING.value:
            return False

        count = self._execution_turns.get(session_id, 0) + 1
        self._execution_turns[session_id] = count

        if count > self._max_auto_turns:
            logger.info(
                "Autonomous session %s: max_auto_turns (%d) exceeded",
                session_id,
                self._max_auto_turns,
            )
            self.transition_state(session_id, AutonomousState.AWAITING_APPROVAL)
            return True

        return False

    # ------------------------------------------------------------------
    # Auto-continue (after approval)
    # ------------------------------------------------------------------

    def _schedule_auto_continue(self, session_id: str) -> None:
        """Schedule a background task to auto-continue the agent.

        Safe to call without a running event loop — the task is simply
        skipped (e.g. in tests that call transition_state synchronously).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "No running event loop; skipping auto-continue for %s",
                session_id,
            )
            return
        task = loop.create_task(self._auto_continue(session_id))
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    async def _auto_continue(self, session_id: str) -> None:
        """Run an auto-continue agent turn after approval.

        Sends a prompt telling the agent to execute the plan.  The reply
        is recorded to history and pushed as an ``agent_message`` frame.

        Acquires the per-owner :class:`RunSerializer` lock (when
        configured) so the auto-continue turn never races a concurrent
        user message.
        """
        session = self._store.get_session(session_id)
        if session is None:
            return

        try:
            agent = self._agent_factory()
            owner_id = self._store.owner_for_session(session_id) or session_id

            async def _run() -> None:
                history = self._store.history(session_id)

                parts: list[str] = []
                try:
                    async for token in agent.stream(
                        _AUTO_CONTINUE_PROMPT,
                        history=history or None,
                        session_id=session_id,
                        client_id=session_id,
                    ):
                        parts.append(token)
                except Exception:
                    logger.exception(
                        "Auto-continue agent call failed for session %s",
                        session_id,
                    )
                    return

                reply = "".join(parts)
                if reply:
                    self._store.record_for_session(
                        session_id,
                        _AUTO_CONTINUE_PROMPT,
                        reply,
                    )
                    self._event_bus.publish(
                        session_id,
                        agent_message_frame(reply, time.time()),
                    )

                    # Check markers in the reply.
                    self.check_reply_for_markers(session_id, reply)

                    # Count the execution turn.
                    self.count_execution_turn(session_id)

            if self._run_serializer is not None:
                async with self._run_serializer.for_owner(owner_id):
                    await _run()
            else:
                await _run()

        except Exception:
            logger.exception(
                "Auto-continue failed for session %s", session_id
            )

    # ------------------------------------------------------------------
    # Completion (auto-close + respawn)
    # ------------------------------------------------------------------

    def _schedule_completion(self, session_id: str) -> None:
        """Schedule auto-close and respawn for a completed session.

        Safe without a running event loop — the task is skipped.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "No running event loop; skipping completion for %s",
                session_id,
            )
            return
        task = loop.create_task(self._handle_completion(session_id))
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    async def _handle_completion(self, session_id: str) -> None:
        """Close the completed session and spawn a new autonomous session."""
        session = self._store.get_session(session_id)
        if session is None:
            return

        owner_id = self._store.owner_for_session(session_id)
        if owner_id is None:
            return

        # Close the completed session.
        self._store.close_session(owner_id, session_id)

        # Spawn a new autonomous session.
        new_session = self._store.create_session(owner_id, kind="autonomous")
        new_sid: str = str(new_session["session_id"])

        logger.info(
            "Autonomous cycle: closed %s, spawned %s",
            session_id,
            new_sid,
        )

        # Publish respawn event on the new session.
        self._event_bus.publish(
            new_sid,
            autonomous_respawned_frame(new_sid, session_id),
        )

    # ------------------------------------------------------------------
    # Resume after restart
    # ------------------------------------------------------------------

    def resume_autonomous_sessions(self) -> None:
        """Resume active autonomous sessions after server restart.

        Sessions in ``AWAITING_APPROVAL`` or ``EXECUTING`` state are
        re-published so the UI can reconnect.  ``COMPLETED`` sessions are
        auto-closed.  ``EXECUTING`` sessions are NOT auto-continued on
        resume — the operator should re-approve via the UI.
        """
        for session in self._store.iter_sessions():
            if getattr(session, "kind", "chat") != "autonomous":
                continue
            state = getattr(session, "autonomous_state", None)
            sid: str = str(session.session_id)
            if state == AutonomousState.COMPLETED.value:
                # Auto-close completed sessions on restart.
                self._schedule_completion(sid)
            elif state == AutonomousState.AWAITING_APPROVAL.value:
                # Re-publish the approval-required frame.
                plan_summary = getattr(session, "autonomous_plan", None) or ""
                self._event_bus.publish(
                    sid,
                    autonomous_approval_required_frame(sid, plan_summary),
                )
            elif state == AutonomousState.EXECUTING.value:
                # Re-publish state so UI can reconnect, but don't
                # auto-continue — the operator should re-approve.
                self._event_bus.publish(
                    sid,
                    autonomous_state_changed_frame(sid, state, state),
                )

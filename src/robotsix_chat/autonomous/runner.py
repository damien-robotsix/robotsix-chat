"""Autonomous session runner — state machine, marker detection, auto-cycling."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robotsix_chat.autonomous.models import AutonomousSession, AutonomousState
from robotsix_chat.chat.events import (
    EventSink,
    agent_message_frame,
    autonomous_state_frame,
    autonomous_token_frame,
)

if TYPE_CHECKING:
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
        event_sink: EventSink | None = None,
    ) -> None:
        """Create a runner with settings, store, agent factory, and serializer."""
        self._settings = settings
        self._store = conversation_store
        self._agent_factory = agent_factory
        self._run_serializer = run_serializer
        self._event_sink = event_sink
        self._persist_path = Path(settings.autonomous.persist_path)
        self._sessions: dict[str, AutonomousSession] = self._load_sessions()
        # Strong references to in-flight auto-continue tasks (see asyncio
        # docs warning on create_task and weak references).
        self._auto_tasks: set[asyncio.Task[None]] = set()

    # -- settings accessors -----------------------------------------------

    @property
    def max_auto_turns(self) -> int:
        """Maximum number of autonomous turns before requiring approval."""
        return self._settings.autonomous.max_auto_turns

    @property
    def session_color(self) -> str:
        """Colour string for autonomous session UI badge."""
        return self._settings.autonomous.session_color

    # -- persistence ------------------------------------------------------

    def _save_sessions(self) -> None:
        """Persist the in-memory session registry to disk."""
        try:
            data = {}
            for sid, aq in self._sessions.items():
                data[sid] = {
                    "session_id": aq.session_id,
                    "owner_id": aq.owner_id,
                    "state": aq.state.value,
                    "plan_text": aq.plan_text,
                    "auto_turn_count": aq.auto_turn_count,
                }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.exception("Failed to persist autonomous sessions")

    def _load_sessions(self) -> dict[str, AutonomousSession]:
        """Load persisted autonomous sessions from disk.

        Returns an empty dict when the persist file does not exist
        or cannot be parsed.
        """
        if not self._persist_path.exists():
            return {}
        try:
            raw = json.loads(self._persist_path.read_text())
        except Exception:
            logger.exception(
                "Failed to load autonomous sessions from %s",
                self._persist_path,
            )
            return {}
        sessions: dict[str, AutonomousSession] = {}
        for sid, entry in raw.items():
            try:
                sessions[sid] = AutonomousSession(
                    session_id=entry["session_id"],
                    owner_id=entry["owner_id"],
                    state=AutonomousState(entry["state"]),
                    plan_text=entry.get("plan_text", ""),
                    auto_turn_count=entry.get("auto_turn_count", 0),
                )
            except Exception:
                logger.exception("Skipping unparsable autonomous session %s", sid)
        logger.info(
            "Loaded %d autonomous sessions from %s",
            len(sessions),
            self._persist_path,
        )
        return sessions

    # -- session registry ---------------------------------------------------

    def _schedule_background(
        self, coro_factory: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        """Schedule a background task; no-op when no loop is running.

        Accepts a zero-argument factory that returns a coroutine so the
        coroutine is only created when a running event loop exists.
        Keeps a strong reference in ``_auto_tasks`` and cleans up on
        completion.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(coro_factory())
        self._auto_tasks.add(task)
        task.add_done_callback(self._auto_tasks.discard)

    def _publish_state(self, session_id: str) -> None:
        """Push an ``autonomous_state`` frame to connected browsers, if any."""
        if self._event_sink is None:
            return
        aq = self._sessions.get(session_id)
        if aq is None:
            return
        self._event_sink.publish(
            session_id,
            autonomous_state_frame(
                session_id=session_id,
                state=aq.state.value,
                plan_text=aq.plan_text,
                auto_turn_count=aq.auto_turn_count,
                max_auto_turns=self._settings.autonomous.max_auto_turns,
                session_color=self._settings.autonomous.session_color,
            ),
        )

    def create_session(
        self,
        owner_id: str,
        session_id: str | None = None,
        *,
        schedule_kickoff: bool = True,
    ) -> AutonomousSession:
        """Register a new autonomous session, creating a store session if needed.

        When *schedule_kickoff* is ``True`` (the default), an initial agent
        turn is scheduled as a background task so the session immediately
        begins subject selection.  Pass ``False`` when the caller will handle
        the kickoff itself (e.g. :meth:`_close_and_respawn`).
        """
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
        self._save_sessions()

        if schedule_kickoff:
            # Schedule the initial agent turn so the session immediately
            # begins subject selection + plan drafting (Fix 1: kickoff).
            self._schedule_background(
                lambda: self._kickoff_initial_turn(session_id, owner_id)
            )

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
            self._save_sessions()
            self._publish_state(session_id)
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
            self._save_sessions()
            self._publish_state(session_id)
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
        self._schedule_background(lambda: self._auto_continue(session_id))

        self._save_sessions()
        self._publish_state(session_id)
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
        self._save_sessions()
        self._publish_state(session_id)
        logger.info(
            "Autonomous session %s rejected — reset to subject selection",
            session_id,
        )

        # Schedule a fresh initial turn so the session is not left inert
        # in selecting_subject (mirrors create_session).
        self._schedule_background(
            lambda sid=session_id, oid=aq.owner_id: self._kickoff_initial_turn(  # type: ignore[misc]
                sid, oid
            )
        )

        return True, ""

    # -- initial turn kickoff ------------------------------------------------

    async def _kickoff_initial_turn(self, session_id: str, owner_id: str) -> None:
        """Run the first agent turn for a new autonomous session.

        Streams the agent with the autonomous instruction supplement so
        it performs subject selection + plan drafting and (when the model
        cooperates) emits the approval marker.  After the reply,
        :meth:`check_reply_for_markers` transitions the session to
        ``awaiting_approval`` (or ``completed``).
        """
        try:
            async with self._run_serializer.for_owner(owner_id):
                agent = await asyncio.to_thread(self._agent_factory)
                initial_task = self._settings.autonomous.initial_task
                if initial_task:
                    prompt = (
                        f"Begin a new autonomous session. Initial task: {initial_task}"
                    )
                else:
                    prompt = (
                        "Begin a new autonomous session. "
                        "Pick a subject and draft a plan."
                    )
                reply_parts: list[str] = []
                async for token in agent.stream(
                    prompt,
                    history=[],
                    session_id=session_id,
                    client_id=session_id,
                ):
                    reply_parts.append(token)
                    if self._event_sink is not None:
                        self._event_sink.publish(
                            session_id,
                            autonomous_token_frame(token),
                        )
                full_reply = "".join(reply_parts)
                self._store.record(
                    session_id,
                    owner_id,
                    prompt,
                    full_reply,
                )
                if self._event_sink is not None:
                    self._event_sink.publish(
                        session_id,
                        agent_message_frame(full_reply, time.time()),
                    )
                self.check_reply_for_markers(session_id, full_reply)
        except asyncio.CancelledError:
            logger.debug("Initial-turn task cancelled for session %s", session_id)
        except Exception:
            logger.exception(
                "Initial-turn error in autonomous session %s",
                session_id,
            )

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
                    self._save_sessions()
                    self._publish_state(session_id)
                    return

                # Acquire the per-owner run lock.
                should_respawn = False
                async with self._run_serializer.for_owner(owner_id):
                    agent = await asyncio.to_thread(self._agent_factory)
                    history = self._store.agent_history(session_id)

                    # First turn after approval: explicit proceed message.
                    if aq.auto_turn_count == 0:
                        message = (
                            "OPERATOR APPROVAL RECEIVED. Your plan has been "
                            "approved. Begin executing the first step of your "
                            "plan immediately — use your tools to take the "
                            "action now. Do not describe what you will do; "
                            "actually perform it. Do not request re-approval "
                            "unless you encounter a genuine blocker that you "
                            "cannot resolve on your own."
                        )
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
                            if self._event_sink is not None:
                                self._event_sink.publish(
                                    session_id,
                                    autonomous_token_frame(token),
                                )
                    except Exception:
                        logger.exception(
                            "Agent stream error in autonomous session %s",
                            session_id,
                        )
                        return

                    full_reply = "".join(reply_parts)

                    # Record the exchange so history accumulates.
                    self._store.record(session_id, owner_id, message, full_reply)

                    if self._event_sink is not None:
                        self._event_sink.publish(
                            session_id,
                            agent_message_frame(full_reply, time.time()),
                        )

                    aq.auto_turn_count += 1
                    self._save_sessions()

                    # Check for lifecycle markers in the reply.
                    new_state = self.check_reply_for_markers(session_id, full_reply)
                    if new_state is AutonomousState.completed:
                        should_respawn = True
                    elif new_state is AutonomousState.awaiting_approval:
                        # Agent hit a blocker — wait for operator.
                        return
                    # Otherwise continue the loop.

                # Release the per-owner lock *before* respawning to avoid
                # deadlock: _close_and_respawn → _kickoff_initial_turn tries
                # to acquire the same non-reentrant asyncio.Lock.
                if should_respawn:
                    await self._close_and_respawn(session_id)
                    return

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

        # Spawn a new autonomous session (no auto-kickoff — we handle it
        # inline below).
        new_sid = self._store.new_session_id()
        self._store.begin(new_sid)
        self.create_session(owner_id, session_id=new_sid, schedule_kickoff=False)

        # Kick off the new session with an initial prompt so the agent
        # starts subject selection without waiting for a user message.
        await self._kickoff_initial_turn(new_sid, owner_id)

    # -- resume on restart --------------------------------------------------

    async def resume_sessions(self) -> None:
        """Handle autonomous sessions on server restart.

        - Sessions in ``completed`` state: auto-close and respawn.
        - Sessions in ``executing`` state: resume auto-continue.
        - Sessions in ``selecting_subject`` state: re-kickoff the initial
          turn (the previous kickoff was lost on restart).
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
                self._schedule_background(
                    lambda sid=session_id: self._auto_continue(sid)  # type: ignore[misc]
                )

            elif aq.state is AutonomousState.selecting_subject:
                logger.info(
                    "Resuming: re-kickoff initial turn for session %s",
                    session_id,
                )
                self._schedule_background(
                    lambda sid=session_id, oid=aq.owner_id: self._kickoff_initial_turn(  # type: ignore[misc]
                        sid, oid
                    )
                )

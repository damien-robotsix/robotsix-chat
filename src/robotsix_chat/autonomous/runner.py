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
        subsession_registry: Any = None,
    ) -> None:
        """Create a runner with settings, store, agent factory, and serializer."""
        self._settings = settings
        self._store = conversation_store
        self._agent_factory = agent_factory
        self._run_serializer = run_serializer
        self._event_sink = event_sink
        self._subsession_registry = subsession_registry
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

        Enforces the single-session invariant: if *owner_id* already has an
        open autonomous session (any non-terminal state), the existing session
        is returned unchanged and no new session is created.
        """
        # Single-session invariant: at most one open autonomous session per owner.
        for existing in self._sessions.values():
            if (
                existing.owner_id == owner_id
                and existing.state is not AutonomousState.completed
            ):
                logger.warning(
                    "Cannot create new autonomous session for owner %s: "
                    "session %s is already open (state=%s)",
                    owner_id,
                    existing.session_id,
                    existing.state.value,
                )
                return existing

        if session_id is None:
            session_id = self._store.new_session_id()
        # Ensure the store has this session AND that it is registered under
        # the owner so it appears in ``list_sessions`` and is persisted.  The
        # runner records turns out-of-band, so without an explicit
        # registration the session would stay orphaned from the owner (only
        # in the store's global map, never in the owner's session set) and be
        # dropped from ``conversations.json`` on restart.
        self._store.begin(session_id)
        self._store.register_session(owner_id, session_id, title="Autonomous chat")
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

            # Auto-approve: skip the human gate and begin executing straight
            # away using the exact same transition an operator approval takes.
            if self._settings.autonomous.auto_approve:
                logger.info(
                    "Autonomous session %s auto-approved (auto_approve=true) — "
                    "starting execution (plan %d chars)",
                    session_id,
                    len(aq.plan_text),
                )
                self._begin_execution(session_id)
                return AutonomousState.executing

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

    def _begin_execution(self, session_id: str) -> None:
        """Transition *session_id* into execution and kick off auto-continue.

        Shared by operator approval (:meth:`approve`) and auto-approval
        (:meth:`check_reply_for_markers` when ``autonomous.auto_approve`` is
        ``True``) so both paths behave identically: reset the turn counter,
        flip to ``executing``, schedule the background auto-continue loop,
        persist, and publish the new state.  No-op when the session is gone.
        """
        aq = self._sessions.get(session_id)
        if aq is None:
            return

        aq.state = AutonomousState.executing
        aq.auto_turn_count = 0

        # Schedule auto-continue as a background task.
        self._schedule_background(lambda: self._auto_continue(session_id))

        self._save_sessions()
        self._publish_state(session_id)

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

        self._begin_execution(session_id)
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

    async def _kickoff_initial_turn(
        self, session_id: str, owner_id: str, *, is_restart: bool = False
    ) -> None:
        """Run the first agent turn for a new autonomous session.

        Streams the agent with the autonomous instruction supplement so
        it performs subject selection + plan drafting and (when the model
        cooperates) emits the approval marker.  After the reply,
        :meth:`check_reply_for_markers` transitions the session to
        ``awaiting_approval`` (or ``completed``).

        When *is_restart* is ``True``, the prompt is adjusted to inform
        the agent that the system was restarted and the session is being
        resumed rather than freshly created.
        """
        try:
            async with self._run_serializer.for_owner(owner_id):
                agent = await asyncio.to_thread(self._agent_factory)
                restart_notice = ""
                if is_restart:
                    restart_notice = (
                        "SYSTEM RESTARTED — you are resuming an existing "
                        "autonomous session. "
                    )
                initial_task = self._settings.autonomous.initial_task
                if initial_task:
                    prompt = (
                        f"{restart_notice}"
                        f"Begin a new autonomous session. Initial task: {initial_task}"
                    )
                else:
                    prompt = (
                        f"{restart_notice}"
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

    def _has_pending_subsessions(self, session_id: str) -> bool:
        """Return True when the session has active non-periodic subsessions.

        Periodic subsessions run indefinitely by design — they are not
        "pending" work that the runner should wait for.  Only task and
        user_chat subsessions (which have finite lifetimes) block the
        auto-continue loop.
        """
        reg = self._subsession_registry
        if reg is None:
            return False
        try:
            subs = reg.list_for_owner(session_id)
        except Exception:
            return False
        return any(
            getattr(s, "is_active", False)
            and getattr(s, "kind", None) not in (None, "periodic")
            for s in subs
        )

    async def _wait_before_continue(self, session_id: str) -> None:
        """Pace the continue loop and pause while pending subsessions exist.

        Always waits at least ``continue_interval_seconds`` (throttle), then
        keeps waiting while the session has active subsessions, bounded by
        ``pending_subsession_wait_timeout`` so a stuck subsession cannot hang
        the session forever. Runs OUTSIDE the per-owner run lock and is
        cancellable (``asyncio.sleep`` propagates ``CancelledError``).
        """
        interval = max(0.0, self._settings.autonomous.continue_interval_seconds)
        timeout = max(0.0, self._settings.autonomous.pending_subsession_wait_timeout)
        step = interval if interval > 0 else 5.0
        if interval > 0:
            await asyncio.sleep(interval)
        waited = interval
        while self._has_pending_subsessions(session_id) and waited < timeout:
            await asyncio.sleep(step)
            waited += step

    async def _auto_continue(
        self, session_id: str, *, is_restart: bool = False
    ) -> None:
        """Drive execution turns until completion, re-approval, or turn cap.

        When *is_restart* is ``True``, the agent is informed that the
        system was restarted and the session is being resumed.
        """
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

                # Throttle + gate: pace continues and pause while the session
                # has pending subsessions/periodic work outstanding.
                if aq.auto_turn_count > 0:
                    await self._wait_before_continue(session_id)
                    aq = self._sessions.get(session_id)
                    if aq is None or aq.state is not AutonomousState.executing:
                        return

                # Acquire the per-owner run lock.
                should_respawn = False
                async with self._run_serializer.for_owner(owner_id):
                    agent = await asyncio.to_thread(self._agent_factory)
                    history = self._store.agent_history(session_id)

                    # First turn after approval: explicit proceed message.
                    if aq.auto_turn_count == 0:
                        restart_prefix = (
                            "SYSTEM RESTARTED — resuming your autonomous session. "
                            if is_restart
                            else ""
                        )
                        message = (
                            f"{restart_prefix}"
                            "OPERATOR APPROVAL RECEIVED. Your plan has been "
                            "approved. Begin executing the first step of your "
                            "plan immediately — use your tools to take the "
                            "action now. Do not describe what you will do; "
                            "actually perform it. Do not request re-approval "
                            "unless you encounter a genuine blocker that you "
                            "cannot resolve on your own."
                        )
                    else:
                        if is_restart:
                            message = (
                                "SYSTEM RESTARTED — resuming your autonomous "
                                "execution session from where it left off. "
                                "Continue."
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

                # Schedule respawn as a background task so the auto-continue
                # loop can return immediately — the respawn kickoff is also
                # non-blocking (see _close_and_respawn docstring).
                if should_respawn:
                    self._schedule_background(
                        lambda sid=session_id: self._close_and_respawn(sid)  # type: ignore[misc]
                    )
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
        """Close the completed autonomous session and spawn a new one.

        This method is *non-blocking*: the respawn kickoff is scheduled as a
        background task and this coroutine returns immediately.  Callers must
        never ``await`` this in startup/lifespan paths — schedule it via
        :meth:`_schedule_background` instead.

        Enforces the single-session invariant: at most one open autonomous
        session per owner at any time.
        """
        try:
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

            # Close the completed session and remove it from the in-memory
            # registry so a concurrent trigger sees ``None`` and exits early
            # (idempotency guard — prevents double-spawn).
            self._store.close_session(owner_id, session_id)
            del self._sessions[session_id]

            # Single-session invariant: never spawn a second open session for
            # this owner.  (The just-closed session is already gone, so any
            # match here is a genuine duplicate.)
            for existing in self._sessions.values():
                if (
                    existing.owner_id == owner_id
                    and existing.state is not AutonomousState.completed
                ):
                    logger.warning(
                        "Cannot spawn new autonomous session for owner %s: "
                        "session %s is still open (state=%s)",
                        owner_id,
                        existing.session_id,
                        existing.state.value,
                    )
                    self._save_sessions()
                    return

            # Spawn a new autonomous session.  ``schedule_kickoff=True`` kicks
            # off the initial turn as a background task, so this coroutine
            # returns immediately — the caller (or the lifespan) is never
            # blocked waiting for the agent's first reply.
            new_sid = self._store.new_session_id()
            self._store.begin(new_sid)
            self.create_session(owner_id, session_id=new_sid, schedule_kickoff=True)
        except Exception:
            logger.exception(
                "Error in _close_and_respawn for session %s",
                session_id,
            )

    # -- resume on restart --------------------------------------------------

    async def resume_sessions(self) -> None:
        """Handle autonomous sessions on server restart.

        - Sessions in ``completed`` state: auto-close and respawn.
        - Sessions in ``executing`` state: resume auto-continue.
        - Sessions in ``selecting_subject`` state: re-kickoff the initial
          turn (the previous kickoff was lost on restart).
        - Sessions in ``awaiting_approval`` state: when
          ``autonomous.auto_approve`` is ``True``, auto-approve and begin
          executing (clears sessions that got stuck before the gate could be
          reached in the UI); otherwise leave them for an operator.
        - When no sessions exist at all (e.g. a fresh or wiped store),
          auto-start exactly one bootstrap session so autonomous mode is
          not permanently idle.
        """
        for session_id in list(self._sessions):
            aq = self._sessions.get(session_id)
            if aq is None:
                continue

            # Reconcile the conversation store.  The AutonomousRunner's state
            # (autonomous_sessions.json) persists independently of the
            # conversation store (conversations.json).  If the conversation
            # entry was never persisted — e.g. the session was created and its
            # turns recorded before the owner existed, so it was never linked
            # to the owner and thus never written to disk — re-register it so
            # the session reappears in ``list_sessions`` for its owner.  This
            # repairs already-orphaned sessions on the next restart.
            self._store.register_session(
                aq.owner_id, session_id, title="Autonomous chat"
            )

            if aq.state is AutonomousState.completed:
                logger.info(
                    "Resuming: auto-closing completed autonomous session %s",
                    session_id,
                )
                self._schedule_background(
                    lambda sid=session_id: self._close_and_respawn(sid)  # type: ignore[misc]
                )

            elif aq.state is AutonomousState.executing:
                logger.info(
                    "Resuming: restarting auto-continue for session %s",
                    session_id,
                )
                self._schedule_background(
                    lambda sid=session_id: self._auto_continue(sid, is_restart=True)  # type: ignore[misc]
                )

            elif aq.state is AutonomousState.selecting_subject:
                logger.info(
                    "Resuming: re-kickoff initial turn for session %s",
                    session_id,
                )
                self._schedule_background(
                    lambda sid=session_id, oid=aq.owner_id: self._kickoff_initial_turn(  # type: ignore[misc]
                        sid, oid, is_restart=True
                    )
                )

            elif aq.state is AutonomousState.awaiting_approval:
                if self._settings.autonomous.auto_approve:
                    logger.info(
                        "Resuming: auto-approving awaiting-approval session %s "
                        "(auto_approve=true) — starting execution",
                        session_id,
                    )
                    self._begin_execution(session_id)
                else:
                    logger.info(
                        "Resuming: leaving session %s in awaiting_approval "
                        "(awaiting operator approval)",
                        session_id,
                    )

        # Bootstrap: when the store is empty (fresh deploy or wiped data),
        # auto-start one session so autonomous mode isn't permanently idle.
        if not self._sessions:
            logger.info(
                "No autonomous sessions found — bootstrapping one "
                "for owner 'autonomous'"
            )
            self.create_session("autonomous", schedule_kickoff=True)

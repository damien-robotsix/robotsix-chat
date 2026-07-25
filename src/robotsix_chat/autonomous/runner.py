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


def _rejected_subjects_note(aq: AutonomousSession | None) -> str:
    """Return a prompt suffix listing previously rejected subjects, or ""."""
    if aq is None or not aq.rejected_subjects:
        return ""
    items = "\n".join(f"  - {s!r}" for s in aq.rejected_subjects)
    return (
        "\n\nPREVIOUSLY REJECTED SUBJECTS — do NOT propose any of these "
        f"subjects again:\n{items}"
    )


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
                    "completion_suppressed": aq.completion_suppressed,
                    "rejected_subjects": aq.rejected_subjects or [],
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
                    completion_suppressed=entry.get("completion_suppressed", False),
                    rejected_subjects=entry.get("rejected_subjects", []),
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
        the kickoff itself (e.g. when the caller will schedule it manually).

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
            state=AutonomousState.planning,
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

        proposal_marker = self._settings.autonomous.proposal_marker
        completion_marker = self._settings.autonomous.completion_marker

        # Check completion first (it terminates the execution loop).
        if completion_marker in reply_text:
            # Gate: suppress completion while the session owns any active
            # subsession (including periodic monitors).  Premature completion
            # closes the session and locks the agent out of spawning tracking
            # monitors, leaving newly-filed tickets untracked.
            if self._has_active_subsessions(session_id):
                logger.warning(
                    "Autonomous session %s attempted completion while "
                    "active subsessions are still running — suppressing",
                    session_id,
                )
                aq.completion_suppressed = True
                self._save_sessions()
                return None

            aq.state = AutonomousState.completed
            logger.info(
                "Autonomous session %s completed",
                session_id,
            )
            self._save_sessions()
            self._publish_state(session_id)
            return AutonomousState.completed

        # Check proposal marker.
        if proposal_marker in reply_text:
            # Plan text is everything before the marker.
            idx = reply_text.index(proposal_marker)
            aq.plan_text = reply_text[:idx].strip()

            aq.state = AutonomousState.proposal
            logger.info(
                "Autonomous session %s proposal ready (plan %d chars)",
                session_id,
                len(aq.plan_text),
            )
            self._save_sessions()
            self._publish_state(session_id)
            return AutonomousState.proposal

        return None

    # -- proposal → execution transition -----------------------------------

    def _begin_execution(self, session_id: str) -> None:
        """Transition *session_id* into execution and kick off auto-continue.

        Shared by :meth:`on_user_message` (when the operator comments on a
        proposal) and :meth:`_auto_continue` (when the agent re-proposes
        after hitting a blocker).  Resets the turn counter, flips to
        ``executing``, schedules the background auto-continue loop,
        persists, and publishes the new state.  No-op when the session
        is gone.
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

    def on_user_message(self, session_id: str) -> None:
        """Handle a user message to an autonomous session.

        When the session is in ``proposal`` state the operator's message
        acts as implicit approval — the session transitions to executing
        and the auto-continue loop begins.  No-op for sessions in any
        other state or unknown sessions.
        """
        aq = self._sessions.get(session_id)
        if aq is None:
            return
        if aq.state is AutonomousState.proposal:
            logger.info(
                "Autonomous session %s — operator message received, "
                "transitioning from proposal to executing",
                session_id,
            )
            self._begin_execution(session_id)

    # -- initial turn kickoff ------------------------------------------------

    async def _kickoff_initial_turn(
        self, session_id: str, owner_id: str, *, is_restart: bool = False
    ) -> None:
        """Run the first agent turn for a new autonomous session.

        Streams the agent with the autonomous instruction supplement so
        it performs subject selection + plan drafting and (when the model
        cooperates) emits the proposal marker.  After the reply,
        :meth:`check_reply_for_markers` transitions the session to
        ``proposal`` (or ``completed``).

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
                rejected_note = _rejected_subjects_note(self._sessions.get(session_id))
                if initial_task:
                    prompt = (
                        f"{restart_notice}"
                        f"Begin a new autonomous session. Initial task: {initial_task}"
                        f"{rejected_note}"
                    )
                else:
                    prompt = (
                        f"{restart_notice}"
                        "Begin a new autonomous session. "
                        "Pick a subject and draft a plan."
                        f"{rejected_note}"
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

    def _has_active_subsessions(self, session_id: str) -> bool:
        """Return True when the session has *any* active subsession.

        Unlike :meth:`_has_pending_subsessions`, this includes periodic
        monitors — used as a pre-completion gate so the session is never
        marked completed while owned background work is still running.
        """
        reg = self._subsession_registry
        if reg is None:
            return False
        try:
            subs = reg.list_for_owner(session_id)
        except Exception:
            return False
        return any(getattr(s, "is_active", False) for s in subs)

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
                        "reverting to proposal",
                        session_id,
                        max_turns,
                    )
                    aq.state = AutonomousState.proposal
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
                async with self._run_serializer.for_owner(owner_id):
                    agent = await asyncio.to_thread(self._agent_factory)
                    history = self._store.agent_history(session_id)

                    # First turn after proposal approval: the operator's
                    # message is already in history, so just prompt
                    # the agent to continue executing its plan.
                    if aq.auto_turn_count == 0:
                        restart_prefix = (
                            "SYSTEM RESTARTED — resuming your autonomous session. "
                            if is_restart
                            else ""
                        )
                        message = (
                            f"{restart_prefix}"
                            "The operator has seen your plan and is ready for "
                            "you to begin. Execute the first step of your plan "
                            "immediately — use your tools to take the action "
                            "now. Do not describe what you will do; actually "
                            "perform it."
                        )
                    else:
                        if aq.completion_suppressed:
                            aq.completion_suppressed = False
                            restart_prefix = (
                                "SYSTEM RESTARTED — resuming your autonomous "
                                "execution session from where it left off. "
                                if is_restart
                                else ""
                            )
                            message = (
                                f"{restart_prefix}"
                                "Continue. (Your previous completion marker "
                                "was ignored because active monitoring "
                                "subsessions are still running.  Use "
                                "list_subsessions to check their status, "
                                "and only emit the completion marker when "
                                "all subsessions have finished.)"
                            )
                        elif is_restart:
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
                        # Session completed — stay open, operator will close.
                        return
                    if new_state is AutonomousState.proposal:
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

    # -- resume on restart --------------------------------------------------

    async def resume_sessions(self) -> None:
        """Handle autonomous sessions on server restart.

        - Sessions in ``completed`` state: left as-is (operator closes).
        - Sessions in ``executing`` state: resume auto-continue.
        - Sessions in ``planning`` state: re-kickoff the initial
          turn (the previous kickoff was lost on restart).
        - Sessions in ``proposal`` state: left for operator review.
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
                    "Resuming: completed autonomous session %s — leaving as-is",
                    session_id,
                )

            elif aq.state is AutonomousState.executing:
                logger.info(
                    "Resuming: restarting auto-continue for session %s",
                    session_id,
                )
                self._schedule_background(
                    lambda sid=session_id: self._auto_continue(sid, is_restart=True)  # type: ignore[misc]
                )

            elif aq.state is AutonomousState.planning:
                logger.info(
                    "Resuming: re-kickoff initial turn for session %s",
                    session_id,
                )
                self._schedule_background(
                    lambda sid=session_id, oid=aq.owner_id: self._kickoff_initial_turn(  # type: ignore[misc]
                        sid, oid, is_restart=True
                    )
                )

            elif aq.state is AutonomousState.proposal:
                logger.info(
                    "Resuming: leaving session %s in proposal "
                    "(awaiting operator review)",
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

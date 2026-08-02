"""Autonomous session runner — state machine, marker detection, auto-cycling."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from robotsix_chat.autonomous.models import AutonomousSession, AutonomousState
from robotsix_chat.autonomous.refinement import RefinementStore
from robotsix_chat.chat.events import (
    EventSink,
    agent_message_frame,
    autonomous_state_frame,
    autonomous_token_frame,
)
from robotsix_chat.subsessions.worker import _is_no_change

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.server.routes import ChatAgent, RunSerializer
    from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)

# Fixed pseudo-owner id under which auto-bootstrapped autonomous sessions live.
# Autonomous sessions are not owned by any browser client, so they are
# registered under this stable owner id and the UI surfaces them by fetching
# ``GET /sessions?owner_id=autonomous``.  MUST match ``AUTONOMOUS_OWNER`` in
# ui/static/chat.js.
BOOTSTRAP_OWNER = "autonomous"

# Name of the default session definition — synthesized when the config's
# ``sessions`` list is empty so the pre-existing single-session behavior is
# preserved out of the box.
DEFAULT_SESSION_NAME = "default"

# Prefix for owner IDs derived from named session definitions.
_OWNER_ID_PREFIX = "autonomous:"


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
        refinement_store: RefinementStore | None = None,
    ) -> None:
        """Create a runner with settings, store, agent factory, and serializer."""
        self._settings = settings
        self._store = conversation_store
        self._agent_factory = agent_factory
        self._run_serializer = run_serializer
        self._event_sink = event_sink
        self._subsession_registry = subsession_registry
        self._refinement_store = refinement_store
        self._persist_path = Path(settings.autonomous.persist_path)
        self._sessions: dict[str, AutonomousSession] = self._load_sessions()
        # Strong references to in-flight auto-continue tasks (see asyncio
        # docs warning on create_task and weak references).
        self._auto_tasks: set[asyncio.Task[None]] = set()
        # Resolve session definitions: use the configured list, or synthesize
        # a default preset when none are configured (backward compat).
        self._definitions = self._resolve_definitions()

    # -- settings accessors -----------------------------------------------

    @property
    def max_auto_turns(self) -> int:
        """Maximum number of autonomous turns before requiring approval."""
        return self._settings.autonomous.max_auto_turns

    @property
    def session_color(self) -> str:
        """Colour string for autonomous session UI badge."""
        return self._settings.autonomous.session_color

    @property
    def bootstrap_owner(self) -> str:
        """Fixed pseudo-owner id under which autonomous sessions are surfaced."""
        return BOOTSTRAP_OWNER

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
                    "consecutive_no_change": aq.consecutive_no_change,
                    "completion_suppressed": aq.completion_suppressed,
                    "rejected_subjects": aq.rejected_subjects or [],
                    "recent_user_messages": aq.recent_user_messages or [],
                    "definition_name": aq.definition_name,
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
        try:
            if not self._persist_path.exists():
                return {}
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
                    consecutive_no_change=entry.get("consecutive_no_change", 0),
                    completion_suppressed=entry.get("completion_suppressed", False),
                    rejected_subjects=entry.get("rejected_subjects", []),
                    recent_user_messages=entry.get("recent_user_messages", []),
                    definition_name=entry.get("definition_name", ""),
                )
            except Exception:
                logger.exception("Skipping unparsable autonomous session %s", sid)
        logger.info(
            "Loaded %d autonomous sessions from %s",
            len(sessions),
            self._persist_path,
        )
        return sessions

    # -- session definitions (config or synthesized) ------------------------

    @staticmethod
    def _synthesize_default_definition(
        continue_interval_seconds: float,
    ) -> dict[str, Any]:
        """Return a single-entry dict with the legacy default definition.

        This is the backward-compat path: when the config's ``sessions``
        list is empty, the runner creates one default session definition
        that mirrors the pre-existing single-session behavior exactly.
        """
        definition: dict[str, Any] = {
            "name": DEFAULT_SESSION_NAME,
            "prompt": "",
            "trigger_type": "periodic",
            "trigger_interval_seconds": continue_interval_seconds,
            "enabled": True,
            "self_refine": False,
            "self_refine_require_approval": False,
        }
        return {DEFAULT_SESSION_NAME: definition}

    def _resolve_definitions(self) -> dict[str, Any]:
        """Build the runtime definition registry from config or defaults.

        Returns a ``dict[name, definition_dict]``.  When the configured
        ``sessions`` list is empty, a single default preset is synthesized
        so the pre-existing single-session behavior is preserved out of
        the box.  Only enabled definitions are included.
        """
        configured = self._settings.autonomous.sessions
        if configured and isinstance(configured, list):
            return {
                d.name: {
                    "name": d.name,
                    "prompt": d.prompt,
                    "trigger_type": d.trigger_type.value
                    if hasattr(d.trigger_type, "value")
                    else d.trigger_type,
                    "trigger_interval_seconds": d.trigger_interval_seconds,
                    "enabled": d.enabled,
                    "self_refine": d.self_refine,
                    "self_refine_require_approval": d.self_refine_require_approval,
                }
                for d in configured
                if d.enabled
            }
        return self._synthesize_default_definition(
            self._settings.autonomous.continue_interval_seconds
        )

    def _owner_id_for_definition(self, name: str) -> str:
        """Return the pseudo-owner ID for a session definition *name*."""
        if name == DEFAULT_SESSION_NAME:
            return BOOTSTRAP_OWNER
        return f"{_OWNER_ID_PREFIX}{name}"

    def _definition_for_owner(self, owner_id: str) -> dict[str, Any] | None:
        """Return the definition dict for *owner_id*, or ``None``."""
        if owner_id == BOOTSTRAP_OWNER:
            return self._definitions.get(DEFAULT_SESSION_NAME)
        if owner_id.startswith(_OWNER_ID_PREFIX):
            name = owner_id[len(_OWNER_ID_PREFIX) :]
            return self._definitions.get(name)
        return None

    @property
    def definition_names(self) -> list[str]:
        """All active definition names (enabled at startup)."""
        return sorted(self._definitions)

    # -- public accessors (used by routes) ---------------------------------

    def get_definition(self, name: str) -> dict[str, Any] | None:
        """Return the definition dict for *name*, or ``None``."""
        return self._definitions.get(name)

    def owner_id_for_definition(self, name: str) -> str:
        """Return the pseudo-owner ID for a session definition *name*."""
        return self._owner_id_for_definition(name)

    def active_session_id_for_definition(self, name: str) -> str | None:
        """Return the ``session_id`` of the active session for *name*, or ``None``.

        An active session is in a non-terminal state (planning, proposal,
        or executing).  Completed sessions are ignored.
        """
        owner_id = self._owner_id_for_definition(name)
        for aq in self._sessions.values():
            if aq.owner_id == owner_id and aq.state is not AutonomousState.completed:
                return aq.session_id
        return None

    def is_autonomous_owner(self, owner_id: str) -> bool:
        """Return ``True`` when *owner_id* belongs to any session definition."""
        if owner_id == BOOTSTRAP_OWNER and DEFAULT_SESSION_NAME in self._definitions:
            return True
        if owner_id.startswith(_OWNER_ID_PREFIX):
            name = owner_id[len(_OWNER_ID_PREFIX) :]
            return name in self._definitions
        return False

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
        definition_name: str = "",
    ) -> AutonomousSession:
        """Register a new autonomous session, creating a store session if needed.

        When *schedule_kickoff* is ``True`` (the default), an initial agent
        turn is scheduled as a background task so the session immediately
        begins subject selection.  Pass ``False`` when the caller will handle
        the kickoff itself (e.g. when the caller will schedule it manually).

        *definition_name* names the :class:`AutonomousSessionDefinition`
        that spawned this session.  When empty, derived from *owner_id*.

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
                # Idempotent re-creation of the SAME session: return it
                # without a warning (the caller is re-registering an
                # already-tracked session, not trying to open a second one).
                if session_id is not None and existing.session_id == session_id:
                    return existing
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
        # Resolve definition name from owner_id when not explicitly provided.
        resolved_name = definition_name
        if not resolved_name:
            defn = self._definition_for_owner(owner_id)
            if defn is not None:
                resolved_name = defn["name"]
        aq = AutonomousSession(
            session_id=session_id,
            owner_id=owner_id,
            state=AutonomousState.planning,
            definition_name=resolved_name,
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

    def forget_session(self, session_id: str) -> bool:
        """Drop *session_id* from the runner registry and persist.

        Used when the operator deletes or closes an autonomous session so the
        runner stops tracking stale state.  Without this, a
        deleted/closed session lingers in the registry — blocking the
        single-session invariant and (for completed sessions) never being
        retired — so ``ensure_active_session`` could never start a fresh run.
        Returns ``True`` when a session was actually removed.
        """
        if session_id in self._sessions:
            del self._sessions[session_id]
            self._save_sessions()
            return True
        return False

    def ensure_active_session(
        self,
        owner_id: str = BOOTSTRAP_OWNER,
        *,
        schedule_kickoff: bool = True,
        definition_name: str = "",
    ) -> AutonomousSession:
        """Guarantee *owner_id* has exactly one open (non-completed) session.

        Implements the "auto-restart always" guarantee: the operator always
        has one live autonomous run.  When an open session already exists it
        is returned unchanged.  Otherwise any completed sessions for
        *owner_id* are retired — dropped from both the runner registry and the
        conversation store (with ``create_replacement=False`` so the store
        does not spawn an empty "New chat" husk) — and a fresh autonomous
        session is started.

        *definition_name* is passed through to :meth:`create_session` when a
        new session is spawned.
        """
        for aq in self._sessions.values():
            if aq.owner_id == owner_id and aq.state is not AutonomousState.completed:
                return aq

        # No open session — retire completed ones so they neither accumulate
        # nor leave an orphaned store entry that the UI cannot resolve.
        stale = [
            sid
            for sid, aq in self._sessions.items()
            if aq.owner_id == owner_id and aq.state is AutonomousState.completed
        ]
        for sid in stale:
            self._sessions.pop(sid, None)
            try:
                self._store.delete_session(owner_id, sid, create_replacement=False)
            except Exception:
                logger.exception(
                    "Failed to retire completed autonomous session %s", sid
                )
        if stale:
            self._save_sessions()

        return self.create_session(
            owner_id,
            schedule_kickoff=schedule_kickoff,
            definition_name=definition_name,
        )

    async def _auto_restart(self, owner_id: str) -> None:
        """Throttle, then ensure *owner_id* has a fresh open autonomous session.

        Scheduled after a session completes.  Uses the session definition's
        ``trigger_interval_seconds`` for the throttle delay (periodic trigger),
        or 0 for on-close trigger (immediate restart).
        """
        defn = self._definition_for_owner(owner_id)
        trigger_type = defn.get("trigger_type", "periodic") if defn else "periodic"
        if trigger_type == "on_close":
            delay = 0.0
        else:
            interval = (
                defn.get("trigger_interval_seconds", 45.0)
                if defn
                else self._settings.autonomous.continue_interval_seconds
            )
            delay = max(0.0, float(interval))
        try:
            if delay:
                await asyncio.sleep(delay)
            self.ensure_active_session(
                owner_id,
                definition_name=defn["name"] if defn else "",
            )
        except asyncio.CancelledError:
            logger.debug("Auto-restart task cancelled for owner %s", owner_id)
        except Exception:
            logger.exception("Auto-restart failed for owner %s", owner_id)

    # -- self-refinement ---------------------------------------------------

    @property
    def refinement_store(self) -> RefinementStore | None:
        """The :class:`RefinementStore` used by this runner, or ``None``."""
        return self._refinement_store

    def _schedule_refinement(self, session_id: str, aq: AutonomousSession) -> None:
        """Schedule a refinement step after *aq* completes, if self_refine is on.

        Reads the conversation history from the store, then calls the
        refinement store's ``propose_refinement`` in a background task.
        No-op when the definition does not have ``self_refine`` enabled,
        or when no refinement store is configured.
        """
        if self._refinement_store is None:
            return

        defn = self._definition_for_owner(aq.owner_id)
        if defn is None or not defn.get("self_refine"):
            return

        definition_name = defn.get("name", "")
        base_prompt = defn.get("prompt", "")
        require_approval = defn.get("self_refine_require_approval", False)

        # Capture conversation history for the LLM refinement prompt.
        # Use the raw transcript (both User and Agent sides) so the
        # refinement LLM sees operator feedback, approvals, rejections,
        # and comments — not just the agent's monologue.
        try:
            history = self._store.history(session_id)
            history_text = "\n".join(
                f"User: {turn[0]}\nAgent: {turn[1]}" for turn in history
            )
            # Cap the transcript at a generous character limit to avoid
            # overflowing the refinement LLM's context window.  When
            # truncated, keep the beginning (plan/approval) and the end
            # (outcome) while dropping the middle (auto-continue turns).
            max_chars = 30_000
            if len(history_text) > max_chars:
                head = history_text[: max_chars // 3]
                tail = history_text[-(max_chars * 2 // 3) :]
                history_text = (
                    f"{head}\n\n... [transcript truncated — "
                    f"{len(history)} turns, showing beginning and end] ...\n\n{tail}"
                )
        except Exception:
            logger.exception(
                "Failed to read history for refinement of session %s",
                session_id,
            )
            return

        async def _refine() -> None:
            try:
                await self._refinement_store.propose_refinement(  # type: ignore[union-attr]
                    definition_name=definition_name,
                    base_prompt=base_prompt,
                    session_id=session_id,
                    conversation_history=history_text,
                    auto_accept=not require_approval,
                )
            except Exception:
                logger.exception(
                    "Refinement step failed for definition %r", definition_name
                )

        self._schedule_background(_refine)

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
            # non-periodic subsession (task / user_chat).  Periodic monitors
            # run indefinitely by design and must not deadlock completion.
            # Premature completion closes the session and locks the agent
            # out of spawning tracking monitors, leaving newly-filed tickets
            # untracked.
            if self._has_pending_subsessions(session_id):
                logger.warning(
                    "Autonomous session %s attempted completion while "
                    "pending subsessions (task / user_chat) are still "
                    "running — suppressing",
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
            # Auto-restart always: schedule a fresh autonomous run (throttled)
            # so the operator always has one live session.
            owner_id = aq.owner_id
            self._schedule_background(lambda: self._auto_restart(owner_id))
            # Self-refinement: if this definition has self_refine enabled,
            # schedule a refinement step after the run completes.
            self._schedule_refinement(session_id, aq)
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
        aq.consecutive_no_change = 0

        # Schedule auto-continue as a background task.
        self._schedule_background(lambda: self._auto_continue(session_id))

        self._save_sessions()
        self._publish_state(session_id)

    # -- conversational approval / rejection detection ---------------------

    _APPROVAL_PHRASES: tuple[str, ...] = (
        "approved",
        "approve",
        "lgtm",
        "looks good",
        "go ahead",
        "proceed",
        "yes",
        "ok",
        "okay",
        "start",
        "begin",
        "execute",
        "do it",
        "let's go",
        "go for it",
        "sure",
        "great",
        "perfect",
        "agreed",
        "accepted",
    )
    _REJECTION_PHRASES: tuple[str, ...] = (
        "reject",
        "rejected",
        "no",
        "nope",
        "try again",
        "different",
        "redo",
        "change",
        "not this",
        "stop",
        "cancel",
        "something else",
        "don't",
        "do not",
    )

    def on_user_message(self, session_id: str, message: str = "") -> str:
        """Handle a user message to an autonomous session.

        Analyses the operator's message for conversational approval or
        rejection when the session is in ``proposal`` state:

        * **Approval** — the session transitions to executing and the
          auto-continue loop begins.
        * **Rejection** — the plan subject is recorded in
          ``rejected_subjects``, the session reverts to ``planning``,
          and a new planning turn is scheduled.
        * **Neutral** — the session stays in ``proposal`` so the agent
          can respond conversationally; the operator can approve or
          reject later.
        * **Stalemate** — the same message has been repeated multiple
          times without the operator engaging with proposals; the
          caller should prepend a stalemate notice so the agent
          acknowledges the pattern instead of cycling again.

        Returns ``"approved"``, ``"rejected"``, ``"neutral"``, or
        ``"stalemate"``.
        No-op for unknown sessions (returns ``"neutral"``).
        """
        aq = self._sessions.get(session_id)
        if aq is None:
            return "neutral"

        # -- stalemate detection (all states) ------------------------------
        stripped = message.strip()
        if aq.recent_user_messages is None:
            aq.recent_user_messages = []
        aq.recent_user_messages.append(stripped)
        # Trim to last N so the list stays bounded.
        _max_recent = 10
        if len(aq.recent_user_messages) > _max_recent:
            aq.recent_user_messages = aq.recent_user_messages[-_max_recent:]
        self._save_sessions()

        # Count *consecutive* identical messages from the tail — an
        # intervening different message resets the repeat count.
        consecutive = 0
        for m in reversed(aq.recent_user_messages):
            if m == stripped:
                consecutive += 1
            else:
                break

        # Stalemate: 3+ consecutive occurrences (2 repeats after the first).
        if consecutive >= 3:
            logger.warning(
                "Autonomous session %s — stalemate: user repeated %r "
                "%d times without engaging with proposals",
                session_id,
                stripped[:80],
                consecutive,
            )
            return "stalemate"

        if aq.state is not AutonomousState.proposal:
            return "neutral"

        lower = message.strip().lower()
        # Very short messages with approval keywords are clear approvals.
        is_approval = any(phrase in lower for phrase in self._APPROVAL_PHRASES)
        is_rejection = any(phrase in lower for phrase in self._REJECTION_PHRASES)

        if is_rejection and not is_approval:
            logger.info(
                "Autonomous session %s — operator rejected the proposal",
                session_id,
            )
            self._handle_rejection(session_id)
            return "rejected"

        if is_approval:
            logger.info(
                "Autonomous session %s — operator approved, "
                "transitioning from proposal to executing",
                session_id,
            )
            self._begin_execution(session_id)
            return "approved"

        # Neutral: the operator is discussing the plan without
        # explicitly approving or rejecting it.
        logger.info(
            "Autonomous session %s — operator message is neutral; staying in proposal",
            session_id,
        )
        return "neutral"

    def _handle_rejection(self, session_id: str) -> None:
        """Record plan rejection and schedule a fresh planning turn.

        Copies the current ``plan_text`` subject into ``rejected_subjects``
        so the next planning round avoids it, resets the session to
        ``planning``, and kicks off a new initial turn.
        """
        aq = self._sessions.get(session_id)
        if aq is None:
            return
        # Record the rejected subject.
        if aq.rejected_subjects is None:
            aq.rejected_subjects = []
        if aq.plan_text:
            # Use the first line as the subject for the rejection list.
            subject = aq.plan_text.strip().split("\n", 1)[0].strip()
            if subject:
                aq.rejected_subjects.append(subject)
        aq.state = AutonomousState.planning
        aq.plan_text = ""
        self._save_sessions()
        self._publish_state(session_id)
        # Schedule a fresh planning turn.
        self._schedule_background(
            lambda: self._kickoff_initial_turn(session_id, aq.owner_id)
        )

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
                # Build the kickoff prompt: use the session definition's
                # custom prompt when provided, otherwise fall back to the
                # global initial_task or the standard prompt.
                defn = self._definition_for_owner(owner_id)
                custom_prompt = defn.get("prompt", "") if defn else ""
                rejected_note = _rejected_subjects_note(self._sessions.get(session_id))
                if custom_prompt:
                    # Apply self-refinement addendum when enabled.
                    effective = custom_prompt
                    if (
                        defn
                        and defn.get("self_refine")
                        and self._refinement_store is not None
                    ):
                        effective = self._refinement_store.effective_prompt(
                            defn.get("name", ""), custom_prompt
                        )
                    prompt = f"{restart_notice}{effective}{rejected_note}"
                else:
                    initial_task = self._settings.autonomous.initial_task
                    if initial_task:
                        prompt = (
                            f"{restart_notice}"
                            "Begin a new autonomous session. "
                            f"Initial task: {initial_task}"
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

    def _list_subsessions(self, session_id: str) -> list[Any]:
        """Return the subsession list owned by *session_id*, or an empty list.

        Guards against a missing registry or a registry error — callers
        receive an empty list on any failure path so ``any()`` predicates
        evaluate to ``False``.
        """
        reg = self._subsession_registry
        if reg is None:
            return []
        try:
            return cast("list[Any]", reg.list_for_owner(session_id))
        except Exception:
            return []

    def _has_active_subsessions(self, session_id: str) -> bool:
        """Return True when the session has *any* active subsession.

        Unlike :meth:`_has_pending_subsessions`, this includes periodic
        monitors — used as a pre-completion gate so the session is never
        marked completed while owned background work is still running.
        """
        return any(
            getattr(s, "is_active", False) for s in self._list_subsessions(session_id)
        )

    def _has_pending_subsessions(self, session_id: str) -> bool:
        """Return True when the session has active non-periodic subsessions.

        Periodic subsessions run indefinitely by design — they are not
        "pending" work that the runner should wait for.  Only task and
        user_chat subsessions (which have finite lifetimes) block the
        auto-continue loop.
        """
        return any(
            getattr(s, "is_active", False)
            and getattr(s, "kind", None) not in (None, "periodic")
            for s in self._list_subsessions(session_id)
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
                    # Suppress auto-continue while any subsession is still
                    # active (including periodic monitors sleeping between
                    # ticks).  Only emit Continue when the conversation is
                    # genuinely idle with no pending background work.
                    if self._has_active_subsessions(session_id):
                        continue

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
                                "was ignored because pending subsessions "
                                "(task / user_chat) are still running.  Use "
                                "list_subsessions to check their status, "
                                "and only emit the completion marker when "
                                "all pending subsessions have finished.)"
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

                    # Detect no-op / idle replies and suppress publication.
                    is_noop = _is_no_change(full_reply)
                    if is_noop:
                        aq.consecutive_no_change += 1
                    else:
                        aq.consecutive_no_change = 0

                    if self._event_sink is not None and not is_noop:
                        self._event_sink.publish(
                            session_id,
                            agent_message_frame(full_reply, time.time()),
                        )

                    aq.auto_turn_count += 1
                    self._save_sessions()

                    # Idle cap: halt the loop after N consecutive no-op turns.
                    max_idle = self._settings.autonomous.max_idle_auto_turns
                    if max_idle > 0 and aq.consecutive_no_change >= max_idle:
                        logger.info(
                            "Autonomous session %s hit max_idle_auto_turns "
                            "(%d consecutive no-op turns) — reverting to "
                            "proposal",
                            session_id,
                            max_idle,
                        )
                        aq.state = AutonomousState.proposal
                        self._save_sessions()
                        self._publish_state(session_id)
                        return

                    # Check for lifecycle markers in the reply.
                    new_state = self.check_reply_for_markers(session_id, full_reply)
                    if new_state is AutonomousState.completed:
                        # check_reply_for_markers already schedules the
                        # _auto_restart background task — we only need to
                        # exit the execution loop here.
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
        - When no sessions exist at all for a definition (e.g. a fresh or
          wiped store), auto-start one session per enabled definition so
          autonomous mode is not permanently idle.
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

        # Auto-restart always: guarantee every enabled session definition has
        # exactly one open autonomous session after resume.  Covers a
        # fresh/wiped store as well as the case where the only surviving
        # sessions were completed or closed.
        self.ensure_all_active_sessions()

    def ensure_all_active_sessions(self) -> None:
        """Guarantee one open session per enabled definition (e.g. on startup)."""
        for name in self._definitions:
            owner_id = self._owner_id_for_definition(name)
            logger.info(
                "Ensuring one open autonomous session for definition %r "
                "(owner %r) after resume",
                name,
                owner_id,
            )
            self.ensure_active_session(
                owner_id,
                definition_name=name,
            )

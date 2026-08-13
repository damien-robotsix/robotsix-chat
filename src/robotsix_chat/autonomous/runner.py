"""Autonomous session runner — completion marker detection, auto-cycling."""

from __future__ import annotations

import asyncio
import hashlib
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

# Name of the default session definition — used as a stable key to identify
# the bare ``autonomous`` pseudo-owner when a preset is named "default".
# (No longer auto-synthesized when ``sessions`` is empty — presets are the
# sole enablement model.)
DEFAULT_SESSION_NAME = "default"

# Prefix for owner IDs derived from named session definitions.
_OWNER_ID_PREFIX = "autonomous:"

# Hardcoded disk-persistence path for autonomous session state.
# Formerly ``autonomous.persist_path`` — now internal-only (not surfaced
# to the settings panel).
AUTONOMOUS_PERSIST_PATH = "/data/autonomous_sessions.json"

# Hardcoded default trigger interval (seconds) for synthesized sessions
# and fallback when a session definition lacks ``trigger_interval_seconds``.
_DEFAULT_TRIGGER_INTERVAL = 45.0

# Hardcoded pending-subsession wait timeout (seconds).
# Formerly ``autonomous.pending_subsession_wait_timeout`` — removed from
# the config model as not an operator-facing setting.
_PENDING_SUBSESSION_WAIT_TIMEOUT = 600.0


class AutonomousRunner:
    """Owns autonomous-session lifecycle and drives auto-continue loops."""

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
        self._persist_path = Path(AUTONOMOUS_PERSIST_PATH)
        self._sessions: dict[str, AutonomousSession] = self._load_sessions()
        # Strong references to in-flight auto-continue tasks (see asyncio
        # docs warning on create_task and weak references).
        self._auto_tasks: set[asyncio.Task[None]] = set()
        # Resolve session definitions from the configured presets list.
        # The settings model ships a default preset in its field default
        # (``{"name": "default"}``), so a fresh config always has at least
        # that definition — there is no hidden injection.
        self._definitions = self._resolve_definitions()

    # -- settings accessors -----------------------------------------------

    @property
    def bootstrap_owner(self) -> str:
        """Fixed pseudo-owner id under which autonomous sessions are surfaced."""
        return BOOTSTRAP_OWNER

    # -- board digest ------------------------------------------------------

    async def _mail_board_digest(self) -> str | None:
        """Return a SHA-256 digest of the current auto-mail board content.

        The digest is computed from the raw ``GET /board-content`` JSON so a
        resuming session can detect byte-for-byte identical board state and
        avoid re-running the board check when nothing new has appeared.

        Returns ``None`` when the mail integration is disabled or the board
        cannot be read (unreachable / error response) — callers then simply
        skip no-change detection for this run.
        """
        # ``is True`` (rather than truthiness) keeps MagicMock-based test
        # settings — where ``settings.mail.enabled`` is itself a mock — from
        # accidentally triggering a real network call.
        if getattr(self._settings.mail, "enabled", False) is not True:
            return None
        try:
            from robotsix_chat.mail.client import MailClient

            content = await MailClient(self._settings.mail).board_content()
        except Exception:
            logger.exception("Failed to fetch mail board content for digest")
            return None
        if not content or content.startswith("Mail API"):
            # ``board_content`` returns a diagnostic string on failure;
            # there is no valid board snapshot to hash.
            return None
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # -- persistence ------------------------------------------------------

    @staticmethod
    def _parse_persisted_state(raw: str) -> AutonomousState:
        """Map a persisted state value to the current state model.

        Sessions persisted by older releases used the ``planning`` and
        ``proposal`` states, which no longer exist.  Any non-completed
        legacy state is treated as an open (executing) session so a
        deployment upgrade resumes in-flight runs instead of dropping them.
        """
        if raw == "completed":
            return AutonomousState.completed
        return AutonomousState.executing

    def _save_sessions(self) -> None:
        """Persist the in-memory session registry to disk."""
        try:
            data = {}
            for sid, aq in self._sessions.items():
                data[sid] = {
                    "session_id": aq.session_id,
                    "owner_id": aq.owner_id,
                    "state": aq.state.value,
                    "auto_turn_count": aq.auto_turn_count,
                    "consecutive_no_change": aq.consecutive_no_change,
                    "completion_suppressed": aq.completion_suppressed,
                    "definition_name": aq.definition_name,
                    "last_board_digest": aq.last_board_digest,
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
                    state=self._parse_persisted_state(entry.get("state", "")),
                    auto_turn_count=entry.get("auto_turn_count", 0),
                    consecutive_no_change=entry.get("consecutive_no_change", 0),
                    completion_suppressed=entry.get("completion_suppressed", False),
                    definition_name=entry.get("definition_name", ""),
                    last_board_digest=entry.get("last_board_digest", ""),
                )
            except Exception:
                logger.exception("Skipping unparsable autonomous session %s", sid)
        logger.info(
            "Loaded %d autonomous sessions from %s",
            len(sessions),
            self._persist_path,
        )
        return sessions

    # -- session definitions (config) --------------------------------------

    def _resolve_definitions(self) -> dict[str, Any]:
        """Build the runtime definition registry from config.

        Returns a ``dict[name, definition_dict]``.  Only enabled definitions
        are included.  When the configured ``sessions`` list is empty, no
        sessions run — presets are the sole enablement model.
        """
        configured = self._settings.autonomous.sessions
        if configured and isinstance(configured, list):
            definitions = {
                d.name: {
                    "name": d.name,
                    "prompt": d.prompt,
                    "trigger_type": d.trigger_type.value
                    if hasattr(d.trigger_type, "value")
                    else d.trigger_type,
                    "trigger_interval_seconds": d.trigger_interval_seconds,
                    "max_auto_turns": d.max_auto_turns,
                    "enabled": d.enabled,
                    "self_refine": d.self_refine,
                    "self_refine_require_approval": d.self_refine_require_approval,
                }
                for d in configured
                if d.enabled
            }
            if definitions:
                preset_summary = ", ".join(
                    f"{name} (trigger={d['trigger_type']},"
                    f" interval={d['trigger_interval_seconds']}s)"
                    for name, d in sorted(definitions.items())
                )
                logger.info(
                    "Autonomous session runner loaded %d enabled preset(s): %s",
                    len(definitions),
                    preset_summary,
                )
            else:
                logger.info(
                    "Autonomous session runner: no enabled presets — "
                    "no autonomous sessions will run"
                )
            return definitions
        logger.info(
            "Autonomous session runner: sessions list is empty — "
            "no autonomous sessions will run"
        )
        return {}

    @property
    def definition_count(self) -> int:
        """Number of enabled session definitions currently loaded."""
        return len(self._definitions)

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

        An active session is in a non-terminal state (executing).  Completed
        sessions are ignored.
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
        # Resolve per-session max_auto_turns from the preset definition.
        defn = self._definition_for_owner(aq.owner_id)
        max_turns = defn.get("max_auto_turns", 20) if defn else 20
        self._event_sink.publish(
            session_id,
            autonomous_state_frame(
                session_id=session_id,
                state=aq.state.value,
                auto_turn_count=aq.auto_turn_count,
                max_auto_turns=max_turns,
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

        When *schedule_kickoff* is ``True`` (the default), the autonomous run
        is scheduled as a background task so the session immediately begins
        executing its configured prompt.  Pass ``False`` when the caller will
        handle the kickoff itself (e.g. when the caller will schedule it
        manually).

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
            state=AutonomousState.executing,
            definition_name=resolved_name,
        )
        self._sessions[session_id] = aq
        self._save_sessions()

        # Lifecycle log: session created.
        defn = self._definition_for_owner(owner_id)
        trigger_type = defn.get("trigger_type", "periodic") if defn else "periodic"
        interval = (
            defn.get("trigger_interval_seconds", _DEFAULT_TRIGGER_INTERVAL)
            if defn
            else _DEFAULT_TRIGGER_INTERVAL
        )
        logger.info(
            "Autonomous session spawned: preset=%s session_id=%s "
            "owner_id=%s trigger=%s interval=%.0fs",
            resolved_name,
            session_id,
            owner_id,
            trigger_type,
            interval,
        )

        if schedule_kickoff:
            # Schedule the autonomous run so the session immediately begins
            # executing its configured prompt and continues to completion.
            self._schedule_background(lambda: self._auto_continue(session_id))

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
        preset_name = defn.get("name", "unknown") if defn else "unknown"
        if trigger_type == "on_close":
            delay = 0.0
        else:
            interval = (
                defn.get("trigger_interval_seconds", _DEFAULT_TRIGGER_INTERVAL)
                if defn
                else _DEFAULT_TRIGGER_INTERVAL
            )
            delay = max(0.0, float(interval))
        next_fire_ts = time.time() + delay
        logger.info(
            "Autonomous session restart scheduled: preset=%s owner_id=%s "
            "trigger=%s delay=%.0fs next_fire_ts=%.3f",
            preset_name,
            owner_id,
            trigger_type,
            delay,
            next_fire_ts,
        )
        try:
            if delay:
                await asyncio.sleep(delay)
            logger.info(
                "Autonomous session restart firing: preset=%s owner_id=%s "
                "trigger=%s fire_ts=%.3f",
                preset_name,
                owner_id,
                trigger_type,
                time.time(),
            )
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
        """Scan *reply_text* for the completion marker and close on match.

        Returns ``AutonomousState.completed`` when the session closed,
        ``None`` otherwise.
        """
        aq = self._sessions.get(session_id)
        if aq is None:
            return None

        completion_marker = self._settings.autonomous.completion_marker

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

            self._mark_completed(session_id, aq)
            return AutonomousState.completed

        return None

    # -- completion ----------------------------------------------------------

    def _mark_completed(self, session_id: str, aq: AutonomousSession) -> None:
        """Close *aq*, persisting and scheduling restart + self-refinement."""
        aq.state = AutonomousState.completed
        preset_name = aq.definition_name or "unknown"
        defn = self._definition_for_owner(aq.owner_id)
        trigger_type = defn.get("trigger_type", "periodic") if defn else "periodic"
        interval = (
            defn.get("trigger_interval_seconds", _DEFAULT_TRIGGER_INTERVAL)
            if defn
            else _DEFAULT_TRIGGER_INTERVAL
        )
        next_fire = time.time() + (0.0 if trigger_type == "on_close" else interval)
        logger.info(
            "Autonomous session completed: preset=%s session_id=%s "
            "owner_id=%s trigger=%s interval=%.0fs "
            "next_fire_ts=%.3f",
            preset_name,
            session_id,
            aq.owner_id,
            trigger_type,
            interval,
            next_fire,
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
        timeout = _PENDING_SUBSESSION_WAIT_TIMEOUT
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
        """Drive an autonomous session from kickoff through completion.

        The first turn runs the session definition's custom prompt (or the
        standard autonomous kickoff prompt); subsequent turns run a
        ``Continue`` instruction until the agent emits the completion marker
        (or the turn/idle caps are hit).  When *is_restart* is ``True``, the
        agent is informed that the system was restarted and the session is
        being resumed.
        """
        aq = self._sessions.get(session_id)
        if aq is None:
            return

        owner_id = aq.owner_id
        defn = self._definition_for_owner(owner_id)
        max_turns = defn.get("max_auto_turns", 20) if defn else 20

        # Compute the board digest + restart context once for this run.
        current_board_digest = await self._mail_board_digest()
        board_unchanged = (
            is_restart
            and current_board_digest is not None
            and aq.last_board_digest == current_board_digest
        )

        try:
            while True:
                aq = self._sessions.get(session_id)
                if aq is None or aq.state is not AutonomousState.executing:
                    return

                # Enforce max_auto_turns.
                if aq.auto_turn_count >= max_turns:
                    logger.info(
                        "Autonomous session %s hit max_auto_turns (%d) — closing",
                        session_id,
                        max_turns,
                    )
                    self._mark_completed(session_id, aq)
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

                    if aq.auto_turn_count == 0:
                        restart_notice = ""
                        if is_restart:
                            restart_notice = (
                                "SYSTEM RESTARTED — you are resuming an existing "
                                "autonomous session. "
                            )
                        no_change_note = ""
                        if board_unchanged:
                            no_change_note = (
                                "BOARD UNCHANGED — the auto-mail board content is "
                                "identical to the previous run. Do NOT re-run the "
                                "board content check or output a duplicate board "
                                "digest. Reply with NO_CHANGE and stop.\n\n"
                            )
                        custom_prompt = defn.get("prompt", "") if defn else ""
                        if custom_prompt:
                            effective = custom_prompt
                            if (
                                defn
                                and defn.get("self_refine")
                                and self._refinement_store is not None
                            ):
                                effective = self._refinement_store.effective_prompt(
                                    defn.get("name", ""), custom_prompt
                                )
                            message = f"{restart_notice}{no_change_note}{effective}"
                        else:
                            message = (
                                f"{restart_notice}{no_change_note}"
                                "Begin a new autonomous session and work it to "
                                "completion."
                            )
                        trace_name = "autonomous-init"
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
                        trace_name = "autonomous-continue"

                    # Stream the agent reply.
                    reply_parts: list[str] = []
                    try:
                        async for token in agent.stream(
                            message,
                            history=history,
                            session_id=session_id,
                            client_id=session_id,
                            trace_name=trace_name,
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
                    # Persist the board digest observed for this run so the
                    # next restart can compare against it.
                    if current_board_digest:
                        aq.last_board_digest = current_board_digest
                    self._save_sessions()

                    # Idle cap: halt the loop after N consecutive no-op turns.
                    max_idle = self._settings.autonomous.max_idle_auto_turns
                    if max_idle > 0 and aq.consecutive_no_change >= max_idle:
                        logger.info(
                            "Autonomous session %s hit max_idle_auto_turns "
                            "(%d consecutive no-op turns) — closing",
                            session_id,
                            max_idle,
                        )
                        self._mark_completed(session_id, aq)
                        return

                    # Check for the completion marker in the reply.
                    new_state = self.check_reply_for_markers(session_id, full_reply)
                    if new_state is AutonomousState.completed:
                        # check_reply_for_markers already schedules the
                        # _auto_restart background task — we only need to
                        # exit the execution loop here.
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
        - When no sessions exist at all for a definition (e.g. a fresh or
          wiped store), auto-start one session per enabled definition so
          autonomous mode is not permanently idle.

        **Startup semantics for periodic presets:** each enabled periodic
        preset fires at t=0 (startup), not after one full interval.
        ``trigger_interval_seconds`` is the delay *between completion
        and the next restart*, not an initial delay.  The first run
        begins immediately at startup via :meth:`ensure_all_active_sessions`.
        """
        logger.info(
            "Autonomous session runner resuming: %d persisted sessions, "
            "%d enabled preset(s)",
            len(self._sessions),
            len(self._definitions),
        )
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
            try:
                self._store.register_session(
                    aq.owner_id, session_id, title="Autonomous chat"
                )
            except Exception:
                logger.exception(
                    "Failed to reconcile conversation store for "
                    "autonomous session %s (owner=%s) — continuing",
                    session_id,
                    aq.owner_id,
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

        # Auto-restart always: guarantee every enabled session definition has
        # exactly one open autonomous session after resume.  Covers a
        # fresh/wiped store as well as the case where the only surviving
        # sessions were completed or closed.
        self.ensure_all_active_sessions()

    def ensure_all_active_sessions(self) -> None:
        """Guarantee one open session per enabled definition (e.g. on startup).

        **Periodic presets fire at t=0 (startup).**  The
        ``trigger_interval_seconds`` is the delay between a completed run
        and the next restart — not an initial delay.  This method
        bootstraps every enabled definition immediately, so the first
        run of a periodic preset begins at startup rather than after
        one full interval.
        """
        startup_ts = time.time()
        for name in self._definitions:
            owner_id = self._owner_id_for_definition(name)
            defn = self._definitions.get(name, {})
            trigger_type = defn.get("trigger_type", "periodic")
            interval = defn.get("trigger_interval_seconds", _DEFAULT_TRIGGER_INTERVAL)
            logger.info(
                "Autonomous session startup: ensuring preset=%s "
                "owner_id=%s trigger=%s interval=%.0fs startup_ts=%.3f",
                name,
                owner_id,
                trigger_type,
                interval,
                startup_ts,
            )
            self.ensure_active_session(
                owner_id,
                definition_name=name,
            )

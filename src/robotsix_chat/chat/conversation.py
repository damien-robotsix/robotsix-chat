"""In-memory multi-session conversation tracking for the chat server.

The chat agent is stateless per call, so on its own it treats every message as a
brand-new conversation. :class:`ConversationStore` adds session-scoped continuity:
conversations are now addressable by ``session_id`` and grouped under an
``owner_id`` (the stable per-browser identity).  Each owner can have multiple
named sessions; the store maintains per-session turn history and per-owner
metadata (title, last-active timestamp, turn count, active session).

Sessions are **persistent**: history is never wiped on idle timeout —
sessions survive idle/restart indefinitely.

The store is process-local and unsynchronised: it is sized for the single-worker
``uvicorn.run`` the server uses. Running multiple workers would split an owner's
sessions across processes — acceptable degradation (each worker just sees fewer
turns), never corruption.

The ``max_conversations`` bound is now a cap on total tracked **sessions**
(LRU-evicted).  There is no per-owner minimum retention — simple global LRU is
used.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A single exchanged turn: ``(user_message, assistant_reply)``.
Turn = tuple[str, str]

# Default title for a freshly-created session.
_DEFAULT_TITLE = "New chat"

# Max characters for auto-derived titles (first user message, truncated).
_MAX_TITLE_CHARS = 60


def _derive_title(first_user_message: str) -> str:
    """Derive a session title from the first user message.

    Collapses whitespace, truncates to ~60 chars.
    """
    single_line = " ".join(first_user_message.split())
    if len(single_line) <= _MAX_TITLE_CHARS:
        return single_line
    return single_line[:_MAX_TITLE_CHARS].rstrip() + "\u2026"


def _parse_turns(turns_raw: object, max_history_turns: int) -> list[Turn]:
    """Parse raw turns list and enforce max history truncation."""
    turns: list[Turn] = []
    if isinstance(turns_raw, list):
        for t in turns_raw:
            if isinstance(t, list) and len(t) == 2:
                turns.append((str(t[0]), str(t[1])))
    if len(turns) > max_history_turns:
        turns = turns[-max_history_turns:]
    return turns


@dataclass
class Session:
    """One session: id, metadata, and turn history."""

    session_id: str
    title: str = _DEFAULT_TITLE
    wall_last_active: float = 0.0
    turns: list[Turn] = field(default_factory=list)
    turn_count: int = 0
    closed: bool = False
    # Session kind: ``"chat"`` (default) or ``"autonomous"``.
    kind: str = "chat"
    # Autonomous lifecycle state (``None`` for non-autonomous sessions).
    autonomous_state: str | None = None
    # The plan text produced by the autonomous agent (for UI display).
    autonomous_plan: str | None = None
    # Summary of the turns before ``compacted_turn_index`` — replayed to the
    # agent in place of those turns.  The full ``turns`` list is untouched, so
    # the UI transcript stays complete.
    compacted_summary: str | None = None
    # How many leading entries of ``turns`` the summary covers.  Adjusted when
    # history trimming drops leading turns.
    compacted_turn_index: int = 0
    # LEGACY (pre in-place compaction): id of the continuation session an old
    # compaction created.  Kept so persisted chains still reroute; new
    # compactions never set it.
    compacted_into: str | None = None


@dataclass
class _OwnerState:
    """Per-owner registry: active session id and session lookup."""

    active_session_id: str
    # session_id → Session (backref into the store's global _sessions dict,
    # kept as a set for fast membership test)
    session_ids: set[str] = field(default_factory=set)


class ConversationStoreSerializer:
    """File I/O and format handling for :class:`ConversationStore` persistence.

    Decouples the on-disk JSON serialisation from the in-memory store so
    that :class:`ConversationStore` stays focused on session/owner lifecycle
    and LRU eviction.
    """

    def __init__(self, persist_path: Path) -> None:
        """*persist_path* — filesystem path for the JSON persistence file."""
        self._persist_path = persist_path

    # -- load ---------------------------------------------------------------

    def load(
        self,
        sessions: OrderedDict[str, Session],
        owners: dict[str, _OwnerState],
        *,
        max_history_turns: int,
        wall_clock: Callable[[], float],
    ) -> None:
        """Restore sessions from the persist file (best-effort).

        Supports both the legacy ``{client_id: {session_id, turns}}``
        format (migrated on load) and the current owner→sessions format.
        """
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return  # first run — no saved state yet
        except OSError:
            logger.exception("Failed to load conversations from %s", self._persist_path)
            return
        except json.JSONDecodeError:
            # Preserve the corrupt file instead of silently starting empty —
            # the next persist() would otherwise overwrite it and every
            # session would be unrecoverable.
            backup = self._persist_path.with_suffix(
                self._persist_path.suffix + f".corrupt-{int(time.time())}"
            )
            try:
                self._persist_path.replace(backup)
                logger.exception(
                    "Corrupt conversations file %s preserved as %s; starting empty",
                    self._persist_path,
                    backup,
                )
            except OSError:
                logger.exception(
                    "Failed to load conversations from %s (and could not "
                    "preserve the corrupt file)",
                    self._persist_path,
                )
            return

        if not isinstance(raw, dict):
            return

        now = wall_clock()

        # Detect format: if top-level values are dicts with "turns" (no
        # "active_session_id" or "sessions" sub-object), it's the legacy
        # {client_id: {session_id, turns}} format.
        is_legacy = False
        for entry in raw.values():
            if isinstance(entry, dict) and "turns" in entry:
                is_legacy = True
            break

        if is_legacy:
            self._load_legacy_format(raw, sessions, owners, max_history_turns, now)
        else:
            self._load_current_format(raw, sessions, owners, max_history_turns, now)

    def _load_legacy_format(
        self,
        raw: dict[str, object],
        sessions: OrderedDict[str, Session],
        owners: dict[str, _OwnerState],
        max_history_turns: int,
        now: float,
    ) -> None:
        """Migrate the legacy ``{client_id: {session_id, turns}}`` format.

        Each top-level key becomes an ``owner_id`` whose single session
        (``session_id`` from the stored value or the key itself) becomes
        that owner's default active session.
        """
        for client_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            turns_raw = entry.get("turns")
            if not isinstance(turns_raw, list):
                continue
            turns = _parse_turns(turns_raw, max_history_turns)
            if not turns:
                continue

            session_id = str(entry.get("session_id", client_id))
            title = _DEFAULT_TITLE
            if turns:
                title = _derive_title(turns[0][0])

            session = Session(
                session_id=session_id,
                title=title,
                wall_last_active=now,
                turns=turns,
                turn_count=len(turns),
            )
            sessions[session_id] = session
            owners[client_id] = _OwnerState(
                active_session_id=session_id,
                session_ids={session_id},
            )

    def _load_current_format(
        self,
        raw: dict[str, object],
        sessions: OrderedDict[str, Session],
        owners: dict[str, _OwnerState],
        max_history_turns: int,
        now: float,
    ) -> None:
        """Restore from the current owner→sessions format.

        Expected shape::

            {
              "<owner_id>": {
                "active_session_id": "...",
                "sessions": [
                  {"session_id": "...", "title": "...",
                   "last_active": 1.0, "turn_count": 3,
                   "turns": [["q", "a"], ...]},
                  ...
                ]
              }
            }
        """
        for owner_id, owner_raw in raw.items():
            if not isinstance(owner_raw, dict):
                continue
            active = owner_raw.get("active_session_id")
            sessions_raw = owner_raw.get("sessions")
            if not isinstance(sessions_raw, list):
                continue

            session_ids: set[str] = set()
            for sraw in sessions_raw:
                if not isinstance(sraw, dict):
                    continue
                sid = sraw.get("session_id")
                if not isinstance(sid, str):
                    continue
                turns_raw = sraw.get("turns")
                turns = _parse_turns(turns_raw, max_history_turns)

                title = str(sraw.get("title", _DEFAULT_TITLE))
                last_active = sraw.get("last_active")
                if not isinstance(last_active, int | float):
                    last_active = now

                compacted_summary_raw = sraw.get("compacted_summary")
                compacted_summary = (
                    str(compacted_summary_raw)
                    if isinstance(compacted_summary_raw, str)
                    else None
                )
                compacted_into_raw = sraw.get("compacted_into")
                compacted_into = (
                    str(compacted_into_raw)
                    if isinstance(compacted_into_raw, str)
                    else None
                )
                compacted_turn_index_raw = sraw.get("compacted_turn_index", 0)
                compacted_turn_index = (
                    int(compacted_turn_index_raw)
                    if isinstance(compacted_turn_index_raw, int | float)
                    else 0
                )

                kind_raw = sraw.get("kind", "chat")
                kind = str(kind_raw) if isinstance(kind_raw, str) else "chat"
                autonomous_state_raw = sraw.get("autonomous_state")
                autonomous_state = (
                    str(autonomous_state_raw)
                    if isinstance(autonomous_state_raw, str)
                    else None
                )
                autonomous_plan_raw = sraw.get("autonomous_plan")
                autonomous_plan = (
                    str(autonomous_plan_raw)
                    if isinstance(autonomous_plan_raw, str)
                    else None
                )

                session = Session(
                    session_id=sid,
                    title=title,
                    wall_last_active=float(last_active),
                    turns=turns,
                    turn_count=int(sraw.get("turn_count", len(turns))),
                    closed=bool(sraw.get("closed", False)),
                    kind=kind,
                    autonomous_state=autonomous_state,
                    autonomous_plan=autonomous_plan,
                    compacted_summary=compacted_summary,
                    compacted_turn_index=min(compacted_turn_index, len(turns)),
                    compacted_into=compacted_into,
                )
                sessions[sid] = session
                session_ids.add(sid)

            if not session_ids:
                continue

            active_sid = (
                str(active)
                if isinstance(active, str) and active in session_ids
                else next(iter(session_ids))
            )
            owners[owner_id] = _OwnerState(
                active_session_id=active_sid,
                session_ids=session_ids,
            )

    # -- persist ------------------------------------------------------------

    def persist(
        self,
        owners: dict[str, _OwnerState],
        sessions: OrderedDict[str, Session],
    ) -> None:
        """Write the full conversation state to the persist file."""
        data: dict[str, dict[str, object]] = {}
        for owner_id, owner_state in owners.items():
            sessions_list: list[dict[str, object]] = []
            for sid in owner_state.session_ids:
                session = sessions.get(sid)
                if session is None:
                    continue
                session_dict: dict[str, object] = {
                    "session_id": session.session_id,
                    "title": session.title,
                    "last_active": session.wall_last_active,
                    "turn_count": session.turn_count,
                    "turns": [list(t) for t in session.turns],
                    "closed": session.closed,
                }
                if session.kind != "chat":
                    session_dict["kind"] = session.kind
                if session.autonomous_state is not None:
                    session_dict["autonomous_state"] = session.autonomous_state
                if session.autonomous_plan is not None:
                    session_dict["autonomous_plan"] = session.autonomous_plan
                if session.compacted_summary is not None:
                    session_dict["compacted_summary"] = session.compacted_summary
                if session.compacted_turn_index:
                    session_dict["compacted_turn_index"] = session.compacted_turn_index
                if session.compacted_into is not None:
                    session_dict["compacted_into"] = session.compacted_into
                sessions_list.append(session_dict)
            if sessions_list:
                data[owner_id] = {
                    "active_session_id": owner_state.active_session_id,
                    "sessions": sessions_list,
                }

        # Write-then-rename so a crash or container kill mid-write can never
        # truncate the store — a torn write here loses every session.
        tmp_path = self._persist_path.with_suffix(self._persist_path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp_path.replace(self._persist_path)
        except OSError:
            logger.exception(
                "Failed to persist conversations to %s", self._persist_path
            )


class ConversationStore:
    """Track per-session conversation history with owner grouping.

    Sessions are keyed by ``session_id`` and grouped under ``owner_id``.
    Each owner maintains an ``active_session_id`` and a set of owned
    session ids.  History is capped at ``max_history_turns`` per session
    and the total number of sessions at ``max_conversations`` (global LRU).

    The store supports optional JSON persistence via *persist_path*:
    after every ``record()`` the full state is written to disk so
    sessions survive container restarts.
    """

    def __init__(
        self,
        *,
        max_history_turns: int = 50,
        max_conversations: int = 1000,
        session_factory: Callable[[], str] | None = None,
        persist_path: Path | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """Configure store bounds, session factory, and optional persistence.

        *wall_clock* provides wall-clock timestamps for ``last_active`` metadata;
        defaults to ``time.time`` so tests can inject deterministic values.
        """
        self._max_history_turns = max_history_turns
        self._max_conversations = max_conversations
        self._wall_clock = wall_clock
        self._session_factory = session_factory or (lambda: uuid.uuid4().hex)

        # session_id → Session  (global, insertion-ordered for LRU)
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        # owner_id → _OwnerState
        self._owners: dict[str, _OwnerState] = {}

        self._serializer: ConversationStoreSerializer | None = None
        if persist_path is not None:
            self._serializer = ConversationStoreSerializer(persist_path)
            self._serializer.load(
                self._sessions,
                self._owners,
                max_history_turns=self._max_history_turns,
                wall_clock=self._wall_clock,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def new_session_id(self) -> str:
        """Return a fresh session id."""
        return self._session_factory()

    def begin(self, session_id: str) -> tuple[str, list[Turn]]:
        """Return the current state of *session_id*.

        Returns ``(session_id, history)`` where *history* is a snapshot
        of the session's recent turns.  If the session does not exist it
        is lazily created (empty history).

        Moves the session to the LRU end and evicts overflow.
        """
        session = self._sessions.get(session_id)
        if session is None:
            session = Session(
                session_id=session_id,
                wall_last_active=self._wall_clock(),
            )
            self._sessions[session_id] = session
        else:
            session.wall_last_active = self._wall_clock()

        self._sessions.move_to_end(session_id)
        self._evict_overflow()

        return session.session_id, self._agent_view(session)

    @staticmethod
    def _agent_view(session: Session) -> list[Turn]:
        """Build the agent-facing history: summary + post-summary turns.

        Turns covered by ``compacted_summary`` are replaced by the summary (a
        synthetic ``("", summary)`` leading turn); everything after
        ``compacted_turn_index`` is replayed verbatim.  The raw ``turns`` list
        (the UI transcript) is never mutated.
        """
        history = list(session.turns[session.compacted_turn_index :])
        if session.compacted_summary:
            history.insert(
                0,
                (
                    "",
                    "[Summary of the earlier part of this conversation]\n"
                    + session.compacted_summary,
                ),
            )
        return history

    def agent_history(self, session_id: str) -> list[Turn]:
        """Read-only agent-facing history for *session_id* (see ``_agent_view``).

        Unlike :meth:`begin` this has no side effects (no lazy creation, no
        LRU bump).  Returns an empty list for unknown sessions.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return []
        return self._agent_view(session)

    def record(
        self,
        session_id: str,
        owner_id: str | None,
        user_message: str,
        assistant_reply: str,
    ) -> None:
        """Append a completed exchange to *session_id*.

        Updates the session's title (on the first turn), ``wall_last_active``,
        ``turn_count``, and — when *owner_id* is provided — the owner's
        ``active_session_id``.  Trims history to ``max_history_turns``.

        If the session was evicted, the turn is silently dropped.
        Persists to disk when configured.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return

        # Derive title from the first user message.
        if session.turn_count == 0 and user_message.strip():
            session.title = _derive_title(user_message)

        session.turns.append((user_message, assistant_reply))
        if len(session.turns) > self._max_history_turns:
            trimmed = len(session.turns) - self._max_history_turns
            del session.turns[: -self._max_history_turns]
            # Keep the compaction marker aligned with the surviving turns.
            session.compacted_turn_index = max(
                0, session.compacted_turn_index - trimmed
            )
        session.turn_count += 1
        session.wall_last_active = self._wall_clock()
        self._sessions.move_to_end(session_id)

        if owner_id:
            owner = self._owners.get(owner_id)
            if owner is not None:
                owner.active_session_id = session_id
                owner.session_ids.add(session_id)

        self._persist()

    def record_for_owner(
        self, owner_id: str, user_message: str, assistant_reply: str
    ) -> None:
        """Record a turn into *owner_id*'s active session.

        Best-effort: if the owner has no active session the turn is dropped.
        """
        owner = self._owners.get(owner_id)
        if owner is None:
            return
        self.record(owner.active_session_id, owner_id, user_message, assistant_reply)

    def record_for_session(
        self, session_id: str, user_message: str, assistant_reply: str
    ) -> None:
        """Record a turn into the exact *session_id*.

        Unlike :meth:`record_for_owner`, this targets one specific session
        rather than an owner's *active* session — so background-task and
        check-loop results land in the session that spawned them, even if the
        user has since switched to a different session.

        The session is lazily created if missing (e.g. a tick fires before the
        first turn was persisted), so the turn is never silently dropped.  The
        owner's active-session pointer is intentionally **not** moved.
        """
        if session_id not in self._sessions:
            self._sessions[session_id] = Session(
                session_id=session_id,
                wall_last_active=self._wall_clock(),
            )
            self._sessions.move_to_end(session_id)
            self._evict_overflow()
        # owner_id=None: append to the session without moving any owner's
        # active-session pointer.
        self.record(session_id, None, user_message, assistant_reply)

    def history(self, session_id: str) -> list[Turn]:
        """Return a snapshot copy of *session_id*'s recorded turns.

        Read-only: does not update any metadata or LRU order.
        Returns an empty list for unknown sessions.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return []
        return list(session.turns)

    def get_session(self, session_id: str) -> Session | None:
        """Return the :class:`Session` object for *session_id*, or ``None``.

        Read-only: does not update any metadata or LRU order.
        """
        return self._sessions.get(session_id)

    def set_title(self, session_id: str, title: str) -> bool:
        """Update the title of *session_id* and persist.

        Returns ``True`` if the session was found and updated, ``False``
        if the session does not exist.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        session.title = title
        self._persist()
        return True

    def list_sessions(self, owner_id: str) -> tuple[list[dict[str, object]], str]:
        """Return ``(sessions, active_session_id)`` for *owner_id*.

        *sessions* is a list of session-metadata dicts sorted by
        ``last_active`` descending.  Each dict contains ``session_id``,
        ``title``, ``last_active`` (wall-clock float), and ``turn_count``.

        If the owner has zero sessions, a default empty session is lazily
        created, marked active, and returned (so the list is never empty
        and the client always has a default active session).  This
        side effect is idempotent.
        """
        owner = self._owners.get(owner_id)
        if owner is None:
            # Lazy default session on first access.
            sid = self._session_factory()
            new_session = Session(
                session_id=sid,
                wall_last_active=self._wall_clock(),
            )
            self._sessions[sid] = new_session
            self._owners[owner_id] = _OwnerState(
                active_session_id=sid,
                session_ids={sid},
            )
            self._evict_overflow()
            self._persist()
            return (
                [
                    {
                        "session_id": sid,
                        "title": _DEFAULT_TITLE,
                        "last_active": new_session.wall_last_active,
                        "turn_count": 0,
                        "closed": False,
                        "kind": "chat",
                    }
                ],
                sid,
            )

        result: list[dict[str, object]] = []
        for sid in owner.session_ids:
            sess = self._sessions.get(sid)
            if sess is not None:
                result.append(
                    {
                        "session_id": sess.session_id,
                        "title": sess.title,
                        "last_active": sess.wall_last_active,
                        "turn_count": sess.turn_count,
                        "closed": sess.closed,
                        "kind": sess.kind,
                    }
                )
        # Sort by last_active descending.
        result.sort(key=lambda s: s["last_active"], reverse=True)  # type: ignore[arg-type,return-value]
        return result, owner.active_session_id

    def create_session(
        self, owner_id: str, *, kind: str = "chat"
    ) -> dict[str, object]:
        """Create a new empty session for *owner_id*, mark it active.

        *kind* may be ``"chat"`` (default) or ``"autonomous"``.
        Returns the session metadata dict.
        """
        sid = self._session_factory()
        now = self._wall_clock()
        session = Session(session_id=sid, wall_last_active=now, kind=kind)
        self._sessions[sid] = session

        owner = self._owners.get(owner_id)
        if owner is None:
            self._owners[owner_id] = _OwnerState(
                active_session_id=sid,
                session_ids={sid},
            )
        else:
            owner.active_session_id = sid
            owner.session_ids.add(sid)

        self._sessions.move_to_end(sid)
        self._evict_overflow()
        self._persist()

        return {
            "session_id": sid,
            "title": _DEFAULT_TITLE,
            "last_active": now,
            "turn_count": 0,
            "closed": False,
            "kind": kind,
        }

    def delete_session(self, owner_id: str, session_id: str) -> dict[str, object]:
        """Delete *session_id* (and its history) for *owner_id*.

        When the deleted session was the owner's active session, the
        most-recently-active remaining session becomes active; if none remain
        a fresh empty session is created so the owner always has an active
        session.  Returns ``{"deleted": bool, "active_session_id": str}`` —
        ``deleted`` is ``False`` (no-op) when the owner is unknown or the
        session is not owned by it.

        Note: this only removes conversation state.  Stopping the session's
        background tasks / check loops is the caller's responsibility (the
        ``DELETE /sessions`` endpoint does both).
        """
        owner = self._owners.get(owner_id)
        if owner is None or session_id not in owner.session_ids:
            return {
                "deleted": False,
                "active_session_id": owner.active_session_id if owner else "",
            }

        owner.session_ids.discard(session_id)
        self._sessions.pop(session_id, None)

        if owner.active_session_id == session_id:
            remaining = [
                self._sessions[s] for s in owner.session_ids if s in self._sessions
            ]
            if remaining:
                newest = max(remaining, key=lambda s: s.wall_last_active)
                owner.active_session_id = newest.session_id
            else:
                # No sessions left — create a fresh empty active session so the
                # owner always has one (mirrors list_sessions' lazy default).
                sid = self._session_factory()
                self._sessions[sid] = Session(
                    session_id=sid,
                    wall_last_active=self._wall_clock(),
                )
                self._sessions.move_to_end(sid)
                owner.session_ids.add(sid)
                owner.active_session_id = sid
                self._evict_overflow()

        self._persist()

        return {"deleted": True, "active_session_id": owner.active_session_id}

    def close_session(self, owner_id: str, session_id: str) -> dict[str, object]:
        """Mark *session_id* as closed for *owner_id*.

        A closed session cannot spawn new background tasks or check loops
        (the tools gate on this flag).  Its history and metadata are preserved
        — only the ``closed`` flag is set and no session data is removed.

        Returns ``{"closed": True}`` on success, or
        ``{"closed": False, "reason": "<explanation>"}`` when the owner is
        unknown or the session is not owned by it.  Idempotent: closing an
        already-closed session succeeds but is a no-op.
        """
        owner = self._owners.get(owner_id)
        if owner is None or session_id not in owner.session_ids:
            return {"closed": False, "reason": "session not found"}
        session = self._sessions.get(session_id)
        if session is None:
            return {"closed": False, "reason": "session not found"}
        session.closed = True
        self._persist()
        return {"closed": True}

    def is_session_closed(self, session_id: str) -> bool:
        """Return ``True`` when *session_id* is marked closed.

        Unknown sessions (never created, or evicted) are treated as
        **not closed** — they have no lifecycle flag to honour.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        return session.closed

    def compact_session(
        self,
        owner_id: str,  # noqa: ARG002 — kept for call-site clarity
        session_id: str,
        summary: str,
    ) -> dict[str, object]:
        """Compact *session_id* **in place**: store *summary* over its turns.

        The session keeps its id, title, and full ``turns`` list (the UI
        transcript is untouched); only the agent-facing replay changes —
        turns up to this point are replaced by *summary* (see
        :meth:`agent_history`).  No new session is created, so the session
        list stays stable and subsessions never change owner.

        (The previous design minted a continuation session per idle gap,
        which proliferated "New chat" husks, dragged subsession trees across
        sessions, and stranded clients still posting to the old id.)

        Returns the session's metadata dict including ``compacted_summary``.
        No-op (still returning metadata) for unknown sessions.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return {
                "session_id": session_id,
                "title": _DEFAULT_TITLE,
                "last_active": self._wall_clock(),
                "turn_count": 0,
                "closed": False,
                "compacted_summary": summary,
            }

        session.compacted_summary = summary
        session.compacted_turn_index = len(session.turns)
        self._persist()

        return {
            "session_id": session.session_id,
            "title": session.title,
            "last_active": session.wall_last_active,
            "turn_count": session.turn_count,
            "closed": session.closed,
            "compacted_summary": summary,
        }

    def resolve_session(self, session_id: str) -> str:
        """Follow ``compacted_into`` links to the live continuation session.

        Returns *session_id* itself when the session is unknown or was never
        compacted.  Guards against cycles and unbounded chains by capping the
        walk at the number of tracked sessions.
        """
        seen: set[str] = set()
        current = session_id
        while current not in seen:
            seen.add(current)
            session = self._sessions.get(current)
            if session is None or session.compacted_into is None:
                return current
            current = session.compacted_into
        return current

    def get_compacted_summary(self, session_id: str) -> str | None:
        """Return the compaction summary stored for *session_id*, or ``None``.

        A compaction summary is a plain-text summary of the preceding session's
        conversation that is injected into the agent context when a new session
        is created after an idle timeout.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return session.compacted_summary

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Persist current state to disk when serialization is active."""
        if self._serializer is not None:
            self._serializer.persist(self._owners, self._sessions)

    def _evict_overflow(self) -> None:
        """Pop the least-recently-used session when the cap is exceeded.

        Removes the evicted session id from every owner's ``session_ids``
        registry.
        """
        while len(self._sessions) > self._max_conversations:
            evicted_sid, _ = self._sessions.popitem(last=False)
            # Remove from all owner registries.
            for owner_state in self._owners.values():
                owner_state.session_ids.discard(evicted_sid)

    def recent_activity(
        self, *, limit: int = 20, max_turns: int = 6
    ) -> list[dict[str, Any]]:
        """Return a read-only snapshot of recent cross-session activity.

        Iterates over sessions in most-recently-active-first order
        (``reversed(self._sessions)`` — ``begin``, ``record``, and
        ``create_session`` all call ``move_to_end`` so insertion order is
        oldest → newest).  Returns at most *limit* entries, each a ``dict``
        with ``client_id`` (the owner id, falling back to session id),
        ``session_id``, and ``turns`` (the last *max_turns* turns as a
        **copy**).

        This method is **read-only**: it does not update LRU ordering,
        ``last_activity`` timestamps, or trigger eviction or persistence.

        Complements, but is independent of, the optional cognee episodic
        memory subsystem (``src/robotsix_chat/memory/``) — this returns
        the live, in-process conversation turns; cognee recalls by
        similarity across past sessions.
        """
        result: list[dict[str, Any]] = []
        for session_id, session in reversed(self._sessions.items()):
            if len(result) >= limit:
                break
            # Resolve the owner id for this session.
            owner_id: str | None = None
            for oid, ostate in self._owners.items():
                if session_id in ostate.session_ids:
                    owner_id = oid
                    break
            client_id = owner_id if owner_id is not None else session_id
            turns = list(session.turns[-max_turns:]) if session.turns else []
            result.append(
                {
                    "client_id": client_id,
                    "session_id": session_id,
                    "turns": turns,
                }
            )
        return result

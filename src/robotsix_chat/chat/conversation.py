"""In-memory multi-session conversation tracking for the chat server.

The chat agent is stateless per call, so on its own it treats every message as a
brand-new conversation. :class:`ConversationStore` adds session-scoped continuity:
conversations are now addressable by ``session_id`` and grouped under an
``owner_id``.  Each owner can have multiple named sessions; the store maintains
per-session turn history and per-owner metadata (title, last-active timestamp,
turn count, active session).

This deployment is **single-user**: there is no login, no account, and no
per-browser identity.  Every human-facing owner id collapses to
:data:`OPERATOR_OWNER` (see :func:`canonical_owner_id`), so the same session
list is served to every browser, device, and private window.  The only owner
id that keeps its own pool is the periodic scheduler's reserved one — that
sessions are machine-owned and the UI fetches them as a separate list.

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
from collections.abc import Callable, Iterator
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

# The one and only human owner.  Single-user deployment: every browser is the
# same person, so every non-reserved owner id normalises to this.
OPERATOR_OWNER = "operator"

# Reserved owner id for scheduler-created periodic sessions. It keeps its own
# pool — the scheduler creates its sessions, the UI lists them separately.
# MUST match ``PERIODIC_OWNER`` in ``robotsix_chat/periodic/scheduler.py``
# (duplicated rather than imported to keep this module free of a
# periodic → conversation import cycle).
_PERIODIC_OWNER = "periodic"


def canonical_owner_id(owner_id: str) -> str:
    """Return the owner pool *owner_id* belongs to.

    The reserved periodic owner is returned unchanged; everything else — any
    client-supplied id, however it was minted — collapses to
    :data:`OPERATOR_OWNER`.  This is what makes the session list identical
    from every access point.
    """
    if owner_id == _PERIODIC_OWNER:
        return owner_id
    return OPERATOR_OWNER


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


def _parse_actions(actions_raw: object, n_turns: int) -> list[list[str]]:
    """Parse the persisted actions log and align it with *n_turns* turns.

    Missing (pre-actions-log sessions), malformed, or misaligned data
    degrades to empty per-turn lists — old persisted sessions always load.
    When the log is longer than the turns (history was trimmed from the
    front on save), the trailing entries are kept so they stay aligned with
    the surviving turns.
    """
    parsed: list[list[str]] = []
    if isinstance(actions_raw, list):
        for entry in actions_raw:
            if isinstance(entry, list):
                parsed.append([str(a) for a in entry if isinstance(a, str)])
            else:
                parsed.append([])
    if len(parsed) > n_turns:
        parsed = parsed[-n_turns:]
    while len(parsed) < n_turns:
        parsed.append([])
    return parsed


@dataclass
class Session:
    """One session: id, metadata, and turn history."""

    session_id: str
    title: str = _DEFAULT_TITLE
    wall_last_active: float = 0.0
    turns: list[Turn] = field(default_factory=list)
    # Per-turn actions log, aligned with ``turns``: ``actions[i]`` lists the
    # compact ``tool(args) -> result`` entries the assistant performed while
    # producing ``turns[i]`` (see :mod:`robotsix_chat.chat.actions`).  Always
    # kept the same length as ``turns``; empty lists for turns without tool
    # calls and for sessions persisted before the log existed.
    actions: list[list[str]] = field(default_factory=list)
    turn_count: int = 0
    closed: bool = False
    # Summary of the turns before ``compacted_turn_index`` — replayed to the
    # agent in place of those turns.  The full ``turns`` list is untouched, so
    # the UI transcript stays complete.
    compacted_summary: str | None = None
    # How many leading entries of ``turns`` the summary covers.  Adjusted when
    # history trimming drops leading turns.
    compacted_turn_index: int = 0
    # Escalated model level for this session, set by the agent's
    # ``escalate_model`` tool when it judges the configured tier insufficient.
    # ``None`` means "use the server's configured chat level".  Sticky for the
    # session's lifetime: a session that needed the stronger tier usually keeps
    # needing it, and switching back mid-session would rebuild the prompt cache.
    model_level: int | None = None
    # LEGACY (pre in-place compaction): id of the continuation session an old
    # compaction created.  Kept so persisted chains still reroute; new
    # compactions never set it.
    compacted_into: str | None = None
    # Marks the single "evergoing" session: a never-ending session whose
    # context is kept bounded by subject-aware auto-trimming (see
    # :meth:`ConversationStore.trim_session`) rather than by summarisation.
    # Exactly one session should carry this flag at a time.
    evergoing: bool = False
    # Index into ``turns`` marking how many leading turns have been physically
    # trimmed out of the active context by the auto-trim pass.  Unlike
    # compaction (which condenses into a summary), trimmed turns are simply
    # dropped from the agent view and the UI transcript — they remain
    # recoverable only via conversation memory (cognee).  Turns before this
    # index are considered removed.
    trimmed_turn_index: int = 0
    # Watermark: the ``turn_count`` value at the last trim pass.  The periodic
    # trim agent compares this against the live ``turn_count`` to decide
    # whether any new input has arrived since — if equal, the pass is skipped
    # and no LLM call is made.
    last_trim_turn_count: int = 0


def _session_metadata(session: Session) -> dict[str, object]:
    """Return the standard session-metadata dict for *session*."""
    return {
        "session_id": session.session_id,
        "title": session.title,
        "last_active": session.wall_last_active,
        "turn_count": session.turn_count,
        "closed": session.closed,
        "model_level": session.model_level,
        "evergoing": session.evergoing,
    }


@dataclass
class _OwnerState:
    """Per-owner registry: active session id and session lookup."""

    active_session_id: str
    # session_id → Session (backref into the store's global _sessions dict,
    # kept as a set for fast membership test)
    session_ids: set[str] = field(default_factory=set)


def _merge_owner(
    owners: dict[str, _OwnerState],
    sessions: OrderedDict[str, Session],
    owner_id: str,
    active_session_id: str,
    session_ids: set[str],
) -> None:
    """Fold one persisted owner record into *owners* under its canonical id.

    On-disk state predating the single-user collapse holds one owner per
    browser that ever opened the UI.  Those all canonicalise to the same key,
    so the records are unioned rather than overwritten — otherwise the last
    owner read would silently drop every earlier browser's sessions.  The
    surviving active pointer is the more recently active of the candidates.
    """
    existing = owners.get(owner_id)
    if existing is None:
        owners[owner_id] = _OwnerState(
            active_session_id=active_session_id,
            session_ids=session_ids,
        )
        return

    existing.session_ids |= session_ids

    def _last_active(sid: str) -> float:
        session = sessions.get(sid)
        return session.wall_last_active if session is not None else -1.0

    if _last_active(active_session_id) > _last_active(existing.active_session_id):
        existing.active_session_id = active_session_id


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
            _merge_owner(
                owners,
                sessions,
                canonical_owner_id(client_id),
                session_id,
                {session_id},
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
                actions = _parse_actions(sraw.get("actions"), len(turns))

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
                trimmed_turn_index_raw = sraw.get("trimmed_turn_index", 0)
                trimmed_turn_index = (
                    int(trimmed_turn_index_raw)
                    if isinstance(trimmed_turn_index_raw, int | float)
                    else 0
                )
                last_trim_turn_count_raw = sraw.get("last_trim_turn_count", 0)
                last_trim_turn_count = (
                    int(last_trim_turn_count_raw)
                    if isinstance(last_trim_turn_count_raw, int | float)
                    else 0
                )

                session = Session(
                    session_id=sid,
                    title=title,
                    wall_last_active=float(last_active),
                    turns=turns,
                    actions=actions,
                    turn_count=int(sraw.get("turn_count", len(turns))),
                    closed=bool(sraw.get("closed", False)),
                    model_level=(
                        int(model_level_raw)
                        if isinstance(model_level_raw := sraw.get("model_level"), int)
                        else None
                    ),
                    compacted_summary=compacted_summary,
                    compacted_turn_index=min(compacted_turn_index, len(turns)),
                    compacted_into=compacted_into,
                    evergoing=bool(sraw.get("evergoing", False)),
                    trimmed_turn_index=min(trimmed_turn_index, len(turns)),
                    last_trim_turn_count=last_trim_turn_count,
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
            _merge_owner(
                owners,
                sessions,
                canonical_owner_id(owner_id),
                active_sid,
                session_ids,
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
                if session.model_level is not None:
                    session_dict["model_level"] = session.model_level
                # Written only when at least one turn logged an action — a
                # session without tool calls keeps the pre-actions-log shape.
                if any(session.actions):
                    session_dict["actions"] = [list(a) for a in session.actions]
                if session.compacted_summary is not None:
                    session_dict["compacted_summary"] = session.compacted_summary
                if session.compacted_turn_index:
                    session_dict["compacted_turn_index"] = session.compacted_turn_index
                if session.compacted_into is not None:
                    session_dict["compacted_into"] = session.compacted_into
                if session.evergoing:
                    session_dict["evergoing"] = True
                if session.trimmed_turn_index:
                    session_dict["trimmed_turn_index"] = session.trimmed_turn_index
                if session.last_trim_turn_count:
                    session_dict["last_trim_turn_count"] = session.last_trim_turn_count
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

        Turns before ``trimmed_turn_index`` were physically removed from the
        active context by the auto-trim pass and are excluded entirely (they
        are recoverable only via conversation memory, not replayed here).
        """
        start = max(session.compacted_turn_index, session.trimmed_turn_index)
        history = list(session.turns[start:])
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

    def agent_history_actions(self, session_id: str) -> list[list[str]]:
        """Per-turn actions log aligned with :meth:`agent_history`.

        Entry ``i`` lists the actions of turn ``i`` of the agent-facing
        history; the synthetic leading summary turn (when present) gets an
        empty list.  Returns ``[]`` for unknown sessions.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return []
        start = max(session.compacted_turn_index, session.trimmed_turn_index)
        actions = [list(a) for a in self._aligned_actions(session)[start:]]
        if session.compacted_summary:
            actions.insert(0, [])
        return actions

    def history_actions(self, session_id: str) -> list[list[str]]:
        """Per-turn actions log aligned with :meth:`history`.

        Returns ``[]`` for unknown sessions.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return []
        return [list(a) for a in self._aligned_actions(session)]

    @staticmethod
    def _aligned_actions(session: Session) -> list[list[str]]:
        """Return ``session.actions`` padded/trimmed to ``len(session.turns)``."""
        n = len(session.turns)
        actions = session.actions
        if len(actions) == n:
            return actions
        if len(actions) > n:
            return actions[-n:]
        return [*actions, *([] for _ in range(n - len(actions)))]

    def record(
        self,
        session_id: str,
        owner_id: str | None,
        user_message: str,
        assistant_reply: str,
        actions: list[str] | None = None,
    ) -> None:
        """Append a completed exchange to *session_id*.

        *actions* is the turn's actions log — the compact ``tool(args) ->
        result`` entries collected while the agent produced
        *assistant_reply* (see :mod:`robotsix_chat.chat.actions`); ``None``
        or ``[]`` records a turn without tool calls.

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

        session.actions = self._aligned_actions(session)
        session.turns.append((user_message, assistant_reply))
        session.actions.append(list(actions) if actions else [])
        if len(session.turns) > self._max_history_turns:
            trimmed = len(session.turns) - self._max_history_turns
            del session.turns[: -self._max_history_turns]
            del session.actions[: -self._max_history_turns]
            # Keep the compaction / trim markers aligned with the surviving
            # turns — both index into ``turns`` and must shift down when
            # leading turns are dropped.
            session.compacted_turn_index = max(
                0, session.compacted_turn_index - trimmed
            )
            session.trimmed_turn_index = max(0, session.trimmed_turn_index - trimmed)
        session.turn_count += 1
        session.wall_last_active = self._wall_clock()
        self._sessions.move_to_end(session_id)

        if owner_id:
            owner = self._owners.get(canonical_owner_id(owner_id))
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
        owner_id = canonical_owner_id(owner_id)
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

        The FULL transcript, including turns the auto-trim pass removed from
        the agent's context: trimming exists to keep the model prompt lean,
        not to hide conversation from the operator (operator-reported: "the
        conversation looks not so long in my chat window but there is a
        missing part of it"). Only :meth:`agent_history` applies
        ``trimmed_turn_index``.
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

    def get_model_level(self, session_id: str) -> int | None:
        """Return *session_id*'s escalated model level, or ``None`` if unset.

        ``None`` means the session has never escalated and should run at the
        server's configured chat level.
        """
        session = self._sessions.get(session_id)
        return None if session is None else session.model_level

    def set_model_level(self, session_id: str, level: int) -> bool:
        """Pin *session_id* to *level*. Returns ``False`` for unknown sessions.

        Escalation is sticky and one-way in practice — the agent only ever
        raises the level — but this setter does not enforce that, so an
        operator-facing reset stays possible without a schema change.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        session.model_level = level
        self._persist()
        return True

    def list_sessions(
        self, owner_id: str, *, create_default: bool = True
    ) -> tuple[list[dict[str, object]], str]:
        """Return ``(sessions, active_session_id)`` for *owner_id*.

        *sessions* is a list of session-metadata dicts sorted by
        ``last_active`` descending.  Each dict contains ``session_id``,
        ``title``, ``last_active`` (wall-clock float), and ``turn_count``.

        If the owner has zero sessions and *create_default* is ``True`` (the
        default), a default empty session is lazily created, marked active,
        and returned (so the list is never empty and the client always has a
        default active session).  This side effect is idempotent.

        Pass ``create_default=False`` for pseudo-owners that must never get a
        lazily-created browser session (e.g. the periodic scheduler's fixed
        owner) — an empty ``([], "")`` is returned instead.  Otherwise the
        husk would surface in the operator's merged session list as an empty,
        un-closable "New chat" (it is owned by the pseudo-owner, not the
        browser client).
        """
        owner_id = canonical_owner_id(owner_id)

        owner = self._owners.get(owner_id)
        if owner is None:
            if not create_default:
                return ([], "")
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
                [_session_metadata(new_session)],
                sid,
            )

        result: list[dict[str, object]] = []
        for sid in owner.session_ids:
            sess = self._sessions.get(sid)
            if sess is not None:
                result.append(_session_metadata(sess))
        # Sort by last_active descending.
        result.sort(key=lambda s: s["last_active"], reverse=True)  # type: ignore[arg-type,return-value]
        return result, owner.active_session_id

    def register_session(
        self,
        owner_id: str,
        session_id: str,
        *,
        title: str | None = None,
        make_active: bool = False,
    ) -> None:
        """Ensure *session_id* exists and is registered under *owner_id*.

        Creates the session (if missing) and links it into the owner's
        ``session_ids`` set (creating the owner entry if needed), then
        persists.  Idempotent — safe to call repeatedly.

        Unlike :meth:`begin`, which only ensures the session exists in the
        global registry, this also establishes the owner→session link that
        :meth:`list_sessions` and persistence rely on.  It exists for
        out-of-band session drivers (the periodic scheduler) that record turns
        without ever going through the normal ``owner_id``-carrying
        ``record`` path against an already-registered owner — without an
        explicit registration such sessions never appear in
        ``list_sessions`` and are never written to disk (only owner-reachable
        sessions are persisted), so they vanish on restart.

        *title* is applied only when the session is new or has no turns yet,
        so it never clobbers a title already derived from real conversation.
        *make_active* moves the owner's active-session pointer to this
        session (default ``False`` to preserve the owner's current active
        session and the single-active-session invariant).
        """
        owner_id = canonical_owner_id(owner_id)
        session = self._sessions.get(session_id)
        if session is None:
            session = Session(
                session_id=session_id,
                title=title if title is not None else _DEFAULT_TITLE,
                wall_last_active=self._wall_clock(),
            )
            self._sessions[session_id] = session
        elif title is not None and session.turn_count == 0:
            session.title = title
        self._sessions.move_to_end(session_id)

        owner = self._owners.get(owner_id)
        if owner is None:
            self._owners[owner_id] = _OwnerState(
                active_session_id=session_id,
                session_ids={session_id},
            )
        else:
            owner.session_ids.add(session_id)
            if make_active:
                owner.active_session_id = session_id

        self._evict_overflow()
        self._persist()

    def create_session(self, owner_id: str) -> dict[str, object]:
        """Create a new empty session for *owner_id*, mark it active.

        Returns the session metadata dict.
        """
        owner_id = canonical_owner_id(owner_id)
        sid = self._session_factory()
        now = self._wall_clock()
        session = Session(session_id=sid, wall_last_active=now)
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

        return _session_metadata(session)

    def delete_session(
        self,
        owner_id: str,
        session_id: str,
        *,
        create_replacement: bool = True,
    ) -> dict[str, object]:
        """Delete *session_id* (and its history) for *owner_id*.

        When the deleted session was the owner's active session, the
        most-recently-active remaining session becomes active; if none remain
        and *create_replacement* is ``True`` (the default) a fresh empty
        session is created so the owner always has an active session.  Pass
        ``create_replacement=False`` for pseudo-owners (e.g. the periodic
        runner's fixed owner) so no empty "New chat" husk is spawned — the
        active pointer is cleared to ``""`` instead.  Returns
        ``{"deleted": bool, "active_session_id": str}`` — ``deleted`` is
        ``False`` (no-op) when the owner is unknown or the session is not
        owned by it.

        Note: this only removes conversation state.  Stopping the session's
        background tasks / check loops is the caller's responsibility (the
        ``DELETE /sessions`` endpoint does both).
        """
        owner = self._owners.get(canonical_owner_id(owner_id))
        if owner is None or session_id not in owner.session_ids:
            return {
                "deleted": False,
                "active_session_id": owner.active_session_id if owner else "",
            }

        owner.session_ids.discard(session_id)
        # Only destroy the conversation itself once no owner references it.
        # A session can be dual-owned: ``record`` registers it under whoever
        # sends a turn, so a periodic session the operator chats with ends
        # up in both owners' registries.  Popping it unconditionally left the
        # other owner holding a dangling id, which ``begin`` then silently
        # re-created as a blank session — the operator kept typing into what
        # looked like the same chat while its history was gone.
        if not self._owner_ids_for(session_id):
            self._sessions.pop(session_id, None)

        if owner.active_session_id == session_id:
            remaining = [
                self._sessions[s] for s in owner.session_ids if s in self._sessions
            ]
            if remaining:
                newest = max(remaining, key=lambda s: s.wall_last_active)
                owner.active_session_id = newest.session_id
            elif not create_replacement:
                # Pseudo-owner: leave the owner with no active session rather
                # than spawning an empty husk.
                owner.active_session_id = ""
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
        owner = self._owners.get(canonical_owner_id(owner_id))
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

    def reopen_session(self, session_id: str) -> bool:
        """Clear the ``closed`` flag on *session_id* (idempotent).

        Returns ``True`` when the session was closed and is now open again;
        ``False`` when the session is unknown or was already open.

        An operator turn reopens a closed session — the conversation is
        still live as long as the operator keeps messaging it, so
        subsession spawning and steering must work again.  Background
        drivers (the periodic scheduler) never call this; only the chat
        endpoint does, on operator-initiated turns.
        """
        session = self._sessions.get(session_id)
        if session is None or not session.closed:
            return False
        session.closed = False
        self._persist()
        return True

    def last_active(self, session_id: str) -> float | None:
        """Return *session_id*'s wall-clock last-activity timestamp.

        ``None`` when the session is unknown (never created, or evicted).
        Public accessor so external code — e.g. a cleanup sweep
        sweep — can tell a session the operator is actively chatting with
        from an abandoned one without reaching into ``_sessions``.
        """
        session = self._sessions.get(session_id)
        return session.wall_last_active if session is not None else None

    def compact_session(
        self,
        owner_id: str,  # noqa: ARG002 — kept for call-site clarity
        session_id: str,
        summary: str,
        keep_recent_turns: int = 0,
    ) -> dict[str, object]:
        """Compact *session_id* **in place**: store *summary* over its turns.

        The session keeps its id, title, and full ``turns`` list (the UI
        transcript is untouched); only the agent-facing replay changes —
        turns up to this point are replaced by *summary* (see
        :meth:`agent_history`).  The most recent ``keep_recent_turns`` turns
        are left verbatim in the replay so a pending proposal (and its exact
        identifiers) survives compaction.  No new session is created, so the
        session list stays stable and subsessions never change owner.

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

        keep = max(0, min(keep_recent_turns, len(session.turns)))
        session.compacted_summary = summary
        session.compacted_turn_index = len(session.turns) - keep
        # Restart the idle clock: compaction consumes the idle gap, and
        # without this a keep>0 window would re-trigger summarisation on
        # every subsequent message instead of waiting for a fresh idle gap.
        session.wall_last_active = self._wall_clock()
        self._persist()

        return {
            "session_id": session.session_id,
            "title": session.title,
            "last_active": session.wall_last_active,
            "turn_count": session.turn_count,
            "closed": session.closed,
            "compacted_summary": summary,
        }

    def mark_evergoing(self, session_id: str) -> bool:
        """Flag *session_id* as the evergoing session. Persist.

        Returns ``False`` for unknown sessions.  The caller is responsible
        for the single-evergoing-session invariant (see
        :meth:`ensure_evergoing_session`).
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        session.evergoing = True
        self._persist()
        return True

    def evergoing_session_id(self) -> str | None:
        """Return the id of the single evergoing session, or ``None``.

        When more than one session is (erroneously) flagged, the
        most-recently-active one wins so callers get a stable answer.
        """
        candidates = [s for s in self._sessions.values() if s.evergoing]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.wall_last_active).session_id

    def ensure_evergoing_session(self, owner_id: str) -> dict[str, object]:
        """Return the evergoing session for *owner_id*, creating it if absent.

        Guarantees exactly one evergoing session exists: if one is already
        flagged it is returned unchanged; otherwise a fresh session is created,
        flagged evergoing, registered under *owner_id*, and returned.  The
        evergoing session is never auto-closed or auto-evicted by lifecycle
        code — callers must not close it.
        """
        existing = self.evergoing_session_id()
        if existing is not None:
            session = self._sessions.get(existing)
            if session is not None:
                return _session_metadata(session)

        owner_id = canonical_owner_id(owner_id)
        sid = self._session_factory()
        now = self._wall_clock()
        session = Session(
            session_id=sid,
            title="Evergoing session",
            wall_last_active=now,
            evergoing=True,
        )
        self._sessions[sid] = session

        owner = self._owners.get(owner_id)
        if owner is None:
            self._owners[owner_id] = _OwnerState(
                active_session_id=sid,
                session_ids={sid},
            )
        else:
            owner.session_ids.add(sid)

        self._sessions.move_to_end(sid)
        self._evict_overflow()
        self._persist()
        return _session_metadata(session)

    def all_session_ids(self) -> list[str]:
        """Return every live session id (any owner), for the trim scheduler."""
        return list(self._sessions.keys())

    def has_new_input_since_trim(self, session_id: str) -> bool:
        """Return ``True`` when new turns arrived since the last trim pass.

        Compares the live ``turn_count`` against the ``last_trim_turn_count``
        watermark.  The periodic trim agent calls this first and skips the
        pass (making no LLM call) when it returns ``False``.  Unknown sessions
        return ``False`` (nothing to trim).
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        return session.turn_count > session.last_trim_turn_count

    def trim_session(
        self,
        session_id: str,
        new_trimmed_index: int,
        *,
        reason: str = "",
        decided_subject_change: bool | None = None,
        keep_min_recent: int = 1,
    ) -> dict[str, object]:
        """Physically trim leading turns of *session_id* out of the active context.

        Distinct from :meth:`compact_session`: trimming **removes** the leading
        turns from both the agent view and the UI transcript (they are
        recoverable only via conversation memory), whereas compaction condenses
        them into a replayed summary and keeps the full transcript.

        *new_trimmed_index* is the desired number of leading turns to drop.
        The value is clamped to be:

        - **monotonic** — never below the current ``trimmed_turn_index`` (trim
          only ever removes more, never restores);
        - **in-flight safe** — never within ``keep_min_recent`` turns of the
          end, so the turn currently being processed is never trimmed away.

        Regardless of whether any turns were dropped, the ``last_trim_turn_count``
        watermark is advanced to the current ``turn_count`` so a subsequent
        no-input interval is correctly skipped.  Emits an audit log line with
        the decision (how many turns trimmed, why, subject-change verdict).

        Returns an audit dict describing the outcome.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return {"trimmed": False, "reason": "session not found"}

        n_turns = len(session.turns)
        upper_bound = max(0, n_turns - max(0, keep_min_recent))
        target = max(session.trimmed_turn_index, min(new_trimmed_index, upper_bound))
        turns_trimmed = target - session.trimmed_turn_index

        session.trimmed_turn_index = target
        # Keep the compaction marker consistent — and RETIRE a legacy summary
        # the trim has overtaken: once every turn the summary covered is
        # trimmed away, keeping it only makes the UI show a stale "summary of
        # the earlier exchanges" block that ratchets forward with each trim
        # (operator-reported: "the evergoing session keeps being summarized,
        # eating the whole conversation" — no new summary existed; the old
        # one was being stretched).
        if session.trimmed_turn_index >= session.compacted_turn_index:
            session.compacted_summary = None
            session.compacted_turn_index = session.trimmed_turn_index
        session.last_trim_turn_count = session.turn_count
        session.wall_last_active = self._wall_clock()
        self._persist()

        logger.info(
            "trim_session session=%s turns_trimmed=%d new_trimmed_index=%d "
            "turn_count=%d subject_change=%s reason=%s",
            session_id,
            turns_trimmed,
            target,
            session.turn_count,
            decided_subject_change,
            reason or "(none given)",
        )

        return {
            "trimmed": turns_trimmed > 0,
            "turns_trimmed": turns_trimmed,
            "trimmed_turn_index": target,
            "turn_count": session.turn_count,
            "decided_subject_change": decided_subject_change,
            "reason": reason,
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
        registry.  The evergoing session is never evicted — it must survive
        indefinitely — so it is skipped over when selecting the LRU victim.
        """
        while len(self._sessions) > self._max_conversations:
            victim_sid: str | None = None
            for sid, session in self._sessions.items():
                if not session.evergoing:
                    victim_sid = sid
                    break
            if victim_sid is None:
                # Only evergoing sessions remain — nothing to evict.
                break
            del self._sessions[victim_sid]
            # Remove from all owner registries.
            for owner_state in self._owners.values():
                owner_state.session_ids.discard(victim_sid)

    def _owner_ids_for(self, session_id: str) -> list[str]:
        """Return every owner id whose registry still holds *session_id*.

        A session is normally owned once, but ``record`` adds it to whichever
        owner sends a turn, so a periodic session the operator chats with
        is genuinely dual-owned.
        """
        return [
            oid
            for oid, ostate in self._owners.items()
            if session_id in ostate.session_ids
        ]

    def owner_ids_for(self, session_id: str) -> list[str]:
        """Return every owner id whose registry still holds *session_id*.

        Public accessor over :meth:`_owner_ids_for` so external code (e.g. the
        delete endpoint) can find every scope a dual-owned session is
        reachable from without reaching into private state.
        """
        return self._owner_ids_for(session_id)

    def owner_for_session(self, session_id: str) -> str | None:
        """Return the ``owner_id`` that owns *session_id*, or ``None``.

        Public accessor so external code (e.g. the periodic scheduler) can
        resolve session ownership without reaching into private state.
        """
        for oid, ostate in self._owners.items():
            if session_id in ostate.session_ids:
                return oid
        return None

    def iter_sessions(self) -> Iterator[tuple[str, Session]]:
        """Yield ``(session_id, Session)`` pairs for every tracked session.

        Public accessor for external iteration without reaching into
        ``_sessions`` directly.
        """
        yield from self._sessions.items()

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

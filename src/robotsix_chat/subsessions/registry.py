"""Subsession registry — in-memory state, inboxes, persistence, SSE frames.

The :class:`SubsessionRegistry` is the single source of truth for every
subsession in the process.  It owns:

* the ``SubsessionInfo`` records (all kinds, all depths),
* a strong reference to each in-flight worker :class:`asyncio.Task`,
* a per-subsession **inbox** (deque + wake event) for messages delivered
  at the subsession's next turn boundary,
* JSON persistence at ``/data/subsessions.json`` (full-state rewrite on
  every mutation, mirroring the previous check-loop registry), and
* SSE lifecycle publishing via the injected
  :class:`~robotsix_chat.chat.events.EventSink` — every frame is
  published to the subsession's ``owner_session_id`` (the root UI chat
  session) so nested subsessions surface in the owning browser tab.

Single-worker asyncio process: the dicts are unsynchronised on purpose
(same stance as ``ConversationStore``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path

from robotsix_chat.chat.events import (
    EventSink,
    subsession_closed_frame,
    subsession_failed_frame,
    subsession_message_frame,
    subsession_started_frame,
    subsession_updated_frame,
)

from .models import (
    ACTIVE_STATUSES,
    InboxMessage,
    SubsessionDedupError,
    SubsessionInfo,
    SubsessionKind,
    SubsessionStatus,
    TranscriptEntry,
)

logger = logging.getLogger(__name__)

# Terminal entries retained in memory/persistence so the panel can show
# recent history after a reload; older ones are pruned oldest-first.
_MAX_TERMINAL_ENTRIES = 50

# Cap on persisted (turn_input, reply) pairs per subsession — must match
# worker._MAX_WORKER_HISTORY_TURNS (the replay window the worker actually
# feeds the agent); capping here too bounds what's kept in the JSON store.
_MAX_TURN_HISTORY_ENTRIES = 20


def _truncate(text: str, limit: int) -> str:
    """Clip *text* to *limit* characters with an ellipsis marker."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _checkpoint_ticket_id(checkpoint: dict[str, object] | None) -> str:
    """Return the ``ticket_id`` in *checkpoint* when it is a non-empty string."""
    raw = checkpoint.get("ticket_id") if checkpoint else None
    return raw if isinstance(raw, str) and raw else ""


def _preserve_event_ticket_id(
    info: SubsessionInfo, checkpoint: dict[str, object] | None
) -> dict[str, object] | None:
    """Keep the system-owned ``ticket_id`` when a WAIT_FOR_EVENT checkpoint is replaced.

    ``set_checkpoint`` (and any other checkpoint writer) replaces the whole
    dict; a replacement that omits ``ticket_id`` would leave an
    event-driven monitor unable to filter mill events after the next
    restart.  Recover the previous value from the current checkpoint or,
    failing that, the subsession's ``dedup_key`` (which for ticket
    monitors is always the ticket id).
    """
    if _checkpoint_ticket_id(checkpoint):
        return checkpoint
    ticket_id = _checkpoint_ticket_id(info.checkpoint) or info.dedup_key
    if not ticket_id:
        return checkpoint
    merged = dict(checkpoint or {})
    merged["ticket_id"] = ticket_id
    return merged


def _checkpoint_auto_stop_no_change_runs(
    checkpoint: dict[str, object] | None,
) -> int | None:
    """Return the valid per-spawn no-change threshold in *checkpoint*, if any.

    Mirrors the read in :func:`_run_periodic_turn`: only a positive
    ``int`` (excluding ``bool``, which is an ``int`` subclass) is a valid
    override — anything else is treated as absent.
    """
    raw = checkpoint.get("auto_stop_no_change_runs") if checkpoint else None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return raw


def _preserve_periodic_auto_stop_no_change_runs(
    info: SubsessionInfo, checkpoint: dict[str, object] | None
) -> dict[str, object] | None:
    """Keep a PERIODIC monitor's ``auto_stop_no_change_runs`` override on replacement.

    The override is seeded into the checkpoint at spawn time.  Because
    ``set_checkpoint`` (and other checkpoint writers) replace the whole
    dict, a replacement that omits ``auto_stop_no_change_runs`` would
    silently drop the override and let the monitor fall back to the global
    ``subsessions.auto_stop_no_change_runs`` default — re-triggering the
    premature auto-stop this override exists to prevent.  Recover the
    previous value from the current checkpoint when it is missing.
    """
    if _checkpoint_auto_stop_no_change_runs(checkpoint) is not None:
        return checkpoint
    previous = _checkpoint_auto_stop_no_change_runs(info.checkpoint)
    if previous is None:
        return checkpoint
    merged = dict(checkpoint or {})
    merged["auto_stop_no_change_runs"] = previous
    return merged


def _checkpoint_no_change_pause_count(
    checkpoint: dict[str, object] | None,
) -> int | None:
    """Return the no-change pause counter in *checkpoint*, if present.

    ``0`` is a meaningful value (progress was observed since the last
    pause), so — unlike the auto-stop override helper — this only
    distinguishes *presence* from absence and does not validate the
    magnitude.  A bool or non-int value is treated as absent.
    """
    if not checkpoint or "no_change_pause_count" not in checkpoint:
        return None
    raw = checkpoint["no_change_pause_count"]
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _preserve_periodic_no_change_pause_count(
    info: SubsessionInfo, checkpoint: dict[str, object] | None
) -> dict[str, object] | None:
    """Keep a PERIODIC monitor's no-change pause counter on replacement.

    The counter is seeded into the checkpoint by the periodic turn loop
    when the monitor auto-pauses.  ``set_checkpoint`` (and other
    checkpoint writers) replace the whole dict; a replacement that omits
    ``no_change_pause_count`` would silently reset the escalation budget
    and let a stuck monitor keep pausing forever.  Recover the previous
    value from the current checkpoint when it is missing.
    """
    if _checkpoint_no_change_pause_count(checkpoint) is not None:
        return checkpoint
    previous = _checkpoint_no_change_pause_count(info.checkpoint)
    if previous is None:
        return checkpoint
    merged = dict(checkpoint or {})
    merged["no_change_pause_count"] = previous
    return merged


def _preserve_periodic_progress_flags(
    info: SubsessionInfo, checkpoint: dict[str, object] | None
) -> dict[str, object] | None:
    """Keep a PERIODIC monitor's ``recent_progress_flags`` on replacement.

    The adaptive run-budget extension relies on the rolling progress
    window surviving ``set_checkpoint`` calls, which replace the whole
    dict.  Recover the previous window when a replacement omits it so a
    monitor that records state every run still counts progress across the
    configured ``max_runs_progress_window``.
    """
    if checkpoint is not None and isinstance(
        checkpoint.get("recent_progress_flags"), list
    ):
        return checkpoint
    previous = info.checkpoint or {}
    if not isinstance(previous.get("recent_progress_flags"), list):
        return checkpoint
    merged = dict(checkpoint or {})
    merged["recent_progress_flags"] = previous["recent_progress_flags"]
    return merged


class RegistryStore:
    """JSON persistence for subsession records — file I/O and terminal retention.

    Owns the store-path, serialisation, and pruning of old terminal
    entries.  Mutates the shared dicts in-place so no return-value
    synchronisation is needed.
    """

    def __init__(
        self,
        store_path: Path | None,
        subs: dict[str, SubsessionInfo],
        inboxes: dict[str, deque[InboxMessage]],
        wake_events: dict[str, asyncio.Event],
        by_owner: dict[str, set[str]],
    ) -> None:
        """*store_path* is the JSON file; the dicts are shared references."""
        self._store_path = store_path
        self._subs = subs
        self._inboxes = inboxes
        self._wake_events = wake_events
        self._by_owner = by_owner

    # ------------------------------------------------------------------
    # public (called by SubsessionRegistry / startup)
    # ------------------------------------------------------------------

    def load_persisted(self) -> list[dict[str, object]]:
        """Read raw persisted entries for the startup resume hook.

        Returns ``[]`` when persistence is disabled, the file is missing,
        or it cannot be parsed (a corrupt store must not block startup).
        """
        if self._store_path is None or not self._store_path.exists():
            return []
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            logger.exception("Could not read subsession store %s", self._store_path)
            return []
        return raw if isinstance(raw, list) else []

    def persist(self) -> None:
        """Write the full registry state as JSON (skipped when disabled)."""
        if self._store_path is None:
            return
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create parent dir for %s", self._store_path)
            return
        entries: list[dict[str, object]] = []
        for info in self._subs.values():
            entry = info.snapshot(with_transcript=True)
            inbox = self._inboxes.get(info.id)
            entry["inbox"] = [message.as_dict() for message in inbox] if inbox else []
            entries.append(entry)
        # Write-then-rename so a crash or container kill mid-write can never
        # truncate the store.
        tmp_path = self._store_path.with_suffix(self._store_path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            tmp_path.replace(self._store_path)
        except OSError:
            logger.exception("Failed to persist subsessions to %s", self._store_path)

    def prune_terminal(self) -> None:
        """Drop the oldest terminal entries beyond the retention cap."""
        terminal = sorted(
            (info for info in self._subs.values() if not info.is_active),
            key=lambda i: i.last_activity_at,
        )
        for info in terminal[: max(0, len(terminal) - _MAX_TERMINAL_ENTRIES)]:
            self._subs.pop(info.id, None)
            self._inboxes.pop(info.id, None)
            self._wake_events.pop(info.id, None)
            owner_set = self._by_owner.get(info.owner_session_id)
            if owner_set is not None:
                owner_set.discard(info.id)
                if not owner_set:
                    del self._by_owner[info.owner_session_id]


class RegistryIndex:
    """Owner-scoped queries and tree operations.

    Owns the ``_by_owner`` index and provides fixpoint tree walks,
    owner reassignment, orphan reaping, and bulk-close operations.
    Receives shared dict references from the parent
    :class:`SubsessionRegistry` and calls back into it for SSE publishing
    and persistence.
    """

    def __init__(
        self,
        subs: dict[str, SubsessionInfo],
        by_owner: dict[str, set[str]],
        running: dict[str, asyncio.Task[None]],
        registry: SubsessionRegistry,
    ) -> None:
        """*subs*, *by_owner*, *running* are shared refs; *registry* is the parent."""
        self._subs = subs
        self._by_owner = by_owner
        self._running = running
        self._registry = registry

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def list_for_owner(self, owner_session_id: str) -> list[SubsessionInfo]:
        """Return the whole subsession tree for an owner, oldest first."""
        infos = [
            self._subs[sub_id]
            for sub_id in self._by_owner.get(owner_session_id, ())
            if sub_id in self._subs
        ]
        return sorted(infos, key=lambda i: i.created_at)

    def list_descendants(self, root_id: str) -> list[SubsessionInfo]:
        """Return every (transitive) child of subsession *root_id*."""
        root = self._subs.get(root_id)
        if root is None:
            return []
        tree = self.list_for_owner(root.owner_session_id)
        descendants: list[SubsessionInfo] = []
        frontier = {root_id}
        # Tree is small (bounded by the concurrency cap + terminal tail);
        # a simple fixpoint pass keeps this dependency-free.
        changed = True
        while changed:
            changed = False
            for info in tree:
                if info.parent_id in frontier and info.id not in frontier:
                    frontier.add(info.id)
                    descendants.append(info)
                    changed = True
        return descendants

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------

    def reap_orphans(self) -> int:
        """Cancel any timer whose subsession id is not in a conversation tree.

        An orphaned subsession has a live worker task but no tree
        membership — the record was removed while the timer survived.
        Returns the number of timers cancelled.
        """
        orphaned: list[str] = []
        for sub_id, task in list(self._running.items()):
            if task.done():
                continue
            found = any(sub_id in owner_ids for owner_ids in self._by_owner.values())
            if not found:
                orphaned.append(sub_id)

        for sub_id in orphaned:
            orphan_task = self._running.get(sub_id)
            if orphan_task is not None and not orphan_task.done():
                orphan_task.cancel()
            logger.warning(
                "Reaped orphaned subsession timer %s — tree record was lost.",
                sub_id,
            )
            # Transition to FAILED so the subsession no longer counts
            # against the concurrency cap and shows as terminal in the
            # UI.  _close_and_publish handles the frame, status mutation
            # and persistence atomically.
            info = self._subs.get(sub_id)
            if info is not None:
                self._registry._close_and_publish(
                    info,
                    status=SubsessionStatus.FAILED,
                    summary=(
                        "This subsession's tree record was lost; its "
                        "timer has been cancelled."
                    ),
                    error="orphaned_timer_reaped",
                )
        return len(orphaned)

    def close_all_for_owner(self, owner_session_id: str, *, reason: str) -> int:
        """Close every active subsession owned by *owner_session_id*.

        Used when a chat session is closed/deleted so its background work
        does not outlive it.  No summaries are delivered — the parent
        session is going away.  Returns the number actually closed.
        """
        closed = 0
        for sub_id in list(self._by_owner.get(owner_session_id, ())):
            if self._registry.cancel_and_close(
                sub_id, reason=reason, closed_by="system"
            ):
                closed += 1
        return closed

    def reassign_owner(
        self, old_owner_session_id: str, new_owner_session_id: str
    ) -> int:
        """Move every subsession owned by *old_owner_session_id* to the new owner.

        Used when an idle-timeout compaction replaces a chat session with a
        continuation session: the whole subsession tree (all kinds, all
        statuses) follows the conversation, so running work keeps delivering
        summaries to the session the user is actually in and the UI panel for
        the continuation shows the full tree.

        Publishes a ``subsession_started`` frame per moved subsession to the
        new owner's event stream so an already-subscribed browser picks them
        up without a refetch.  Returns the number of subsessions moved.
        """
        if old_owner_session_id == new_owner_session_id:
            return 0
        sub_ids = self._by_owner.pop(old_owner_session_id, None)
        if not sub_ids:
            return 0
        moved = 0
        for sub_id in sub_ids:
            info = self._subs.get(sub_id)
            if info is None:
                continue
            info.owner_session_id = new_owner_session_id
            self._by_owner[new_owner_session_id].add(sub_id)
            self._registry._publish(
                new_owner_session_id,
                subsession_started_frame(info.snapshot()),
            )
            moved += 1
        self._registry._store.persist()
        return moved


class SubsessionRegistry:
    """Track every subsession in the process (see module docstring)."""

    def __init__(
        self,
        *,
        event_sink: EventSink | None = None,
        store_path: Path | None = Path("/data/subsessions.json"),
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
        transcript_max_entries: int = 200,
    ) -> None:
        """Configure the sink, JSON store path, clock, and transcript cap.

        *store_path* defaults to ``/data/subsessions.json``; pass ``None``
        to disable persistence (tests).  *clock* must return wall-clock
        seconds (``time.time``) — timestamps are shown in the UI and
        persisted across restarts.
        """
        self._event_sink = event_sink
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._transcript_max_entries = transcript_max_entries
        # sub_id → SubsessionInfo (all statuses, terminal entries pruned).
        self._subs: dict[str, SubsessionInfo] = {}
        # sub_id → asyncio.Task (strong ref so workers are not GC'd).
        self._running: dict[str, asyncio.Task[None]] = {}
        # sub_id → inbox deque (runtime only — NOT persisted).
        self._inboxes: dict[str, deque[InboxMessage]] = {}
        # sub_id → wake event, set whenever the inbox gains a message.
        self._wake_events: dict[str, asyncio.Event] = {}
        # owner_session_id → set of sub_ids (whole tree, incl. terminal).
        self._by_owner: dict[str, set[str]] = defaultdict(set)
        # dedup_key → sub_id for active subsessions — prevents
        # duplicate side-chats for the same known global issue.
        self._active_dedup_keys: dict[str, str] = {}
        # ticket_id -> set of sub_ids for WAIT_FOR_EVENT subsessions
        # currently blocked in the event wait loop.
        self._event_waiters: dict[str, set[str]] = defaultdict(set)

        # Extracted collaborators.
        self._store = RegistryStore(
            store_path, self._subs, self._inboxes, self._wake_events, self._by_owner
        )
        self._index = RegistryIndex(self._subs, self._by_owner, self._running, self)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def persist(self) -> None:
        """Write the full registry state to the JSON store.

        See :meth:`RegistryStore.persist`.
        """
        self._store.persist()

    def create(
        self,
        *,
        kind: SubsessionKind,
        owner_session_id: str,
        parent_id: str | None,
        depth: int,
        title: str,
        prompt: str,
        model_level: int,
        interval_seconds: float | None = None,
        include_previous_result: bool = False,
        max_runs: int | None = None,
        sub_id: str | None = None,
        runs: int = 0,
        completed_runs: set[int] | None = None,
        turn_history: list[tuple[str, str]] | None = None,
        checkpoint: dict[str, object] | None = None,
        dedup_key: str | None = None,
        retry_count: int = 0,
        event_timeout_seconds: float | None = None,
    ) -> SubsessionInfo:
        """Register a new subsession and publish ``subsession_started``.

        *sub_id* lets the resume path re-register a persisted subsession
        under its original id.  Idempotent: when *sub_id* is given and
        already registered the existing record is returned unchanged and
        no frame is published — the caller must not launch a duplicate
        worker.

        *runs* and *completed_runs* seed the run counter and run guard
        for periodic subsessions resumed after a restart, so already-
        executed run numbers are persisted atomically from the first
        write and the worker's ``runs + 1`` lands on the first
        unexecuted run instead of replaying (and skip-sleeping through)
        every historical one. *turn_history* seeds the agent-visible
        replay window the same way, so a resumed periodic worker picks
        up with the context it had before the restart instead of
        starting blank.  *checkpoint* seeds task-specific state (e.g.
        monitored ticket id and last-known state) so recovery can
        decide whether to resume the monitoring loop or close.
        """
        if sub_id is not None and sub_id in self._subs:
            return self._subs[sub_id]
        now = self._clock()
        resolved_id = sub_id or self._id_factory()
        # Run dedup checks BEFORE inserting the entry so a failure
        # (e.g. self-match on a resumed periodic with
        # dedup_key == checkpoint.ticket_id) does not leave a
        # half-registered RUNNING entry with no worker task attached.
        if dedup_key is not None:
            existing_id = self._active_dedup_keys.get(dedup_key)
            if existing_id is not None:
                existing_info = self._subs.get(existing_id)
                if existing_info is not None and existing_info.is_active:
                    raise SubsessionDedupError(existing_id)
                # Stale entry — clean up proactively.
                self._active_dedup_keys.pop(dedup_key, None)
            # Cross-reference: a PERIODIC subsession may have been
            # created without a dedup_key but recorded the watched
            # ticket_id in its checkpoint after the first run.
            if kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT):
                cp_match = self.find_active_periodic_by_ticket_id(dedup_key)
                if cp_match is not None:
                    raise SubsessionDedupError(cp_match)
        info = SubsessionInfo(
            id=resolved_id,
            kind=kind,
            owner_session_id=owner_session_id,
            parent_id=parent_id,
            depth=depth,
            title=title,
            prompt=prompt,
            model_level=model_level,
            status=SubsessionStatus.RUNNING,
            created_at=now,
            last_activity_at=now,
            interval_seconds=interval_seconds,
            include_previous_result=include_previous_result,
            max_runs=max_runs,
            runs=runs,
            completed_runs=completed_runs or set(),
            turn_history=turn_history or [],
            checkpoint=checkpoint,
            dedup_key=dedup_key,
            retry_count=retry_count,
            event_timeout_seconds=event_timeout_seconds,
        )
        self._subs[info.id] = info
        self._inboxes[info.id] = deque()
        self._wake_events[info.id] = asyncio.Event()
        self._by_owner[owner_session_id].add(info.id)
        if dedup_key is not None:
            self._active_dedup_keys[dedup_key] = info.id
        self._store.prune_terminal()
        self._publish(owner_session_id, subsession_started_frame(info.snapshot()))
        self._store.persist()
        return info

    def attach_task(self, sub_id: str, task: asyncio.Task[None]) -> None:
        """Hold a strong reference to *task* until it completes."""
        self._running[sub_id] = task
        task.add_done_callback(lambda _t: self._running.pop(sub_id, None))

    def restore(self, info: SubsessionInfo) -> None:
        """Re-register a persisted record without publishing or persisting.

        Used by the startup resume hook to rebuild terminal history and to
        stage interrupted entries before their terminal transition.  No-op
        when the id is already registered.
        """
        if info.id in self._subs:
            return
        self._subs[info.id] = info
        self._inboxes[info.id] = deque()
        self._wake_events[info.id] = asyncio.Event()
        self._by_owner[info.owner_session_id].add(info.id)

    def restore_inbox(self, sub_id: str, messages: list[InboxMessage]) -> None:
        """Restore undelivered inbox messages persisted before a restart.

        Called by the startup resume hook immediately after a resumed
        subsession is re-registered/spawned.  Populates the deque and sets
        the wake event so the worker drains the restored messages on its
        next turn boundary, then persists so the on-disk inbox matches the
        in-memory state (``create``/``restore`` wrote an empty deque first).
        """
        if not messages:
            return
        inbox = self._inboxes.get(sub_id)
        if inbox is None:
            return
        inbox.extend(messages)
        event = self._wake_events.get(sub_id)
        if event is not None:
            event.set()
        self._store.persist()

    def set_status(
        self,
        sub_id: str,
        status: SubsessionStatus,
        *,
        runs: int | None = None,
        next_run_at: float | None = None,
        last_result: str | None = None,
    ) -> None:
        """Mutate scheduling state and publish ``subsession_updated``.

        Keyword fields left at ``None`` are not touched.  No-op for
        unknown or already-terminal subsessions (guards the race between
        an external close and the worker's own bookkeeping).
        """
        info = self._subs.get(sub_id)
        if info is None or (not info.is_active and status in ACTIVE_STATUSES):
            return
        info.status = status
        info.last_activity_at = self._clock()
        if runs is not None:
            info.runs = runs
        if next_run_at is not None:
            info.next_run_at = next_run_at
        if last_result is not None:
            info.last_result = last_result
        self._publish(
            info.owner_session_id,
            subsession_updated_frame(
                info.id,
                info.status.value,
                runs=info.runs,
                next_run_at=info.next_run_at,
                last_activity_at=info.last_activity_at,
                last_result=info.last_result,
            ),
        )
        self._store.persist()

    def append_transcript(self, sub_id: str, role: str, text: str) -> None:
        """Append one transcript entry, capped, and publish it as a frame."""
        info = self._subs.get(sub_id)
        if info is None:
            return
        now = self._clock()
        info.transcript.append(TranscriptEntry(role=role, text=text, timestamp=now))
        if len(info.transcript) > self._transcript_max_entries:
            del info.transcript[: -self._transcript_max_entries]
        info.last_activity_at = now
        self._publish(
            info.owner_session_id,
            subsession_message_frame(info.id, role, text, now),
        )
        self._store.persist()

    def append_turn_history(self, sub_id: str, turn_input: str, reply: str) -> None:
        """Record one (turn_input, reply) pair for context-on-resume, capped."""
        info = self._subs.get(sub_id)
        if info is None:
            return
        info.turn_history.append((turn_input, reply))
        if len(info.turn_history) > _MAX_TURN_HISTORY_ENTRIES:
            del info.turn_history[:-_MAX_TURN_HISTORY_ENTRIES]
        self._store.persist()

    def enqueue_message(self, sub_id: str, role: str, text: str) -> bool:
        """Queue a message for the subsession's next turn boundary.

        Returns ``False`` when the subsession is unknown or no longer
        active.  The message is transcripted (and SSE-echoed) immediately
        so the sender sees it before the agent replies; the worker is
        woken via the inbox event.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return False
        inbox = self._inboxes.get(sub_id)
        if inbox is None:
            return False
        inbox.append(InboxMessage(role=role, text=text, timestamp=self._clock()))
        logger.info(
            "Subsession %s: enqueued inbox message (role=%s) — inbox size=%d.",
            sub_id,
            role,
            len(inbox),
        )
        self.append_transcript(sub_id, role, text)
        event = self._wake_events.get(sub_id)
        if event is not None:
            event.set()
        return True

    def drain_inbox(self, sub_id: str) -> list[InboxMessage]:
        """Return and clear all queued inbox messages; reset the wake event."""
        inbox = self._inboxes.get(sub_id)
        event = self._wake_events.get(sub_id)
        if event is not None:
            event.clear()
        if not inbox:
            return []
        messages = list(inbox)
        inbox.clear()
        # Persist immediately so a restart after the drain does not
        # re-deliver messages that were already handed to the worker.
        self._store.persist()
        logger.info(
            "Subsession %s: drained %d inbox message(s) — inbox size=0.",
            sub_id,
            len(messages),
        )
        return messages

    async def wait_for_inbox(self, sub_id: str, timeout: float | None) -> bool:
        """Wait until the inbox gains a message or *timeout* elapses.

        Returns ``True`` when woken by a message, ``False`` on timeout.
        Cancellable — the worker relies on plain task cancellation for
        external closes.
        """
        event = self._wake_events.get(sub_id)
        if event is None:
            return False
        if timeout is None:
            await event.wait()
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def _close_and_publish(
        self,
        info: SubsessionInfo,
        *,
        status: SubsessionStatus,
        summary: str,
        reason: str = "",
        closed_by: str = "agent",
        error: str | None = None,
    ) -> SubsessionInfo:
        """Set terminal state, publish frame, persist, and return *info*."""
        info.status = status
        info.summary = summary
        info.last_activity_at = self._clock()

        # Clean up the dedup key so a new side-chat for the same issue can
        # be spawned after this one closes.
        if info.dedup_key is not None:
            self._active_dedup_keys.pop(info.dedup_key, None)

        if status is SubsessionStatus.FAILED:
            info.error = error
            frame = subsession_failed_frame(
                info.id,
                kind=info.kind.value,
                title=info.title,
                error=error,  # type: ignore[arg-type]  # non-None when FAILED
                summary=summary,
                parent_id=info.parent_id,
            )
        else:
            info.close_reason = reason
            frame = subsession_closed_frame(
                info.id,
                kind=info.kind.value,
                title=info.title,
                reason=reason,
                summary=summary,
                closed_by=closed_by,
                parent_id=info.parent_id,
            )

        self._publish(info.owner_session_id, frame)
        self._store.persist()
        return info

    def mark_closed(
        self, sub_id: str, *, summary: str, reason: str, closed_by: str = "agent"
    ) -> SubsessionInfo | None:
        """Set terminal ``CLOSED`` state and publish ``subsession_closed``.

        The worker's own clean-close path — does NOT cancel the task.
        No-op (returns ``None``) when the subsession is unknown or already
        terminal, so an external close racing the worker wins exactly once.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return None
        return self._close_and_publish(
            info,
            status=SubsessionStatus.CLOSED,
            summary=summary,
            reason=reason,
            closed_by=closed_by,
        )

    def mark_paused(
        self, sub_id: str, *, summary: str, reason: str = "paused"
    ) -> SubsessionInfo | None:
        """Transition a live periodic subsession to ``PAUSED``.

        Sets ``PAUSED`` status and publishes a ``subsession_updated``
        frame — the worker stays alive and blocks on a resume signal
        instead of terminating.  No-op (returns ``None``) when the
        subsession is unknown or already terminal.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return None
        if info.status in (SubsessionStatus.CLOSED, SubsessionStatus.FAILED):
            return None
        info.status = SubsessionStatus.PAUSED
        info.summary = summary
        info.close_reason = reason
        info.last_activity_at = self._clock()
        self._publish(
            info.owner_session_id,
            subsession_updated_frame(
                sub_id,
                status="paused",
                runs=info.runs,
                last_activity_at=info.last_activity_at,
                last_result=info.last_result,
            ),
        )
        self._store.persist()
        return info

    def resume(self, sub_id: str) -> SubsessionInfo | None:
        """Resume a paused periodic subsession — PAUSED → RUNNING.

        Clears *close_reason* and *summary*, publishes an update frame,
        and persists.  Returns ``None`` when the subsession is unknown or
        not currently ``PAUSED``.
        """
        info = self._subs.get(sub_id)
        if info is None:
            return None
        if info.status is not SubsessionStatus.PAUSED:
            return None
        info.status = SubsessionStatus.RUNNING
        info.last_activity_at = self._clock()
        info.close_reason = None
        info.summary = None
        # Reset the human_approval_since timestamp so the resumed
        # monitor does not immediately time out again.
        if info.checkpoint is not None:
            info.checkpoint.pop("human_approval_since", None)
        self._publish(
            info.owner_session_id,
            subsession_updated_frame(
                sub_id,
                status="running",
                runs=info.runs,
                last_activity_at=info.last_activity_at,
                last_result=info.last_result,
            ),
        )
        self._store.persist()
        return info

    def reopen(self, sub_id: str) -> SubsessionInfo | None:
        """Reopen a terminal periodic subsession.

        Accepts records whose status is ``CLOSED`` or ``PAUSED``, kind is
        ``PERIODIC``, and ``close_reason`` is ``"paused"``,
        ``"human_approval_timeout"``, ``"pre_authorized_approval"``, or
        ``"max_runs"``.
        Other records are left untouched.  Returns the updated record or
        ``None`` when the subsession is unknown, not in a reopenable
        state, or already active (excluding PAUSED).
        """
        info = self._subs.get(sub_id)
        if info is None:
            return None
        # PAUSED subsessions are "active" but their worker may be dead
        # after a restart — allow reopen for them.
        if info.is_active and info.status is not SubsessionStatus.PAUSED:
            return None
        if (
            info.status not in (SubsessionStatus.CLOSED, SubsessionStatus.PAUSED)
            or info.kind is not SubsessionKind.PERIODIC
            or info.close_reason
            not in (
                "paused",
                "human_approval_timeout",
                "pre_authorized_approval",
                "max_runs",
            )
        ):
            return None
        info.status = SubsessionStatus.RUNNING
        info.last_activity_at = self._clock()
        # When reopening a max_runs-exhausted monitor, reset the run
        # counter so the monitor gets a fresh budget rather than
        # immediately hitting the limit again.  Capture the reason
        # before we clear it below.
        _was_max_runs = info.close_reason == "max_runs"
        _has_escalation_count = (
            info.checkpoint is not None
            and info.checkpoint.get("max_runs_exhausted_count") is not None
        )
        info.close_reason = None
        info.summary = None
        if _was_max_runs or _has_escalation_count:
            info.runs = 0
        # Reset the human_approval_since timestamp so the reopened
        # monitor does not immediately time out again.
        if info.checkpoint is not None:
            info.checkpoint.pop("human_approval_since", None)
        self._publish(
            info.owner_session_id,
            subsession_updated_frame(
                sub_id,
                status="running",
                runs=info.runs,
                last_activity_at=info.last_activity_at,
                last_result=info.last_result,
            ),
        )
        self._store.persist()
        return info

    def find_paused_periodic(self) -> list[SubsessionInfo]:
        """Return every paused periodic subsession waiting for a state change.

        Includes monitors in ``PAUSED`` status (auto-paused by
        ``max_idle_runs`` — worker is alive, waiting on an inbox signal),
        and monitors closed with reason ``"paused"``,
        ``"human_approval_timeout"``, ``"pre_authorized_approval"``, or
        ``"max_runs"``
        (legacy records from before the ``PAUSED`` status existed).
        All are waiting for a ticket-state change — typically a PR merge
        or an operator action — before they can safely resume.
        """
        result: list[SubsessionInfo] = []
        for info in self._subs.values():
            if info.kind is not SubsessionKind.PERIODIC:
                continue
            if info.status is SubsessionStatus.PAUSED or (
                info.status is SubsessionStatus.CLOSED
                and info.close_reason
                in (
                    "paused",
                    "human_approval_timeout",
                    "pre_authorized_approval",
                    "max_runs",
                )
            ):
                result.append(info)
        return result

    def find_paused_periodic_by_ticket_id(self, ticket_id: str) -> list[SubsessionInfo]:
        """Return live ``PAUSED`` periodic monitors tracking *ticket_id*.

        Only live workers are returned (``PAUSED`` status — the worker is
        alive and blocking on an inbox signal).  Legacy closed records
        with a paused-style close reason are handled by the background
        watcher's reopen path, not by inbox wake.
        """
        result: list[SubsessionInfo] = []
        for info in self._subs.values():
            if info.kind is not SubsessionKind.PERIODIC:
                continue
            if info.status is not SubsessionStatus.PAUSED:
                continue
            cp = info.checkpoint
            if cp is None:
                continue
            cp_ticket_id = cp.get("ticket_id")
            if isinstance(cp_ticket_id, str) and cp_ticket_id == ticket_id:
                result.append(info)
        return result

    def cancel_and_close(
        self, sub_id: str, *, reason: str, closed_by: str
    ) -> SubsessionInfo | None:
        """Externally close a live subsession: cancel its worker, mark CLOSED.

        Builds a best-effort summary from the last assistant transcript
        entry.  Returns the closed record (so the caller can deliver the
        summary to the parent) or ``None`` when unknown / already terminal.
        Idempotent: a second call returns ``None``.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return None
        # Cancel FIRST so the worker cannot race us into mark_closed /
        # fail while we build the terminal state.
        task = self._running.get(sub_id)
        if task is not None and not task.done():
            task.cancel()
        last = self.last_assistant_text(info)
        summary = f"{reason.capitalize()}."
        if last:
            summary += f" Last state: {_truncate(last, 500)}"
        return self._close_and_publish(
            info,
            status=SubsessionStatus.CLOSED,
            summary=summary,
            reason=reason,
            closed_by=closed_by,
        )

    def fail(self, sub_id: str, *, error: str) -> SubsessionInfo | None:
        """Set terminal ``FAILED`` state and publish ``subsession_failed``.

        Returns the failed record, or ``None`` when unknown / already
        terminal (e.g. an external close landed first).
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return None
        last = self.last_assistant_text(info)
        summary = f"Failed: {_truncate(error, 300)}"
        if last:
            summary += f" Last state: {_truncate(last, 500)}"
        return self._close_and_publish(
            info,
            status=SubsessionStatus.FAILED,
            summary=summary,
            error=error,
        )

    def mark_interrupted(self, sub_id: str, *, summary: str) -> SubsessionInfo | None:
        """Set terminal ``INTERRUPTED`` state (startup resume path).

        Published as a ``subsession_closed`` frame with ``closed_by=
        "system"`` — the UI treats it like any other terminal close.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return None
        return self._close_and_publish(
            info,
            status=SubsessionStatus.INTERRUPTED,
            summary=summary,
            reason="interrupted",
            closed_by="system",
        )

    # ------------------------------------------------------------------
    # delegation: persistence
    # ------------------------------------------------------------------

    def load_persisted(self) -> list[dict[str, object]]:
        """Read raw persisted entries for the startup resume hook."""
        return self._store.load_persisted()

    # ------------------------------------------------------------------
    # delegation: index / queries
    # ------------------------------------------------------------------

    def list_for_owner(self, owner_session_id: str) -> list[SubsessionInfo]:
        """Return the whole subsession tree for an owner, oldest first."""
        return self._index.list_for_owner(owner_session_id)

    def list_descendants(self, root_id: str) -> list[SubsessionInfo]:
        """Return every (transitive) child of subsession *root_id*."""
        return self._index.list_descendants(root_id)

    def reap_orphans(self) -> int:
        """Cancel any timer whose subsession id is not in a conversation tree."""
        return self._index.reap_orphans()

    def close_all_for_owner(self, owner_session_id: str, *, reason: str) -> int:
        """Close every active subsession owned by *owner_session_id*."""
        return self._index.close_all_for_owner(owner_session_id, reason=reason)

    def reassign_owner(
        self, old_owner_session_id: str, new_owner_session_id: str
    ) -> int:
        """Move every subsession owned by *old_owner_session_id* to the new owner."""
        return self._index.reassign_owner(old_owner_session_id, new_owner_session_id)

    # ------------------------------------------------------------------
    # core queries (retained on registry)
    # ------------------------------------------------------------------

    def now(self) -> float:
        """Return the registry's wall-clock reading (test-injectable)."""
        return self._clock()

    def get(self, sub_id: str) -> SubsessionInfo | None:
        """Return the record for *sub_id*, or ``None``."""
        return self._subs.get(sub_id)

    def list_all(self) -> list[SubsessionInfo]:
        """Return every registered subsession (all owners), oldest first."""
        return sorted(self._subs.values(), key=lambda i: i.created_at)

    def count_active(self) -> int:
        """Return the number of active subsessions process-wide.

        PAUSED subsessions are excluded — their workers are alive but
        idle, waiting on a resume signal, and should not count against
        the concurrency cap.
        """
        return sum(
            1
            for info in self._subs.values()
            if info.is_active and info.status is not SubsessionStatus.PAUSED
        )

    def count_active_for_owner(self, owner_session_id: str) -> int:
        """Return the number of active subsessions owned by *owner_session_id*.

        Uses the same exclusion rules as :meth:`count_active`: PAUSED
        subsessions do not count against the concurrency cap.
        """
        sub_ids = self._by_owner.get(owner_session_id, ())
        return sum(
            1
            for sub_id in sub_ids
            if (info := self._subs.get(sub_id)) is not None
            and info.is_active
            and info.status is not SubsessionStatus.PAUSED
        )

    _RECLAIMABLE_STATUSES: frozenset[SubsessionStatus] = frozenset(
        {
            SubsessionStatus.SLEEPING,
            SubsessionStatus.PAUSED,
        }
    )

    def find_stale_for_reclaim(
        self, *, exclude_owner: str, stale_seconds: float
    ) -> SubsessionInfo | None:
        """Return the best candidate for stale-subsession reclamation.

        A candidate must belong to an owner other than *exclude_owner*,
        be in a reclaimable status (SLEEPING or PAUSED), and have
        ``last_activity_at`` older than ``now() - stale_seconds``.
        SLEEPING subsessions are preferred over PAUSED because they
        count against the global capacity cap; reclaiming one actually
        frees a slot.

        Returns ``None`` when no eligible subsession exists.
        """
        now = self.now()
        cutoff = now - stale_seconds
        best: SubsessionInfo | None = None
        best_score = -1  # 2 = sleeping, 1 = paused

        for owner_id, sub_ids in self._by_owner.items():
            if owner_id == exclude_owner:
                continue
            for sub_id in sub_ids:
                info = self._subs.get(sub_id)
                if info is None:
                    continue
                if not info.is_active or info.last_activity_at > cutoff:
                    continue
                if info.status not in self._RECLAIMABLE_STATUSES:
                    continue
                score = 2 if info.status is SubsessionStatus.SLEEPING else 1
                if score > best_score or (
                    score == best_score
                    and (best is None or info.last_activity_at < best.last_activity_at)
                ):
                    best = info
                    best_score = score
        return best

    def claim_run(self, sub_id: str, run_n: int) -> bool:
        """Atomically claim a periodic run number.

        Returns ``True`` when *run_n* was claimed (not previously
        executed); ``False`` when it was already completed — the caller
        must skip the agent turn.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return False
        if run_n in info.completed_runs:
            return False
        info.completed_runs.add(run_n)
        self._store.persist()
        return True

    def update_checkpoint(
        self, sub_id: str, checkpoint: dict[str, object] | None
    ) -> bool:
        """Replace the checkpoint data for *sub_id* and persist.

        For ``WAIT_FOR_EVENT`` subsessions the ``ticket_id`` key is
        system-owned: when the replacement drops it, it is recovered from
        the previous checkpoint (or the subsession's ``dedup_key``) so the
        monitor's event filter survives agent ``set_checkpoint`` calls and
        process restarts.

        For ``PERIODIC`` subsessions the ``auto_stop_no_change_runs``
        override is likewise system-owned: when the replacement drops it,
        it is recovered from the previous checkpoint so a long-lived
        monitor that records PR/state via ``set_checkpoint`` does not
        silently revert to the global auto-stop default.

        Returns ``True`` when the update was applied; ``False`` when the
        subsession is unknown (including already-terminal).
        """
        info = self._subs.get(sub_id)
        if info is None:
            return False
        if info.kind is SubsessionKind.WAIT_FOR_EVENT:
            checkpoint = _preserve_event_ticket_id(info, checkpoint)
        elif info.kind is SubsessionKind.PERIODIC:
            checkpoint = _preserve_periodic_auto_stop_no_change_runs(info, checkpoint)
            checkpoint = _preserve_periodic_no_change_pause_count(info, checkpoint)
            checkpoint = _preserve_periodic_progress_flags(info, checkpoint)
        info.checkpoint = checkpoint
        self._store.persist()
        return True

    def update_periodic_config(
        self,
        sub_id: str,
        *,
        prompt: str | None = None,
        interval_seconds: float | None = None,
        max_runs: int | None = None,
    ) -> bool:
        """Update the run configuration of an active periodic subsession.

        Only *prompt* (instructions), *interval_seconds*, and *max_runs*
        are accepted — the run counter is never reset, so self-update
        cannot bypass max-run limits.  Fields left at ``None`` are not
        touched.

        Returns ``True`` when the update was applied; ``False`` when the
        subsession is unknown or not an active periodic.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return False
        if info.kind is not SubsessionKind.PERIODIC:
            return False
        if prompt is not None:
            info.prompt = prompt
        if interval_seconds is not None:
            info.interval_seconds = interval_seconds
        if max_runs is not None:
            info.max_runs = max_runs
        self._store.persist()
        return True

    def is_dedup_key_active(self, dedup_key: str) -> str | None:
        """Return the active subsession id for *dedup_key*, or ``None``.

        Subsessions of any kind with a matching dedup_key are tracked.
        Returns ``None`` when the key is unknown or the tracked subsession
        has become terminal (the close/fail path cleans up proactively,
        but this is a safety net for races).
        """
        sub_id = self._active_dedup_keys.get(dedup_key)
        if sub_id is None:
            return None
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            # Stale entry — clean up proactively.
            self._active_dedup_keys.pop(dedup_key, None)
            return None
        return sub_id

    def find_active_periodic_by_ticket_id(self, ticket_id: str) -> str | None:
        """Return the id of an active PERIODIC or WAIT_FOR_EVENT.

        subsession whose checkpoint carries *ticket_id*.

        Returns ``None`` when no match is found.

        This is a cross-reference complement to
        ``is_dedup_key_active``: a monitor may have been
        spawned without a dedup_key (the agent forgot), but
        after its first run the checkpoint records the
        watched ``ticket_id``.  When a new spawn arrives
        WITH a dedup_key, this method catches the match
        that ``is_dedup_key_active`` misses because the
        original dedup_key was never set.
        """
        for info in self._subs.values():
            if (
                info.kind
                not in (
                    SubsessionKind.PERIODIC,
                    SubsessionKind.WAIT_FOR_EVENT,
                )
                or not info.is_active
            ):
                continue
            cp = info.checkpoint
            if cp is None:
                continue
            cp_ticket_id = cp.get("ticket_id")
            if isinstance(cp_ticket_id, str) and cp_ticket_id == ticket_id:
                return info.id
        return None

    def register_event_waiter(self, sub_id: str, ticket_id: str) -> None:
        """Register *sub_id* as waiting for events on *ticket_id*."""
        self._event_waiters[ticket_id].add(sub_id)

    def unregister_event_waiter(self, sub_id: str, ticket_id: str) -> None:
        """Remove *sub_id* from the event-waiter set for *ticket_id*."""
        waiters = self._event_waiters.get(ticket_id)
        if waiters is not None:
            waiters.discard(sub_id)
            if not waiters:
                del self._event_waiters[ticket_id]

    def route_mill_event(self, ticket_id: str, event_payload: dict[str, object]) -> int:
        """Route an incoming mill state-change event to waiting monitors.

        Wakes both ``WAIT_FOR_EVENT`` monitors registered as event waiters
        and live ``PAUSED`` periodic monitors tracking the same ticket
        (auto-paused monitors whose worker is blocking on the inbox).
        """
        old_state = event_payload.get("old_state", "")
        new_state = event_payload.get("new_state", "")
        timestamp = event_payload.get("timestamp", "")
        event_text = (
            f"Mill event: ticket {ticket_id} state changed from "
            f"'{old_state}' to '{new_state}' at {timestamp}. "
            f"This event was pushed from the mill — you MUST verify "
            f"the current state via a live GET of the ticket API "
            f"before acting on it."
        )

        woken = 0
        waiters = self._event_waiters.get(ticket_id)
        if waiters:
            for sub_id in list(waiters):
                if self.enqueue_message(sub_id, "system", event_text):
                    woken += 1
                else:
                    waiters.discard(sub_id)

            if not waiters:
                del self._event_waiters[ticket_id]

        # Auto-paused periodic monitors do not register as event waiters —
        # their worker blocks in ``_paused_wait_loop`` on the same inbox
        # wake mechanism, so enqueueing the event here re-arms them.
        for info in self.find_paused_periodic_by_ticket_id(ticket_id):
            if self.enqueue_message(info.id, "system", event_text):
                woken += 1

        if woken:
            logger.info(
                "Routed mill event for ticket %s to %d monitor(s).",
                ticket_id,
                woken,
            )
        else:
            logger.debug(
                "Mill event for ticket %s had no active waiters — ignored.",
                ticket_id,
            )

        return woken

    def is_duplicate_ticket_terminal(self, ticket_id: str, exclude_sub_id: str) -> bool:
        """Check for a duplicate terminal report for *ticket_id*.

        Returns ``True`` when a different (already-CLOSED) subsession has
        already reported the same ticket as terminal — when a prior monitor
        has already reported the ticket as closed/done, a second monitor's
        completion notice for that ticket is a duplicate and should be
        suppressed to avoid a redundant (and often verbose) reaction turn
        in the parent conversation.
        """
        for info in self._subs.values():
            if info.id == exclude_sub_id:
                continue
            if info.status is not SubsessionStatus.CLOSED:
                continue
            cp = info.checkpoint
            if cp is None:
                continue
            cp_ticket_id = cp.get("ticket_id")
            if not isinstance(cp_ticket_id, str) or cp_ticket_id != ticket_id:
                continue
            # Only count a prior subsession as a terminal reporter when
            # its close_reason indicates it actually reported the terminal
            # state (not a non-terminal close like pause / max_runs).
            if info.close_reason in ("ticket_terminal", "completed"):
                return True
        return False

    def is_duplicate_auto_pause(self, ticket_id: str, exclude_sub_id: str) -> bool:
        """Check for a duplicate auto-pause / no-change report for *ticket_id*.

        Returns ``True`` when a different (already-PAUSED or CLOSED)
        subsession has already reported the same ticket as auto-paused,
        auto-stopped, or terminal — when a prior monitor has already
        notified the user that the ticket needs no attention, a second
        monitor's no-change notice for that ticket is a duplicate and
        should be suppressed to avoid a redundant (and often noisy)
        reaction turn in the parent conversation.
        """
        for info in self._subs.values():
            if info.id == exclude_sub_id:
                continue
            if info.status not in (
                SubsessionStatus.CLOSED,
                SubsessionStatus.PAUSED,
            ):
                continue
            cp = info.checkpoint
            if cp is None:
                continue
            cp_ticket_id = cp.get("ticket_id")
            if not isinstance(cp_ticket_id, str) or cp_ticket_id != ticket_id:
                continue
            if info.close_reason in (
                "paused",
                "no_change_auto_stop",
                "ticket_terminal",
                "completed",
            ):
                return True
        return False

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _publish(self, owner_session_id: str, frame: dict[str, object]) -> None:
        """Publish *frame* to the owning UI session (no-op without a sink)."""
        if self._event_sink is not None:
            self._event_sink.publish(owner_session_id, frame)

    @staticmethod
    def last_assistant_text(info: SubsessionInfo) -> str:
        """Return the most recent assistant transcript text, or ``""``."""
        for entry in reversed(info.transcript):
            if entry.role == "assistant":
                return entry.text
        return ""

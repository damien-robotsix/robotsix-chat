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
import logging
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path

from robotsix_chat.chat.events import (
    EventSink,
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
from .registry_checkpoint import (
    TICKET_ID_RE,
    _checkpoint_auto_stop_no_change_runs,
    _checkpoint_no_change_pause_count,
    _checkpoint_ticket_id,
    _preserve_event_ticket_id,
    _preserve_periodic_auto_stop_no_change_runs,
    _preserve_periodic_no_change_pause_count,
    _preserve_periodic_progress_flags,
    _truncate,
    user_chat_ticket_id,
)
from .registry_constants import (
    _MAX_TURN_HISTORY_ENTRIES,
    _MONITOR_KINDS,
    _TICKET_TERMINAL_CLOSE_REASONS,
    DECISION_GATE_STATES,
    OWNER_CLOSED_REASON,
)
from .registry_index import RegistryIndex
from .registry_lifecycle import RegistryLifecycle
from .registry_store import _MAX_TERMINAL_ENTRIES, RegistryStore
from .registry_tickets import RegistryTickets

logger = logging.getLogger(__name__)

__all__ = [
    "DECISION_GATE_STATES",
    "OWNER_CLOSED_REASON",
    "TICKET_ID_RE",
    "_MAX_TERMINAL_ENTRIES",
    "_MAX_TURN_HISTORY_ENTRIES",
    "_MONITOR_KINDS",
    "_TICKET_TERMINAL_CLOSE_REASONS",
    "RegistryIndex",
    "RegistryLifecycle",
    "RegistryStore",
    "RegistryTickets",
    "SubsessionRegistry",
    "_checkpoint_auto_stop_no_change_runs",
    "_checkpoint_no_change_pause_count",
    "_checkpoint_ticket_id",
    "_preserve_event_ticket_id",
    "_preserve_periodic_auto_stop_no_change_runs",
    "_preserve_periodic_no_change_pause_count",
    "_preserve_periodic_progress_flags",
    "_truncate",
    "user_chat_ticket_id",
]


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
        # sub_id → inbox deque of queued operator/steering messages.
        self._inboxes: dict[str, deque[InboxMessage]] = {}
        # sub_id → drained inbox messages awaiting turn completion.  These
        # are persisted so a restart between ``drain_inbox`` and the
        # completed turn does not lose the operator's message.
        self._in_flight: dict[str, list[InboxMessage]] = {}
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
            store_path,
            self._subs,
            self._inboxes,
            self._in_flight,
            self._wake_events,
            self._by_owner,
        )
        self._index = RegistryIndex(self._subs, self._by_owner, self._running, self)

        # Observers notified after every terminal transition (a monitor
        # closed/failed/interrupted — i.e. a slot freed).  The
        # slot-budget manager registers here to drain pending queues.
        self._close_callbacks: list[Callable[[SubsessionInfo], None]] = []

        self._lifecycle = RegistryLifecycle(
            subs=self._subs,
            running=self._running,
            inboxes=self._inboxes,
            in_flight=self._in_flight,
            active_dedup_keys=self._active_dedup_keys,
            close_callbacks=self._close_callbacks,
            store=self._store,
            registry=self,
        )
        self._tickets = RegistryTickets(
            subs=self._subs,
            active_dedup_keys=self._active_dedup_keys,
            event_waiters=self._event_waiters,
            registry=self,
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def add_close_callback(self, callback: Callable[[SubsessionInfo], None]) -> None:
        """Register *callback* to run after every terminal transition.

        The callback receives the closed record.  Used by the
        slot-budget manager to drain per-conversation pending queues
        when a monitor terminates and its slot frees.
        """
        self._close_callbacks.append(callback)

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
        anchor_time: str | None = None,
        include_previous_result: bool = False,
        max_runs: int | None = None,
        sub_id: str | None = None,
        runs: int = 0,
        completed_runs: set[int] | None = None,
        turn_history: list[tuple[str, str]] | None = None,
        checkpoint: dict[str, object] | None = None,
        dedup_key: str | None = None,
        depends_on_ticket_id: str | None = None,
        retry_count: int = 0,
        event_timeout_seconds: float | None = None,
        run_timeout_seconds: float | None = None,
        inbox: list[InboxMessage] | None = None,
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
        *inbox* seeds queued inbox messages (re-enqueued on resume) and
        wakes the inbox event so they are drained at the next turn
        boundary.
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
            anchor_time=anchor_time,
            include_previous_result=include_previous_result,
            max_runs=max_runs,
            runs=runs,
            completed_runs=completed_runs or set(),
            turn_history=turn_history or [],
            checkpoint=checkpoint,
            dedup_key=dedup_key,
            depends_on_ticket_id=depends_on_ticket_id,
            retry_count=retry_count,
            event_timeout_seconds=event_timeout_seconds,
            run_timeout_seconds=run_timeout_seconds,
        )
        self._subs[info.id] = info
        self._inboxes[info.id] = deque(inbox) if inbox else deque()
        self._wake_events[info.id] = asyncio.Event()
        if inbox:
            self._wake_events[info.id].set()
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

    def restore_transcript(self, sub_id: str, entries: list[TranscriptEntry]) -> None:
        """Restore a resumed subsession's persisted transcript after a restart.

        Called by the startup resume hook immediately after a resumed
        subsession is re-registered/spawned.  ``create`` starts every entry
        with an empty transcript, so a resumed ``user_chat`` still waiting
        for the operator would otherwise render an empty box ("No messages
        yet.") even though its pending question was persisted before the
        restart.  Re-seeding the transcript from durable storage — and
        persisting it — keeps the outstanding question visible and lets it
        survive every subsequent restart.

        Idempotent: only seeds when the live transcript is empty, so it never
        duplicates entries already present.  Does not publish SSE frames (a
        startup restore has no live listener; the operator's UI reads the
        transcript from the detail snapshot when it reconnects).
        """
        if not entries:
            return
        info = self._subs.get(sub_id)
        if info is None or info.transcript:
            return
        info.transcript.extend(entries)
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
        # The drained inbox messages for this turn are now consumed —
        # clear them from the persisted in-flight buffer so a later
        # restart does not re-deliver already-processed messages.
        self._in_flight.pop(sub_id, None)
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
        """Return and clear all queued inbox messages; reset the wake event.

        The drained messages are moved into the persisted in-flight buffer
        (cleared by :meth:`append_turn_history` on turn completion) so a
        restart between ``drain_inbox`` and the completed turn does not
        lose the operator's message.
        """
        inbox = self._inboxes.get(sub_id)
        event = self._wake_events.get(sub_id)
        if event is not None:
            event.clear()
        if not inbox:
            return []
        messages = list(inbox)
        inbox.clear()
        self._in_flight.setdefault(sub_id, []).extend(messages)
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

    # ------------------------------------------------------------------
    # delegation: lifecycle transitions
    # ------------------------------------------------------------------

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
        return self._lifecycle._close_and_publish(
            info,
            status=status,
            summary=summary,
            reason=reason,
            closed_by=closed_by,
            error=error,
        )

    def mark_closed(
        self, sub_id: str, *, summary: str, reason: str, closed_by: str = "agent"
    ) -> SubsessionInfo | None:
        """Set terminal ``CLOSED`` state (see :meth:`RegistryLifecycle.mark_closed`)."""
        return self._lifecycle.mark_closed(
            sub_id, summary=summary, reason=reason, closed_by=closed_by
        )

    def mark_paused(
        self, sub_id: str, *, summary: str, reason: str = "paused"
    ) -> SubsessionInfo | None:
        """Transition a live periodic subsession to ``PAUSED``."""
        return self._lifecycle.mark_paused(sub_id, summary=summary, reason=reason)

    def resume(self, sub_id: str) -> SubsessionInfo | None:
        """Resume a paused periodic subsession — PAUSED → RUNNING."""
        return self._lifecycle.resume(sub_id)

    def reopen(self, sub_id: str) -> SubsessionInfo | None:
        """Reopen a terminal monitor subsession (see :class:`RegistryLifecycle`)."""
        return self._lifecycle.reopen(sub_id)

    def find_paused_periodic(self) -> list[SubsessionInfo]:
        """Return every paused monitor subsession waiting for a state change."""
        return self._lifecycle.find_paused_periodic()

    def find_paused_periodic_by_ticket_id(self, ticket_id: str) -> list[SubsessionInfo]:
        """Return live ``PAUSED`` monitor subsessions tracking *ticket_id*."""
        return self._lifecycle.find_paused_periodic_by_ticket_id(ticket_id)

    def cancel_and_close(
        self,
        sub_id: str,
        *,
        reason: str,
        closed_by: str = "system",
        summary: str | None = None,
    ) -> SubsessionInfo | None:
        """Externally close a live subsession: cancel its worker, mark CLOSED."""
        return self._lifecycle.cancel_and_close(
            sub_id, reason=reason, closed_by=closed_by, summary=summary
        )

    def fail(self, sub_id: str, *, error: str) -> SubsessionInfo | None:
        """Set terminal ``FAILED`` state and publish ``subsession_failed``."""
        return self._lifecycle.fail(sub_id, error=error)

    def mark_interrupted(self, sub_id: str, *, summary: str) -> SubsessionInfo | None:
        """Set terminal ``INTERRUPTED`` state (startup resume path)."""
        return self._lifecycle.mark_interrupted(sub_id, summary=summary)

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

    def reassign_live_user_chats(
        self, old_owner_session_id: str, new_owner_session_id: str
    ) -> int:
        """Move the live ``user_chat`` panels of *old* to *new* (see index)."""
        return self._index.reassign_live_user_chats(
            old_owner_session_id, new_owner_session_id
        )

    def count_active(self) -> int:
        """Return the number of active (non-PAUSED) subsessions process-wide."""
        return self._index.count_active()

    def count_active_for_owner(self, owner_session_id: str) -> int:
        """Return the number of active subsessions owned by *owner_session_id*."""
        return self._index.count_active_for_owner(owner_session_id)

    def count_occupied_for_owner(self, owner_session_id: str) -> int:
        """Return the number of occupied monitor slots for *owner_session_id*."""
        return self._index.count_occupied_for_owner(owner_session_id)

    def find_paused_for_reuse(self, owner_session_id: str) -> str | None:
        """Return the least-recently-active PAUSED monitor for *owner_session_id*."""
        return self._index.find_paused_for_reuse(owner_session_id)

    def find_stale_for_reclaim(
        self, *, exclude_owner: str, stale_seconds: float
    ) -> SubsessionInfo | None:
        """Return the best candidate for stale-subsession reclamation."""
        return self._index.find_stale_for_reclaim(
            exclude_owner=exclude_owner, stale_seconds=stale_seconds
        )

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

    def claim_run(self, sub_id: str, run_n: int) -> bool:
        """Atomically claim a periodic run number (see :class:`RegistryLifecycle`)."""
        return self._lifecycle.claim_run(sub_id, run_n)

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
        """Update the run configuration of an active periodic subsession."""
        return self._lifecycle.update_periodic_config(
            sub_id, prompt=prompt, interval_seconds=interval_seconds, max_runs=max_runs
        )

    # ------------------------------------------------------------------
    # delegation: ticket-scoped queries / mill events
    # ------------------------------------------------------------------

    def is_dedup_key_active(self, dedup_key: str) -> str | None:
        """Return the active subsession id for *dedup_key*, or ``None``."""
        return self._tickets.is_dedup_key_active(dedup_key)

    def find_active_user_chat_by_ticket_id(self, ticket_id: str) -> str | None:
        """Return the id of an active USER_CHAT panel about *ticket_id*, else None."""
        return self._tickets.find_active_user_chat_by_ticket_id(ticket_id)

    def close_user_chats_for_moved_ticket(
        self, ticket_id: str, old_state: str, new_state: str
    ) -> int:
        """Close open decision panels whose ticket left the human gates."""
        return self._tickets.close_user_chats_for_moved_ticket(
            ticket_id, old_state, new_state
        )

    def find_active_periodic_by_ticket_id(self, ticket_id: str) -> str | None:
        """Return the id of an active monitor whose checkpoint carries *ticket_id*."""
        return self._tickets.find_active_periodic_by_ticket_id(ticket_id)

    def register_event_waiter(self, sub_id: str, ticket_id: str) -> None:
        """Register *sub_id* as waiting for events on *ticket_id*."""
        self._tickets.register_event_waiter(sub_id, ticket_id)

    def unregister_event_waiter(self, sub_id: str, ticket_id: str) -> None:
        """Remove *sub_id* from the event-waiter set for *ticket_id*."""
        self._tickets.unregister_event_waiter(sub_id, ticket_id)

    def route_mill_event(self, ticket_id: str, event_payload: dict[str, object]) -> int:
        """Route an incoming mill state-change event to waiting monitors."""
        return self._tickets.route_mill_event(ticket_id, event_payload)

    def get_owner_session_ids_for_ticket(self, ticket_id: str) -> set[str]:
        """Return owner session ids for every active subsession tracking *ticket_id*."""
        return self._tickets.get_owner_session_ids_for_ticket(ticket_id)

    def is_duplicate_ticket_terminal(self, ticket_id: str, exclude_sub_id: str) -> bool:
        """Check for a duplicate terminal report for *ticket_id*."""
        return self._tickets.is_duplicate_ticket_terminal(ticket_id, exclude_sub_id)

    def has_terminal_report_for_ticket(self, ticket_id: str) -> bool:
        """Check whether a terminal monitor already reported *ticket_id*."""
        return self._tickets.has_terminal_report_for_ticket(ticket_id)

    def is_duplicate_auto_pause(self, ticket_id: str, exclude_sub_id: str) -> bool:
        """Check for a duplicate auto-pause / no-change report for *ticket_id*."""
        return self._tickets.is_duplicate_auto_pause(ticket_id, exclude_sub_id)

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

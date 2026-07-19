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
        entries = [info.snapshot(with_transcript=True) for info in self._subs.values()]
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
        # dedup_key → sub_id for active user_chat subsessions — prevents
        # duplicate side-chats for the same known global issue.
        self._active_dedup_keys: dict[str, str] = {}

        # Extracted collaborators.
        self._store = RegistryStore(
            store_path, self._subs, self._inboxes, self._wake_events, self._by_owner
        )
        self._index = RegistryIndex(self._subs, self._by_owner, self._running, self)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

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
        info = SubsessionInfo(
            id=sub_id or self._id_factory(),
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
        """Return the number of active subsessions process-wide."""
        return sum(1 for info in self._subs.values() if info.is_active)

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

        Returns ``True`` when the update was applied; ``False`` when the
        subsession is unknown (including already-terminal).
        """
        info = self._subs.get(sub_id)
        if info is None:
            return False
        info.checkpoint = checkpoint
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

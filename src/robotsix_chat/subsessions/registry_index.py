"""Owner-scoped queries and tree operations (:class:`RegistryIndex`).

Split out of :mod:`robotsix_chat.subsessions.registry`; that module
re-exports these names so existing imports keep working.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from robotsix_chat.chat.events import subsession_started_frame

from .models import SubsessionInfo, SubsessionKind, SubsessionStatus
from .registry_store import RegistryStore

logger = logging.getLogger(__name__)


class _RegistryCallbacks(Protocol):
    """The parent-registry surface :class:`RegistryIndex` calls back into.

    Structural type for :class:`~.registry.SubsessionRegistry`, declared
    here so this module never imports ``registry`` (avoids an import cycle).
    """

    _store: RegistryStore

    def _close_and_publish(
        self,
        info: SubsessionInfo,
        *,
        status: SubsessionStatus,
        summary: str,
        reason: str = ...,
        closed_by: str = ...,
        error: str | None = ...,
    ) -> SubsessionInfo:
        """Close *info* with *status* and publish the closed frame."""

    def cancel_and_close(
        self,
        sub_id: str,
        *,
        reason: str,
        closed_by: str = ...,
        summary: str | None = ...,
    ) -> SubsessionInfo | None:
        """Cancel the running task for *sub_id* and close it."""

    def _publish(self, owner_session_id: str, frame: dict[str, object]) -> None:
        """Publish *frame* to the owner session's event stream."""


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
        registry: _RegistryCallbacks,
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

    def reassign_live_user_chats(
        self, old_owner_session_id: str, new_owner_session_id: str
    ) -> int:
        """Move the live ``user_chat`` panels of one owner to another.

        Used when a periodic preset fires again: the previous run's
        decision panels that still await the operator's answer follow the
        preset to its new session instead of dying with the superseded one
        — otherwise the new run finds no open panel and re-asks the same
        question, and the operator never gets to answer any of them.

        Only ACTIVE ``user_chat`` subsessions move (``running`` /
        ``waiting``); every other kind and every terminal record stays with
        the old owner, whose close path cleans it up.  Publishes a
        ``subsession_started`` frame per moved panel to the new owner's
        event stream.  Returns the number of panels moved.
        """
        if old_owner_session_id == new_owner_session_id:
            return 0
        sub_ids = self._by_owner.get(old_owner_session_id)
        if not sub_ids:
            return 0
        moved = 0
        for sub_id in list(sub_ids):
            info = self._subs.get(sub_id)
            if (
                info is None
                or info.kind is not SubsessionKind.USER_CHAT
                or not info.is_active
            ):
                continue
            sub_ids.discard(sub_id)
            info.owner_session_id = new_owner_session_id
            self._by_owner[new_owner_session_id].add(sub_id)
            self._registry._publish(
                new_owner_session_id,
                subsession_started_frame(info.snapshot()),
            )
            moved += 1
        if not sub_ids:
            self._by_owner.pop(old_owner_session_id, None)
        if moved:
            self._registry._store.persist()
        return moved

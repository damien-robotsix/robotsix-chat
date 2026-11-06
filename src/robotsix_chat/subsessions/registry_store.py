"""JSON persistence for the subsession registry (:class:`RegistryStore`).

Split out of :mod:`robotsix_chat.subsessions.registry`; that module
re-exports these names so existing imports keep working.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from pathlib import Path

from .models import InboxMessage, SubsessionInfo

logger = logging.getLogger(__name__)

# Terminal entries retained in memory/persistence so the panel can show
# recent history after a reload; older ones are pruned oldest-first.
_MAX_TERMINAL_ENTRIES = 50


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
        in_flight: dict[str, list[InboxMessage]],
        wake_events: dict[str, asyncio.Event],
        by_owner: dict[str, set[str]],
    ) -> None:
        """*store_path* is the JSON file; the dicts are shared references."""
        self._store_path = store_path
        self._subs = subs
        self._inboxes = inboxes
        self._in_flight = in_flight
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
            in_flight = self._in_flight.get(info.id)
            entry["inbox"] = [message.as_dict() for message in inbox] if inbox else []
            entry["in_flight_inbox"] = (
                [message.as_dict() for message in in_flight] if in_flight else []
            )
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
            self._in_flight.pop(info.id, None)
            self._wake_events.pop(info.id, None)
            owner_set = self._by_owner.get(info.owner_session_id)
            if owner_set is not None:
                owner_set.discard(info.id)
                if not owner_set:
                    del self._by_owner[info.owner_session_id]

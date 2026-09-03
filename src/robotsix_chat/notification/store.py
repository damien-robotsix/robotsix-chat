"""Persistent bounded store for browser notifications.

A :class:`NotificationStore` persists notifications under chat-data so they
survive container recreation and are no longer silently dropped when no
browser is connected at publish time.  This module provides the storage
foundation only — wiring :func:`notify_user` to write into the store (and
replaying missed notifications) is handled by later steps.

Each record carries:
    id (str) — unique identifier (uuid4 hex)
    ts (str) — ISO-8601 UTC timestamp of publication
    title (str) — one-line notification title
    body (str) — notification message body
    source_session (str) — session that published the notification
    delivered (bool) — whether it has been pushed to an SSE client
    read (bool) — whether the user has acknowledged/seen it
    read_ts (str | None) — ISO-8601 UTC timestamp of when it was marked
        read (``None`` while unread)

Retention is bounded by a three-part policy applied on every write, so the
store never grows without bound:

* **Absolute lifetime** — records older than ``retention_days`` (default
  90) are dropped regardless of read state.
* **Read lifetime** — records that have been read are dropped once they
  have been read for more than ``read_retention_days`` (default 30),
  reclaiming space from acknowledged notifications sooner.
* **Count cap** — at most ``max_events`` (default 200) newest records are
  kept; the oldest beyond that are evicted.

When the count cap evicts records (the store is at capacity), a warning is
logged so operators can monitor store pressure.

No external database is introduced — records are persisted to a single
JSON file, mirroring the ``ContinuationStore`` pattern.  Reads and writes
are guarded by an in-process lock, which is sufficient for the simple
sequential access used by the notification publish path and SSE replay
(single uvicorn worker / event loop).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

#: Maximum records retained in the store (newest kept).
MAX_EVENTS = 200

#: Records older than this many days are dropped on write (absolute lifetime).
RETENTION_DAYS = 90

#: Read records are dropped once read for more than this many days.
READ_RETENTION_DAYS = 30


class NotificationRecord(BaseModel):
    """A single persisted browser notification."""

    id: str
    ts: str
    title: str
    body: str
    source_session: str
    delivered: bool = False
    read: bool = False
    read_ts: str | None = None


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


class NotificationStore:
    """Persist a bounded, ordered set of browser notifications.

    Args:
        path: Path to the JSON persistence file (under chat-data).
        max_events: Maximum number of newest records to retain.  Default
            ``MAX_EVENTS`` (200).
        retention_days: Records older than this many days are dropped on
            write (absolute lifetime).  Default ``RETENTION_DAYS`` (90).
        read_retention_days: Read records are dropped once they have been
            read for more than this many days.  Default
            ``READ_RETENTION_DAYS`` (30).

    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_events: int = MAX_EVENTS,
        retention_days: int = RETENTION_DAYS,
        read_retention_days: int = READ_RETENTION_DAYS,
    ) -> None:
        """Create a store persisting to *path*."""
        self._path = Path(path)
        self._max_events = max_events
        self._retention_days = retention_days
        self._read_retention_days = read_retention_days
        self._lock = Lock()
        self._records: list[dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def append(
        self,
        *,
        title: str,
        body: str,
        source_session: str,
        ts: str | None = None,
    ) -> NotificationRecord:
        """Append a new notification record and return it.

        The record is persisted, then bounded retention is applied — the
        oldest records beyond ``max_events``, anything older than
        ``retention_days``, and read records older than
        ``read_retention_days`` since being read are dropped.

        Args:
            title: One-line notification title.
            body: Notification message body.
            source_session: The session that published the notification.
            ts: Optional ISO-8601 UTC timestamp (normally auto-generated).
                Provided mainly for tests that need to exercise retention.

        Returns:
            The newly appended :class:`NotificationRecord`.

        """
        record: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "ts": ts if ts is not None else _now_iso(),
            "title": title,
            "body": body,
            "source_session": source_session,
            "delivered": False,
            "read": False,
            "read_ts": None,
        }
        with self._lock:
            self._records.append(record)
            self._prune()
            self._persist()
        return NotificationRecord(**record)

    def list(
        self,
        *,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> list[NotificationRecord]:
        """Return persisted records, newest first.

        Args:
            session_id: When given, only records published by this session
                are returned.
            limit: When given, return at most this many records.

        Returns:
            Records ordered newest-first.

        """
        with self._lock:
            records = list(self._records)
        if session_id is not None:
            records = [r for r in records if r["source_session"] == session_id]
        records.sort(key=lambda r: r["ts"], reverse=True)
        if limit is not None:
            records = records[:limit]
        return [NotificationRecord(**r) for r in records]

    def mark_delivered(self, ids: Iterable[str]) -> int:
        """Mark the given records as delivered to an SSE client.

        Args:
            ids: Notification ids to mark delivered.

        Returns:
            The number of records whose ``delivered`` flag changed.

        """
        return self._set_flag(ids, "delivered", True)

    def mark_read(self, ids: Iterable[str]) -> int:
        """Mark the given records as read by the user.

        Newly-read records are stamped with a ``read_ts`` timestamp so the
        read-lifetime retention policy can purge them once they have been
        read for longer than ``read_retention_days``.

        Args:
            ids: Notification ids to mark read.

        Returns:
            The number of records whose ``read`` flag changed.

        """
        return self._set_flag(ids, "read", True)

    def size(self) -> int:
        """Return the number of records currently held in the store.

        Exposed for monitoring the store's size against ``max_events``.
        """
        with self._lock:
            return len(self._records)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _set_flag(self, ids: Iterable[str], flag: str, value: bool) -> int:
        """Set a boolean flag on matching records and persist changes."""
        wanted = set(ids)
        changed = 0
        with self._lock:
            for record in self._records:
                if record["id"] in wanted and record[flag] != value:
                    record[flag] = value
                    # Stamp when a record transitions to read so read-based
                    # retention has a reference point; clear it on unread.
                    if flag == "read":
                        record["read_ts"] = _now_iso() if value else None
                    changed += 1
            if changed:
                self._persist()
        return changed

    def _prune(self) -> None:
        """Apply the bounded retention policy to :attr:`_records` in place.

        Three rules are applied in order: absolute lifetime
        (``retention_days``), read lifetime (``read_retention_days`` since
        being read), then the ``max_events`` count cap.  When the count cap
        evicts records a warning is logged so store pressure is observable.
        """
        now = datetime.now(UTC)
        pub_cutoff_iso = (now - timedelta(days=self._retention_days)).isoformat()
        read_cutoff_iso = (now - timedelta(days=self._read_retention_days)).isoformat()

        def _survives(record: dict[str, Any]) -> bool:
            # Absolute lifetime: drop anything published before the cutoff.
            if record["ts"] < pub_cutoff_iso:
                return False
            # Read lifetime: drop read records read before the read cutoff.
            read_ts = record.get("read_ts")
            return not (record.get("read") and read_ts and read_ts < read_cutoff_iso)

        kept = [r for r in self._records if _survives(r)]
        kept.sort(key=lambda r: r["ts"])
        if len(kept) > self._max_events:
            evicted = len(kept) - self._max_events
            kept = kept[-self._max_events :]
            logger.warning(
                "Notification store %s at capacity (%d): evicted %d oldest "
                "record(s) before natural expiry",
                self._path,
                self._max_events,
                evicted,
            )
        self._records = kept

    def _load(self) -> None:
        """Load persisted records from disk, or start empty on failure."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._records = []
            return
        except json.JSONDecodeError, OSError:
            logger.warning(
                "Notification store %s unreadable; starting empty", self._path
            )
            self._records = []
            return
        if not isinstance(data, list):
            logger.warning(
                "Notification store %s holds %s, not a list; starting empty",
                self._path,
                type(data).__name__,
            )
            self._records = []
            return
        kept: list[dict[str, Any]] = []
        skipped = 0
        for record in data:
            if not isinstance(record, dict):
                skipped += 1
                continue
            try:
                # Validate each persisted record against the schema so a
                # single corrupted/partial entry (missing or wrong-typed
                # field) cannot crash ``list``/``mark_*`` and blank the
                # whole unread API — drop it instead.
                NotificationRecord(**record)
            except TypeError, ValidationError:
                skipped += 1
                continue
            kept.append(record)
        if skipped:
            logger.warning(
                "Notification store %s: dropped %d corrupted record(s) on load",
                self._path,
                skipped,
            )
        self._records = kept

    def _persist(self) -> None:
        """Atomically write records to disk (tmp file + rename)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._records, indent=2), encoding="utf-8")
        tmp.replace(self._path)

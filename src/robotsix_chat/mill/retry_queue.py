"""Persistent retry queue for board writes that fail due to broker unavailability.

When ``consult_mill`` encounters a
:class:`~robotsix_chat.broker_client.BrokerUnavailableError` the request is
enqueued here and retried later with exponential backoff instead of dropping
the write silently.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["BoardWriteRetryQueue"]

_QUEUE_PATH = Path(".data/board_write_queue.json")
_INITIAL_DELAY = 900  # 15 min
_MAX_DELAY = 14_400  # 4 hr
_BACKOFF_FACTOR = 2
_JITTER_FRACTION = 0.2  # ±20 %


def _next_delay(attempt_count: int) -> float:
    """Compute retry delay for *attempt_count* with exponential backoff and jitter."""
    raw: float = min(
        _INITIAL_DELAY * (_BACKOFF_FACTOR ** attempt_count), _MAX_DELAY
    )
    return raw * random.uniform(  # noqa: S311 — jitter, not cryptography
        1 - _JITTER_FRACTION, 1 + _JITTER_FRACTION
    )


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _now_iso_after_delay(delay_seconds: float) -> str:
    """Return an ISO-8601 UTC timestamp *delay_seconds* from now."""
    return (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()


class BoardWriteRetryQueue:
    """Persistent queue that retries board writes after broker-unavailability failures.

    Each entry is keyed by a SHA-256 digest of the request text so that
    identical concurrent enqueue attempts produce the same ID and are
    deduplicated.
    """

    def __init__(
        self,
        consult_fn: Callable[[str], Awaitable[str]],
        queue_path: Path = _QUEUE_PATH,
    ) -> None:
        """Store *consult_fn* and *queue_path*, load any persisted entries."""
        self._consult_fn = consult_fn
        self._queue_path = queue_path
        self._entries: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task[None] | None = None
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, request: str) -> str:
        """Add *request* to the retry queue and return a status string for the LLM.

        If an entry with the same request hash is already pending (or
        failed), a short "already queued" message is returned instead of
        creating a duplicate.
        """
        entry_id = hashlib.sha256(request.encode()).hexdigest()[:16]
        if entry_id in self._entries:
            existing = self._entries[entry_id]
            logger.debug(
                "Board write already in retry queue (id=%s, status=%s)",
                entry_id,
                existing["status"],
            )
            return f"[retry-queue] Write already queued (id={entry_id})."

        entry: dict[str, Any] = {
            "id": entry_id,
            "request": request,
            "enqueued_at": _now_iso(),
            "attempt_count": 0,
            "next_attempt_at": _now_iso(),  # overwritten below
            "last_error": None,
            "status": "pending",
        }
        # Set first attempt time (now + initial delay with jitter)
        entry["next_attempt_at"] = _now_iso_after_delay(_next_delay(0))
        self._entries[entry_id] = entry
        self._persist()
        self._ensure_started()
        logger.info("Board write enqueued for retry (id=%s)", entry_id)
        return (
            f"[retry-queue] Board write queued for retry "
            f"(id={entry_id}; first retry in ~15 min)."
        )

    def status(self) -> str:
        """Return a human-readable summary of the queue contents."""
        if not self._entries:
            return "[retry-queue] No pending board writes."

        now = datetime.now(UTC)
        header = (
            f"{'id':<16} {'status':<10} {'request':<40} "
            f"{'att':>3} {'last_error':<40} {'next_attempt'}"
        )
        lines: list[str] = [header]
        for e in self._entries.values():
            nxt = e.get("next_attempt_at")
            if nxt:
                try:
                    nxt_dt = datetime.fromisoformat(nxt)
                    delta = (nxt_dt - now).total_seconds()
                    if delta <= 0:
                        rel = "overdue"
                    else:
                        m, s = divmod(int(delta), 60)
                        h, m = divmod(m, 60)
                        rel = f"in {h}h {m}m {s}s" if h else f"in {m}m {s}s"
                except (ValueError, TypeError):
                    rel = str(nxt)
            else:
                rel = "-"
            req = str(e.get("request", ""))[:40]
            err = str(e.get("last_error", "") or "")[:40]
            row = (
                f"{e['id']:<16} {e['status']:<10} {req:<40} "
                f"{e.get('attempt_count', 0):>3} {err:<40} {rel}"
            )
            lines.append(row)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Start (or restart) the background drain loop if not already running."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No running loop; drain will start on next async call
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._drain_loop())

    async def _drain_loop(self) -> None:
        """Continuously process pending entries as their retry time arrives."""
        while True:
            pending = [
                e for e in self._entries.values() if e["status"] == "pending"
            ]
            if not pending:
                break

            now = datetime.now(UTC)
            wait_secs = max(
                0.0,
                min(
                    (
                        datetime.fromisoformat(e["next_attempt_at"]) - now
                    ).total_seconds()
                    for e in pending
                ),
            )
            await asyncio.sleep(wait_secs)

            now = datetime.now(UTC)
            for entry in list(pending):
                nxt_dt = datetime.fromisoformat(entry["next_attempt_at"])
                if nxt_dt <= now:
                    await self._attempt(entry)

    async def _attempt(self, entry: dict[str, Any]) -> None:
        """Try to deliver one enqueued request."""
        try:
            await self._consult_fn(entry["request"])
        except BrokerUnavailableError as exc:  # noqa: F821 — imported at bottom
            entry["attempt_count"] = entry.get("attempt_count", 0) + 1
            entry["last_error"] = str(exc)
            entry["next_attempt_at"] = _now_iso_after_delay(
                _next_delay(entry["attempt_count"])
            )
            self._persist()
            logger.debug(
                "Retry #%d for board write id=%s: %s",
                entry["attempt_count"],
                entry["id"],
                exc,
            )
        except Exception as exc:
            entry["status"] = "failed"
            entry["last_error"] = str(exc)
            self._persist()
            logger.warning(
                "Board write id=%s permanently failed (non-retryable): %s",
                entry["id"],
                exc,
            )
        else:
            del self._entries[entry["id"]]
            self._persist()
            logger.info(
                "Board write id=%s succeeded after %d attempt(s)",
                entry["id"],
                entry.get("attempt_count", 0) + 1,
            )

    def _persist(self) -> None:
        """Write the full queue to ``self._queue_path`` as JSON."""
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            self._queue_path.write_text(
                json.dumps(list(self._entries.values()), indent=2)
            )
        except OSError as exc:
            logger.warning("Failed to persist retry queue: %s", exc)

    def _load(self) -> None:
        """Populate ``self._entries`` from the persisted JSON file, if any."""
        if not self._queue_path.exists():
            return
        try:
            raw = self._queue_path.read_text()
            entries: list[dict[str, Any]] = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to load retry queue from %s: %s", self._queue_path, exc
            )
            return
        self._entries = {e["id"]: e for e in entries}


# Lazy import to avoid circular dependency (BrokerUnavailableError lives in
# broker_client.py which does not import this module).
from robotsix_chat.broker_client import BrokerUnavailableError  # noqa: E402, I001

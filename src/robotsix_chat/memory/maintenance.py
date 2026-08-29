"""Periodic compaction/pruning for the cognee LanceDB vector store.

Every cognify write appends a fragment, a new dataset version and deletion
files to each LanceDB table; nothing in cognee ever calls LanceDB's own
maintenance, so the tables accumulate thousands of tiny fragments and
versions.  A vector search then has to scan every fragment and apply every
deletion vector, which both starves recall and saturates the host disk.

This module runs LanceDB's :meth:`Table.optimize` (compact fragments + prune
old versions) on a periodic schedule.  Tables are processed **sequentially**
so the first run over a badly-fragmented store cannot exhaust memory, and the
whole pass runs under the cognee write lock so it never overlaps a live
``cognify`` write — if a write is already in progress the pass is skipped and
logged.

``lancedb`` is imported lazily inside :func:`optimize_lancedb_store` because it
is only present when the ``memory`` extra (cognee) is installed; this module is
otherwise import-safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# The LanceDB store lives under ``<data_dir>/system/databases/cognee.lancedb``
# (cognee's ``system_root_directory`` is ``<data_dir>/system``).
LANCEDB_RELATIVE_PATH = Path("system") / "databases" / "cognee.lancedb"


@dataclass
class TableMaintenanceResult:
    """Per-table before/after counts and timing for one optimize pass."""

    name: str
    fragments_before: int = 0
    fragments_after: int = 0
    versions_before: int = 0
    versions_after: int = 0
    rows_before: int = 0
    rows_after: int = 0
    duration_seconds: float = 0.0
    error: str | None = None


def _table_stats(table: object) -> tuple[int, int, int]:
    """Return ``(fragments, versions, rows)`` for a LanceDB table."""
    dataset = table.to_lance()  # type: ignore[attr-defined]
    fragments = len(list(dataset.get_fragments()))
    versions = len(list(table.list_versions()))  # type: ignore[attr-defined]
    rows = int(table.count_rows())  # type: ignore[attr-defined]
    return fragments, versions, rows


def optimize_lancedb_store(
    store_path: Path | str,
    *,
    cleanup_older_than: timedelta,
) -> list[TableMaintenanceResult]:
    """Compact and prune every table in the LanceDB store at *store_path*.

    Runs **synchronously** (LanceDB's client is blocking) — call it via
    :func:`asyncio.to_thread` from async code.  Tables are processed one at a
    time to bound peak memory, and a failure on one table is captured in its
    :class:`TableMaintenanceResult` and logged without aborting the rest.

    Args:
        store_path: Filesystem path of the ``cognee.lancedb`` directory.
        cleanup_older_than: Version-retention window passed to
            :meth:`Table.optimize` — versions older than this are pruned.

    Returns:
        One :class:`TableMaintenanceResult` per table (in table order).

    """
    import lancedb

    store = Path(store_path)
    db = lancedb.connect(str(store))
    results: list[TableMaintenanceResult] = []
    for name in db.table_names():
        start = time.monotonic()
        try:
            table = db.open_table(name)
            frags_before, vers_before, rows_before = _table_stats(table)
            table.optimize(cleanup_older_than=cleanup_older_than)
            frags_after, vers_after, rows_after = _table_stats(table)
            duration = time.monotonic() - start
            result = TableMaintenanceResult(
                name=name,
                fragments_before=frags_before,
                fragments_after=frags_after,
                versions_before=vers_before,
                versions_after=vers_after,
                rows_before=rows_before,
                rows_after=rows_after,
                duration_seconds=duration,
            )
            logger.info(
                "lancedb maintenance: table=%s fragments %d->%d versions %d->%d "
                "rows %d->%d in %.1fs",
                name,
                frags_before,
                frags_after,
                vers_before,
                vers_after,
                rows_before,
                rows_after,
                duration,
            )
            if rows_before != rows_after:
                logger.error(
                    "lancedb maintenance changed row count for table %s: %d -> %d",
                    name,
                    rows_before,
                    rows_after,
                )
        except Exception as exc:
            duration = time.monotonic() - start
            result = TableMaintenanceResult(
                name=name, duration_seconds=duration, error=str(exc)
            )
            logger.warning(
                "lancedb maintenance failed for table %s (%s) — continuing",
                name,
                exc,
                exc_info=True,
            )
        results.append(result)
    return results


class LanceDbMaintenanceScheduler:
    """Run :func:`optimize_lancedb_store` on startup and on a fixed interval.

    Mirrors :class:`robotsix_chat.health.scheduler.HealthScheduler`: ``start``
    launches a background loop that runs immediately then sleeps on the
    interval, and ``stop`` cancels it.  Every pass acquires the shared cognee
    write lock — if a ``cognify`` write is already holding it the pass is
    skipped (and logged) rather than blocking the write, and errors never
    escape the loop.
    """

    def __init__(
        self,
        *,
        store_path: Path | str,
        write_lock: asyncio.Lock,
        interval_seconds: float,
        cleanup_older_than: timedelta,
    ) -> None:
        """Create a scheduler for the LanceDB store at *store_path*.

        Args:
            store_path: Filesystem path of the ``cognee.lancedb`` directory.
            write_lock: The cognee write lock serialising ``cognify`` writes;
                held for the duration of each maintenance pass.
            interval_seconds: Seconds between scheduled passes.
            cleanup_older_than: Version-retention window forwarded to
                :meth:`Table.optimize`.

        """
        self.store_path = Path(store_path)
        self._write_lock = write_lock
        self.interval_seconds = interval_seconds
        self.cleanup_older_than = cleanup_older_than
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Launch the background loop (idempotent — no-op if already running)."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "lancedb maintenance scheduler started (interval=%ss, store=%s)",
            self.interval_seconds,
            self.store_path,
        )

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("lancedb maintenance scheduler stopped")

    async def run_once(self) -> list[TableMaintenanceResult]:
        """Run one maintenance pass; skip (and log) if a write is in progress.

        Never raises: any failure opening the store or optimizing a table is
        logged and swallowed so the scheduler loop — and the server — keep
        running.
        """
        # Single-threaded event loop: no await between the ``locked()`` probe
        # and acquiring the free lock, so this cannot race a concurrent write.
        if self._write_lock.locked():
            logger.info("lancedb maintenance skipped — a memory write is in progress")
            return []
        if not self.store_path.exists():
            logger.debug(
                "lancedb maintenance skipped — store %s does not exist yet",
                self.store_path,
            )
            return []
        start = time.monotonic()
        async with self._write_lock:
            try:
                results = await asyncio.to_thread(
                    optimize_lancedb_store,
                    self.store_path,
                    cleanup_older_than=self.cleanup_older_than,
                )
            except Exception:
                logger.exception(
                    "lancedb maintenance pass failed — will retry next interval"
                )
                return []
        logger.info(
            "lancedb maintenance pass complete: %d table(s) in %.1fs",
            len(results),
            time.monotonic() - start,
        )
        return results

    async def _loop(self) -> None:
        """Run a pass immediately, then every ``interval_seconds`` forever."""
        while True:
            try:
                await self.run_once()
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("lancedb maintenance loop iteration raised", exc_info=True)

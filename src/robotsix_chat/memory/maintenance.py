"""Periodic maintenance for the cognee memory stores.

Two independent jobs live here, each targeting one store and never touching
the others:

* :func:`optimize_lancedb_store` / :class:`LanceDbMaintenanceScheduler` —
  compact + prune the cognee **LanceDB** vector store.  Every cognify write
  appends a fragment, a new dataset version and deletion files; nothing in
  cognee ever calls LanceDB's own maintenance, so the tables accumulate
  thousands of tiny fragments and versions.  A vector search then has to scan
  every fragment and apply every deletion vector, which both starves recall
  and saturates the host disk.
* :func:`vacuum_cognee_db` / :class:`CogneeDbVacuumScheduler` — run
  ``VACUUM`` / ``PRAGMA incremental_vacuum`` against the cognee **SQLite**
  relational store (``cognee_db``) so pages freed by row deletion (e.g.
  retention pruning of the bookkeeping tables) are returned to disk instead
  of accumulating as freelist.  Runs on a configurable off-peak window.

Both jobs process their store **sequentially** so the first run over a
badly-fragmented store cannot exhaust memory, and each pass runs under the
cognee write lock so it never overlaps a live ``cognify`` write — if a write
is already in progress the pass is skipped and logged.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# The LanceDB store lives under ``<data_dir>/system/databases/cognee.lancedb``
# (cognee's ``system_root_directory`` is ``<data_dir>/system``).
LANCEDB_RELATIVE_PATH = Path("system") / "databases" / "cognee.lancedb"

# The SQLite relational store lives next to it as
# ``<data_dir>/system/databases/cognee_db`` (cognee's ``db_name`` default).
COGNEE_DB_RELATIVE_PATH = Path("system") / "databases" / "cognee_db"


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


# ---------------------------------------------------------------------------
# cognee_db SQLite VACUUM maintenance
# ---------------------------------------------------------------------------


@dataclass
class CogneeDbVacuumResult:
    """Before/after size and freelist deltas for one vacuum pass."""

    mode: str
    size_before: int = 0
    size_after: int = 0
    freelist_before: int = 0
    freelist_after: int = 0
    duration_seconds: float = 0.0
    error: str | None = None


def _db_file_size(path: Path) -> int:
    """Return the on-disk size of *path* (0 when missing/unreadable)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def vacuum_cognee_db(
    db_path: Path | str,
    *,
    mode: str = "incremental_vacuum",
) -> CogneeDbVacuumResult:
    """Run VACUUM or incremental_vacuum against the cognee SQLite store.

    Uses the stdlib ``sqlite3`` module directly (no extra dependency): opens
    the ``cognee_db`` file, records its size and freelist, runs the requested
    vacuum mode, then records the post-pass size/freelist.  The before/after
    deltas are logged at INFO so growth and reclamation are visible.

    Args:
        db_path: Filesystem path of the ``cognee_db`` SQLite file.
        mode: ``"incremental_vacuum"`` (default) or ``"vacuum"``.

            * ``"incremental_vacuum"`` — issues ``PRAGMA incremental_vacuum``,
              reclaiming freelist pages at the tail of the file without a full
              rebuild.  Requires the database to have been created with
              ``auto_vacuum`` enabled; when ``PRAGMA auto_vacuum`` is 0
              (NONE) an incremental vacuum is a no-op, so this mode falls
              back to a full ``VACUUM`` for that store (and logs it).
            * ``"vacuum"`` — a full ``VACUUM`` rebuild, which also works when
              ``auto_vacuum`` is NONE and returns freed pages to the OS.

    Returns:
        A :class:`CogneeDbVacuumResult`; never raises.  A lock/unopenable
        store is captured in ``result.error`` and logged.

    """
    import sqlite3

    db = Path(db_path)
    start = time.monotonic()
    result = CogneeDbVacuumResult(
        mode=mode,
        size_before=_db_file_size(db),
    )
    if not db.is_file():
        # sqlite3.connect would silently CREATE an empty file; a missing
        # store must instead be reported so the scheduler knows to skip.
        result.error = f"store {db} does not exist"
        result.duration_seconds = time.monotonic() - start
        logger.warning("cognee_db vacuum skipped — store %s does not exist", db)
        return result
    try:
        conn = sqlite3.connect(str(db), timeout=60.0)
        try:
            auto_vacuum = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
            result.freelist_before = int(
                conn.execute("PRAGMA freelist_count").fetchone()[0]
            )
            if mode == "vacuum" or auto_vacuum == 0:
                if mode == "incremental_vacuum" and auto_vacuum == 0:
                    logger.info(
                        "cognee_db vacuum: auto_vacuum is NONE, falling back to "
                        "full VACUUM for %s",
                        db,
                    )
                conn.execute("VACUUM")
            else:
                # Reclaim every freelist page (omitted N = all of them).
                conn.execute("PRAGMA incremental_vacuum")
            result.freelist_after = int(
                conn.execute("PRAGMA freelist_count").fetchone()[0]
            )
        finally:
            conn.close()
        result.size_after = _db_file_size(db)
        result.duration_seconds = time.monotonic() - start
        logger.info(
            "cognee_db vacuum: mode=%s size %d -> %d bytes, freelist %d -> %d in %.1fs",
            result.mode,
            result.size_before,
            result.size_after,
            result.freelist_before,
            result.freelist_after,
            result.duration_seconds,
        )
    except Exception as exc:
        result.duration_seconds = time.monotonic() - start
        result.error = str(exc)
        logger.warning(
            "cognee_db vacuum failed for %s (%s) — will retry next interval",
            db,
            exc,
            exc_info=True,
        )
    return result


class CogneeDbVacuumScheduler:
    """Run :func:`vacuum_cognee_db` on a configurable off-peak schedule.

    Mirrors :class:`LanceDbMaintenanceScheduler` (and
    :class:`robotsix_chat.health.scheduler.HealthScheduler`): ``start``
    launches a background loop and ``stop`` cancels it.  The pass only runs
    inside the configured off-peak UTC hour window; outside it the loop
    sleeps until the window opens.  Every pass acquires the shared cognee
    write lock — if a ``cognify`` write is already holding it the pass is
    skipped (and logged) rather than blocking the write, and errors never
    escape the loop.
    """

    def __init__(
        self,
        *,
        db_path: Path | str,
        write_lock: asyncio.Lock,
        interval_seconds: float,
        mode: str = "incremental_vacuum",
        off_peak_window: tuple[int, int] | None = None,
    ) -> None:
        """Create a scheduler for the cognee SQLite store at *db_path*.

        Args:
            db_path: Filesystem path of the ``cognee_db`` SQLite file.
            write_lock: The cognee write lock serialising ``cognify`` writes;
                held for the duration of each vacuum pass.
            interval_seconds: Seconds between scheduled passes (when inside
                the off-peak window).
            mode: Vacuum mode forwarded to :func:`vacuum_cognee_db`
                (``"incremental_vacuum"`` default).
            off_peak_window: ``(start_hour, end_hour)`` in UTC — the pass
                only runs while ``start_hour <= utc_hour < end_hour``.
                ``None`` disables the window (run any time).

        """
        self.db_path = Path(db_path)
        self._write_lock = write_lock
        self.interval_seconds = interval_seconds
        self.mode = mode
        self.off_peak_window = off_peak_window
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Launch the background loop (idempotent — no-op if already running)."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "cognee_db vacuum scheduler started (interval=%ss, mode=%s, "
            "window=%s, db=%s)",
            self.interval_seconds,
            self.mode,
            self.off_peak_window,
            self.db_path,
        )

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("cognee_db vacuum scheduler stopped")

    def _in_off_peak_window(self, now: datetime | None = None) -> bool:
        """Return True if *now* (UTC, default: now) is inside the window."""
        if self.off_peak_window is None:
            return True
        hour = (now if now is not None else datetime.now(UTC)).hour
        start_hour, end_hour = self.off_peak_window
        return start_hour <= hour < end_hour

    async def run_once(self) -> CogneeDbVacuumResult | None:
        """Run one vacuum pass; skip (and log) if a write is in progress.

        Returns the :class:`CogneeDbVacuumResult`, or ``None`` when the pass
        was skipped (write in progress, missing store, outside the off-peak
        window).  Never raises.
        """
        if self._write_lock.locked():
            logger.info("cognee_db vacuum skipped — a memory write is in progress")
            return None
        if not self.db_path.exists():
            logger.debug(
                "cognee_db vacuum skipped — store %s does not exist yet",
                self.db_path,
            )
            return None
        if not self._in_off_peak_window():
            logger.info(
                "cognee_db vacuum skipped — outside off-peak window %s",
                self.off_peak_window,
            )
            return None
        async with self._write_lock:
            try:
                return await asyncio.to_thread(
                    vacuum_cognee_db, self.db_path, mode=self.mode
                )
            except Exception:
                logger.exception(
                    "cognee_db vacuum pass failed — will retry next interval"
                )
                return None

    async def _loop(self) -> None:
        """Run a pass inside the off-peak window, sleeping to its opening."""
        while True:
            try:
                if self._in_off_peak_window():
                    await self.run_once()
                    await asyncio.sleep(self.interval_seconds)
                else:
                    await asyncio.sleep(self._seconds_until_window_open())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("cognee_db vacuum loop iteration raised", exc_info=True)

    def _seconds_until_window_open(self, now: datetime | None = None) -> float:
        """Seconds until the next window opening (0 when inside the window).

        Only called when the current time is outside the window; assumes a
        single daily window that does not wrap midnight.
        """
        if self.off_peak_window is None:
            return 0.0
        now = now if now is not None else datetime.now(UTC)
        start_hour, _end_hour = self.off_peak_window
        seconds_into_day = now.hour * 3600 + now.minute * 60 + now.second
        start_seconds = start_hour * 3600
        delta = start_seconds - seconds_into_day
        if delta <= 0:
            delta += 24 * 3600
        return float(delta)

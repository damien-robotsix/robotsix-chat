"""Periodic maintenance for the cognee memory stores.

Three independent jobs live here, each targeting one concern and never
touching the others:

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
* :func:`emit_store_metrics` / :class:`StoreMetricsScheduler` — a **read-only**
  consolidated store-size emitter.  On a configurable interval it logs the
  cognee_db file size and per-table (``pipeline_runs``/``results``/``queries``)
  row counts and sizes, the graph store size and node/edge counts, and the
  LanceDB total size plus fragment/deletion file counts, so store growth can
  be trended and alarmed from one line.  It mutates nothing and therefore
  never takes the write lock.

The two mutating jobs process their store **sequentially** so the first run
over a
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# Consolidated store-size metrics
# ---------------------------------------------------------------------------

# The ladybug graph store's main database file and WAL sidecars all share this
# basename prefix under ``<data_dir>/system/databases``.
GRAPH_STORE_GLOB = "cognee_graph_ladybug*"

# The cognee_db SQLite bookkeeping tables whose per-table size/row-count is
# emitted (the parent store-visibility ticket named exactly these three).
COGNEE_DB_METRIC_TABLES: tuple[str, ...] = ("pipeline_runs", "results", "queries")

# An optional async callback the scheduler uses to fetch ``(nodes, edges)``
# counts from the live graph engine; returns ``None`` when unavailable.
GraphStatsProvider = Callable[[], Awaitable[tuple[int, int] | None]]


@dataclass
class StoreMetrics:
    """Consolidated point-in-time size snapshot of the cognee stores."""

    cognee_db_size: int = 0
    table_rows: dict[str, int] = field(default_factory=dict)
    table_sizes: dict[str, int] = field(default_factory=dict)
    graph_size: int = 0
    graph_nodes: int | None = None
    graph_edges: int | None = None
    lancedb_size: int = 0
    lancedb_fragments: int = 0
    lancedb_deletion_files: int = 0
    error: str | None = None


def _dir_total_size(path: Path) -> int:
    """Return the recursive on-disk size of *path* (0 when missing)."""
    if not path.exists():
        return 0
    if path.is_file():
        return _db_file_size(path)
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _lancedb_file_stats(store_path: Path) -> tuple[int, int, int]:
    """Return ``(total_size_bytes, fragment_files, deletion_files)``.

    Derived purely from the on-disk layout (no ``lancedb`` import): fragment
    data files are ``*.lance`` under a table's ``data`` directory, and
    deletion files live under a ``_deletions`` directory.
    """
    if not store_path.exists():
        return 0, 0, 0
    total = 0
    fragments = 0
    deletions = 0
    for child in store_path.rglob("*"):
        try:
            if not child.is_file():
                continue
            total += child.stat().st_size
        except OSError:
            continue
        parts = child.parts
        if child.suffix == ".lance" and "data" in parts:
            fragments += 1
        if "_deletions" in parts:
            deletions += 1
    return total, fragments, deletions


def _cognee_db_table_stats(db_path: Path) -> tuple[dict[str, int], dict[str, int]]:
    """Return ``(row_counts, byte_sizes)`` for :data:`COGNEE_DB_METRIC_TABLES`.

    Row counts come from ``SELECT count(*)``; per-table byte sizes come from
    the ``dbstat`` virtual table when the SQLite build provides it (it is
    absent in some builds, in which case ``byte_sizes`` is simply empty).
    Missing tables are omitted rather than reported as zero.
    """
    import sqlite3

    rows: dict[str, int] = {}
    sizes: dict[str, int] = {}
    if not db_path.is_file():
        return rows, sizes
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    try:
        existing = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for name in COGNEE_DB_METRIC_TABLES:
            if name in existing:
                # ``name`` is drawn only from the hardcoded
                # COGNEE_DB_METRIC_TABLES constant — never user input.
                rows[name] = int(
                    conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]  # noqa: S608
                )
        try:
            for name, pgsize in conn.execute(
                "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
            ):
                if name in COGNEE_DB_METRIC_TABLES:
                    sizes[name] = int(pgsize)
        except sqlite3.Error:
            # dbstat not compiled in — row counts alone still convey growth.
            pass
    finally:
        conn.close()
    return rows, sizes


def emit_store_metrics(
    data_dir: Path | str,
    *,
    graph_nodes: int | None = None,
    graph_edges: int | None = None,
) -> StoreMetrics:
    """Collect and log a consolidated size snapshot of the cognee stores.

    Runs **synchronously** and is read-only — it opens the cognee_db SQLite
    file for ``count(*)``/``dbstat`` queries and walks the store directories,
    but mutates nothing, so it never needs the cognee write lock.  Call it via
    :func:`asyncio.to_thread` from async code.

    Args:
        data_dir: cognee's ``data_dir`` — the parent of ``system/databases``.
        graph_nodes: Node count from the live graph engine when a caller could
            obtain it (see :data:`GraphStatsProvider`); ``None`` otherwise.
        graph_edges: Edge count from the live graph engine; ``None`` otherwise.

    Returns:
        A populated :class:`StoreMetrics`; never raises.  Any failure is
        captured in ``result.error`` and logged.

    """
    root = Path(data_dir).expanduser().resolve()
    metrics = StoreMetrics(graph_nodes=graph_nodes, graph_edges=graph_edges)
    try:
        cognee_db = root / COGNEE_DB_RELATIVE_PATH
        lancedb_store = root / LANCEDB_RELATIVE_PATH
        databases_dir = root / "system" / "databases"

        metrics.cognee_db_size = _db_file_size(cognee_db)
        metrics.table_rows, metrics.table_sizes = _cognee_db_table_stats(cognee_db)
        metrics.graph_size = sum(
            _dir_total_size(p) for p in databases_dir.glob(GRAPH_STORE_GLOB)
        )
        (
            metrics.lancedb_size,
            metrics.lancedb_fragments,
            metrics.lancedb_deletion_files,
        ) = _lancedb_file_stats(lancedb_store)

        logger.info(
            "cognee store metrics: cognee_db=%d bytes rows=%s sizes=%s | "
            "graph=%d bytes nodes=%s edges=%s | "
            "lancedb=%d bytes fragments=%d deletion_files=%d",
            metrics.cognee_db_size,
            metrics.table_rows,
            metrics.table_sizes,
            metrics.graph_size,
            metrics.graph_nodes,
            metrics.graph_edges,
            metrics.lancedb_size,
            metrics.lancedb_fragments,
            metrics.lancedb_deletion_files,
        )
    except Exception as exc:
        metrics.error = str(exc)
        logger.warning(
            "cognee store metrics collection failed (%s) — will retry next interval",
            exc,
            exc_info=True,
        )
    return metrics


class StoreMetricsScheduler:
    """Emit :func:`emit_store_metrics` on startup and on a fixed interval.

    Mirrors :class:`LanceDbMaintenanceScheduler`: ``start`` launches a
    background loop that emits immediately then sleeps on the interval, and
    ``stop`` cancels it.  Unlike the LanceDB/vacuum schedulers this job is
    read-only, so it never acquires the cognee write lock and never skips a
    pass because a write is in progress.  Errors never escape the loop.
    """

    def __init__(
        self,
        *,
        data_dir: Path | str,
        interval_seconds: float,
        graph_stats_provider: GraphStatsProvider | None = None,
    ) -> None:
        """Create a scheduler emitting store metrics for *data_dir*.

        Args:
            data_dir: cognee's ``data_dir`` (parent of ``system/databases``).
            interval_seconds: Seconds between scheduled emissions.
            graph_stats_provider: Optional async callback returning
                ``(nodes, edges)`` from the live graph engine, or ``None`` when
                the counts are unavailable.  Called best-effort each pass; any
                exception is swallowed and the counts logged as ``None``.

        """
        self.data_dir = Path(data_dir)
        self.interval_seconds = interval_seconds
        self._graph_stats_provider = graph_stats_provider
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Launch the background loop (idempotent — no-op if already running)."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "store metrics scheduler started (interval=%ss, data_dir=%s)",
            self.interval_seconds,
            self.data_dir,
        )

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("store metrics scheduler stopped")

    async def run_once(self) -> StoreMetrics:
        """Emit one metrics snapshot (never raises)."""
        graph_nodes: int | None = None
        graph_edges: int | None = None
        if self._graph_stats_provider is not None:
            try:
                stats = await self._graph_stats_provider()
                if stats is not None:
                    graph_nodes, graph_edges = stats
            except Exception:
                logger.debug("store metrics graph-stats provider failed", exc_info=True)
        return await asyncio.to_thread(
            emit_store_metrics,
            self.data_dir,
            graph_nodes=graph_nodes,
            graph_edges=graph_edges,
        )

    async def _loop(self) -> None:
        """Emit a snapshot immediately, then every ``interval_seconds``."""
        while True:
            try:
                await self.run_once()
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("store metrics loop iteration raised", exc_info=True)

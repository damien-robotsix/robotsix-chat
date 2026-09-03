"""Tests for the periodic LanceDB maintenance scheduler.

``lancedb`` is not installed (it ships only with the ``memory`` extra), so a
fake ``lancedb`` module is injected into :data:`sys.modules`; the fake records
every ``optimize`` call and simulates compaction by collapsing fragment/version
counts while leaving the row count untouched.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest

from robotsix_chat.config import (
    MemoryEmbeddingSettings,
    MemoryLlmSettings,
    MemorySettings,
)
from robotsix_chat.memory import maintenance as maintenance_module
from robotsix_chat.memory.cognee import CogneeMemory
from robotsix_chat.memory.maintenance import (
    COGNEE_DB_METRIC_TABLES,
    COGNEE_DB_RELATIVE_PATH,
    LANCEDB_RELATIVE_PATH,
    CogneeDbVacuumResult,
    CogneeDbVacuumScheduler,
    LanceDbMaintenanceScheduler,
    StoreMetrics,
    StoreMetricsScheduler,
    TableMaintenanceResult,
    emit_store_metrics,
    optimize_lancedb_store,
    vacuum_cognee_db,
)


class _FakeDataset:
    def __init__(self, fragments: int) -> None:
        self._fragments = fragments

    def get_fragments(self) -> list[int]:
        return list(range(self._fragments))


class _FakeTable:
    """A LanceDB table stand-in that records optimize calls."""

    def __init__(
        self,
        *,
        fragments: int,
        versions: int,
        rows: int,
        raise_on_optimize: Exception | None = None,
    ) -> None:
        self.fragments = fragments
        self.versions = versions
        self.rows = rows
        self._raise = raise_on_optimize
        self.optimize_calls: list[timedelta] = []

    def to_lance(self) -> _FakeDataset:
        return _FakeDataset(self.fragments)

    def list_versions(self) -> list[int]:
        return list(range(self.versions))

    def count_rows(self) -> int:
        return self.rows

    def optimize(self, *, cleanup_older_than: timedelta) -> None:
        if self._raise is not None:
            raise self._raise
        self.optimize_calls.append(cleanup_older_than)
        # Simulate compaction + pruning: many fragments/versions collapse to a
        # handful; the row count is invariant.
        self.fragments = 1
        self.versions = 1


class _FakeDB:
    def __init__(self, tables: dict[str, _FakeTable]) -> None:
        self._tables = tables

    def table_names(self) -> list[str]:
        return list(self._tables)

    def open_table(self, name: str) -> _FakeTable:
        return self._tables[name]


def _install_fake_lancedb(
    monkeypatch: pytest.MonkeyPatch, tables: dict[str, _FakeTable]
) -> list[str]:
    """Inject a fake ``lancedb`` module; return the list of connect URIs."""
    connected: list[str] = []

    def _connect(uri: str) -> _FakeDB:
        connected.append(uri)
        return _FakeDB(tables)

    fake = types.ModuleType("lancedb")
    fake.connect = _connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lancedb", fake)
    return connected


def _big_tables() -> dict[str, _FakeTable]:
    """Tables shaped like the production store (thousands of fragments)."""
    return {
        "EdgeType_relationship_name.lance": _FakeTable(
            fragments=4470, versions=3595, rows=1000
        ),
        "Entity_name.lance": _FakeTable(fragments=4311, versions=1768, rows=500),
        "DocumentChunk_text.lance": _FakeTable(fragments=2016, versions=1551, rows=250),
    }


def test_optimize_invoked_per_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every table is opened and optimized once, with the retention window."""
    tables = _big_tables()
    connected = _install_fake_lancedb(monkeypatch, tables)
    retention = timedelta(hours=1)

    results = optimize_lancedb_store(
        "/store/cognee.lancedb", cleanup_older_than=retention
    )

    assert connected == ["/store/cognee.lancedb"]
    assert {r.name for r in results} == set(tables)
    for table in tables.values():
        assert table.optimize_calls == [retention]


def test_optimize_reduces_fragments_and_versions_preserving_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: <50 fragments, <10 versions, identical row counts."""
    tables = _big_tables()
    _install_fake_lancedb(monkeypatch, tables)

    results = optimize_lancedb_store(
        "/store/cognee.lancedb", cleanup_older_than=timedelta(hours=1)
    )

    for r in results:
        assert r.error is None
        assert r.fragments_after < 50
        assert r.versions_after < 10
        assert r.rows_before == r.rows_after
        assert r.fragments_after < r.fragments_before
        assert r.versions_after < r.versions_before


def test_optimize_error_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure on one table does not abort maintenance of the others."""
    boom = _FakeTable(
        fragments=100, versions=100, rows=10, raise_on_optimize=RuntimeError("boom")
    )
    ok = _FakeTable(fragments=100, versions=100, rows=10)
    tables = {"broken.lance": boom, "healthy.lance": ok}
    _install_fake_lancedb(monkeypatch, tables)

    results = optimize_lancedb_store(
        "/store/cognee.lancedb", cleanup_older_than=timedelta(hours=1)
    )

    by_name = {r.name: r for r in results}
    assert by_name["broken.lance"].error is not None
    assert "boom" in by_name["broken.lance"].error
    # The healthy table was still optimized.
    assert by_name["healthy.lance"].error is None
    assert ok.optimize_calls == [timedelta(hours=1)]


@pytest.mark.asyncio
async def test_run_once_optimizes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """run_once acquires the (free) write lock and optimizes the store."""
    store = tmp_path / "cognee.lancedb"
    store.mkdir()
    tables = _big_tables()
    _install_fake_lancedb(monkeypatch, tables)
    sched = LanceDbMaintenanceScheduler(
        store_path=store,
        write_lock=asyncio.Lock(),
        interval_seconds=3600,
        cleanup_older_than=timedelta(hours=1),
    )

    results = await sched.run_once()

    assert len(results) == len(tables)
    for table in tables.values():
        assert table.optimize_calls == [timedelta(hours=1)]


@pytest.mark.asyncio
async def test_run_once_skips_when_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pass is skipped (no connect) while a write holds the lock."""
    store = tmp_path / "cognee.lancedb"
    store.mkdir()
    tables = _big_tables()
    connected = _install_fake_lancedb(monkeypatch, tables)
    lock = asyncio.Lock()
    sched = LanceDbMaintenanceScheduler(
        store_path=store,
        write_lock=lock,
        interval_seconds=3600,
        cleanup_older_than=timedelta(hours=1),
    )

    await lock.acquire()
    try:
        results = await sched.run_once()
    finally:
        lock.release()

    assert results == []
    assert connected == []
    for table in tables.values():
        assert table.optimize_calls == []


@pytest.mark.asyncio
async def test_run_once_skips_when_store_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-existent store is skipped without touching lancedb."""
    connected = _install_fake_lancedb(monkeypatch, {})
    sched = LanceDbMaintenanceScheduler(
        store_path=tmp_path / "does-not-exist.lancedb",
        write_lock=asyncio.Lock(),
        interval_seconds=3600,
        cleanup_older_than=timedelta(hours=1),
    )

    assert await sched.run_once() == []
    assert connected == []


@pytest.mark.asyncio
async def test_run_once_never_raises_on_optimize_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A store-open failure is logged and swallowed (server keeps running)."""
    store = tmp_path / "cognee.lancedb"
    store.mkdir()

    def _boom(*_a: Any, **_k: Any) -> list[TableMaintenanceResult]:
        raise RuntimeError("store open failed")

    monkeypatch.setattr(maintenance_module, "optimize_lancedb_store", _boom)
    sched = LanceDbMaintenanceScheduler(
        store_path=store,
        write_lock=asyncio.Lock(),
        interval_seconds=3600,
        cleanup_older_than=timedelta(hours=1),
    )

    assert await sched.run_once() == []


@pytest.mark.asyncio
async def test_loop_runs_then_sleeps_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The loop runs a pass immediately then sleeps the configured interval."""
    sched = LanceDbMaintenanceScheduler(
        store_path=tmp_path,
        write_lock=asyncio.Lock(),
        interval_seconds=777,
        cleanup_older_than=timedelta(hours=1),
    )
    runs = 0
    sleeps: list[float] = []

    async def _fake_run_once() -> list[TableMaintenanceResult]:
        nonlocal runs
        runs += 1
        return []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(sched, "run_once", _fake_run_once)
    monkeypatch.setattr(maintenance_module.asyncio, "sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await sched._loop()

    assert runs == 2
    assert sleeps == [777, 777]


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_cancels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Start launches one task; a second start is a no-op; stop cancels it."""
    store = tmp_path / "cognee.lancedb"
    store.mkdir()
    _install_fake_lancedb(monkeypatch, _big_tables())
    sched = LanceDbMaintenanceScheduler(
        store_path=store,
        write_lock=asyncio.Lock(),
        interval_seconds=3600,
        cleanup_older_than=timedelta(hours=1),
    )

    sched.start()
    first_task = sched._task
    sched.start()
    assert sched._task is first_task
    await sched.stop()
    assert sched._task is None
    assert (first_task is not None and first_task.cancelled()) or first_task.done()


def _enabled_memory_settings(data_dir: str, **overrides: Any) -> MemorySettings:
    return MemorySettings(
        enabled=True,
        data_dir=data_dir,
        llm=MemoryLlmSettings(),
        embedding=MemoryEmbeddingSettings(endpoint="http://box:11434/v1"),
        **overrides,
    )


@pytest.mark.asyncio
async def test_cognee_start_maintenance_uses_write_lock_and_store_path(
    tmp_path: Path,
) -> None:
    """CogneeMemory wires the scheduler to its write lock and LanceDB store."""
    mem = CogneeMemory(_enabled_memory_settings(str(tmp_path)))
    mem.start_maintenance()
    try:
        sched = mem._maintenance
        assert sched is not None
        assert sched._write_lock is mem._write_lock
        expected = (tmp_path / LANCEDB_RELATIVE_PATH).resolve()
        assert sched.store_path == expected
        assert sched.interval_seconds == 21600.0
        assert sched.cleanup_older_than == timedelta(seconds=3600.0)
    finally:
        await mem.stop_maintenance()
    assert mem._maintenance is None


@pytest.mark.asyncio
async def test_cognee_start_maintenance_noop_when_disabled(tmp_path: Path) -> None:
    """No scheduler is created when maintenance is disabled."""
    mem = CogneeMemory(
        _enabled_memory_settings(str(tmp_path), maintenance_enabled=False)
    )
    mem.start_maintenance()
    assert mem._maintenance is None
    # stop is safe even when nothing started.
    await mem.stop_maintenance()


# ---------------------------------------------------------------------------
# cognee_db VACUUM maintenance
# ---------------------------------------------------------------------------


def _make_cognee_db(db_path: Path, *, auto_vacuum: int = 2) -> None:
    """Create a populated SQLite ``cognee_db`` (WAL off, journal delete).

    ``auto_vacuum`` is set BEFORE any table is created (SQLite only honours it
    at table-creation time), mirroring how the mode selection in
    :func:`vacuum_cognee_db` reads ``PRAGMA auto_vacuum``.
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA auto_vacuum={auto_vacuum}")
        conn.execute(
            "CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY, payload TEXT)"
        )
        conn.executemany(
            "INSERT INTO pipeline_runs (payload) VALUES (?)",
            [(f"row-{i}",) for i in range(2000)],
        )
        conn.commit()
    finally:
        conn.close()


def _delete_rows(db_path: Path, n: int = 1500) -> None:
    """Delete rows without reclaiming space (simulates retention pruning)."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DELETE FROM pipeline_runs WHERE id <= ?", (n,))
        conn.commit()
    finally:
        conn.close()


def _freelist(db_path: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    finally:
        conn.close()


def test_vacuum_incremental_reclaims_freelist_when_auto_vacuum(
    tmp_path: Path,
) -> None:
    """incremental_vacuum reclaims freelist pages freed by row deletion."""
    db = tmp_path / "cognee_db"
    _make_cognee_db(db, auto_vacuum=2)
    _delete_rows(db)
    assert _freelist(db) > 0

    result = vacuum_cognee_db(db, mode="incremental_vacuum")

    assert result.error is None
    assert result.mode == "incremental_vacuum"
    # Tail freelist pages were reclaimed (freelist shrank, file shrank).
    assert result.freelist_before > 0
    assert result.freelist_after < result.freelist_before
    assert result.size_after <= result.size_before


def test_vacuum_incremental_falls_back_to_full_when_auto_vacuum_none(
    tmp_path: Path,
) -> None:
    """With auto_vacuum=NONE, incremental is a no-op so it falls back to VACUUM."""
    db = tmp_path / "cognee_db"
    _make_cognee_db(db, auto_vacuum=0)
    _delete_rows(db)
    size_before = db.stat().st_size
    assert _freelist(db) > 0

    result = vacuum_cognee_db(db, mode="incremental_vacuum")

    assert result.error is None
    # Full VACUUM reclaims the entire freelist and shrinks the file.
    assert result.freelist_after == 0
    assert result.size_after < size_before


def test_vacuum_full_shrinks_file_after_prune(tmp_path: Path) -> None:
    """Full VACUUM returns freed pages to disk and shrinks the file."""
    db = tmp_path / "cognee_db"
    _make_cognee_db(db, auto_vacuum=0)
    _delete_rows(db)
    size_before = db.stat().st_size
    assert _freelist(db) > 0

    result = vacuum_cognee_db(db, mode="vacuum")

    assert result.error is None
    assert result.freelist_after == 0
    assert result.size_after < size_before


def test_vacuum_logs_and_captures_error_on_missing_file(tmp_path: Path) -> None:
    """A missing/unopenable store is captured in the result, never raises."""
    missing = tmp_path / "no-such-cognee_db"
    result = vacuum_cognee_db(missing, mode="incremental_vacuum")
    assert result.error is not None
    assert result.size_before == 0
    assert result.size_after == 0


@pytest.mark.asyncio
async def test_vacuum_run_once_runs_inside_window(tmp_path: Path) -> None:
    """run_once vacuums when inside the off-peak window."""
    db = tmp_path / "cognee_db"
    _make_cognee_db(db, auto_vacuum=2)
    _delete_rows(db)
    sched = CogneeDbVacuumScheduler(
        db_path=db,
        write_lock=asyncio.Lock(),
        interval_seconds=3600,
        mode="incremental_vacuum",
        off_peak_window=(2, 6),
    )
    inside = sched._in_off_peak_window()
    if not inside:
        sched.off_peak_window = None

    result = await sched.run_once()

    assert result is not None
    assert result.error is None
    # Incremental reclaims at least the tail freelist pages.
    assert result.freelist_after < result.freelist_before


@pytest.mark.asyncio
async def test_vacuum_run_once_skips_when_writing(tmp_path: Path) -> None:
    """A pass is skipped (no vacuum) while a write holds the lock."""
    db = tmp_path / "cognee_db"
    _make_cognee_db(db, auto_vacuum=2)
    lock = asyncio.Lock()
    sched = CogneeDbVacuumScheduler(
        db_path=db,
        write_lock=lock,
        interval_seconds=3600,
        mode="incremental_vacuum",
    )
    await lock.acquire()
    try:
        result = await sched.run_once()
    finally:
        lock.release()
    assert result is None


@pytest.mark.asyncio
async def test_vacuum_run_once_skips_when_store_missing(tmp_path: Path) -> None:
    """A non-existent store is skipped without touching sqlite."""
    sched = CogneeDbVacuumScheduler(
        db_path=tmp_path / "does-not-exist",
        write_lock=asyncio.Lock(),
        interval_seconds=3600,
        mode="incremental_vacuum",
    )
    assert await sched.run_once() is None


@pytest.mark.asyncio
async def test_vacuum_run_once_skips_outside_window(tmp_path: Path) -> None:
    """run_once returns None when the current UTC hour is outside the window."""
    db = tmp_path / "cognee_db"
    _make_cognee_db(db, auto_vacuum=2)
    from datetime import datetime

    now = datetime.now(UTC)
    # A window that cannot contain the current hour.
    sched = CogneeDbVacuumScheduler(
        db_path=db,
        write_lock=asyncio.Lock(),
        interval_seconds=3600,
        mode="incremental_vacuum",
        off_peak_window=((now.hour + 1) % 24, (now.hour + 2) % 24),
    )
    assert not sched._in_off_peak_window()
    assert await sched.run_once() is None


@pytest.mark.asyncio
async def test_vacuum_loop_sleeps_to_window_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Outside the window the loop sleeps until it opens (never vacuums)."""
    from datetime import datetime

    sched = CogneeDbVacuumScheduler(
        db_path=tmp_path / "cognee_db",
        write_lock=asyncio.Lock(),
        interval_seconds=777,
        mode="incremental_vacuum",
        off_peak_window=(23, 24),  # 23:00-24:00 UTC — usually not now
    )
    runs = 0
    sleeps: list[float] = []

    async def _fake_run_once() -> CogneeDbVacuumResult | None:
        nonlocal runs
        runs += 1
        return None

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(sched, "run_once", _fake_run_once)
    monkeypatch.setattr(maintenance_module.asyncio, "sleep", _fake_sleep)

    now = datetime.now(UTC)
    with pytest.raises(asyncio.CancelledError):
        await sched._loop()

    if sched._in_off_peak_window(now):
        # 23:00-24:00 happens to be now — the pass ran and slept the interval.
        assert runs == 1
        assert sleeps == [777]
    else:
        assert runs == 0
        assert sleeps and sleeps[0] == sched._seconds_until_window_open(now)


@pytest.mark.asyncio
async def test_cognee_start_maintenance_starts_vacuum_scheduler(
    tmp_path: Path,
) -> None:
    """CogneeMemory wires the vacuum scheduler to its write lock and DB path."""
    mem = CogneeMemory(_enabled_memory_settings(str(tmp_path)))
    mem.start_maintenance()
    try:
        sched = mem._vacuum
        assert sched is not None
        assert sched._write_lock is mem._write_lock
        expected = (tmp_path / COGNEE_DB_RELATIVE_PATH).resolve()
        assert sched.db_path == expected
        assert sched.interval_seconds == 21600.0
        assert sched.mode == "incremental_vacuum"
        assert sched.off_peak_window == (2, 6)
    finally:
        await mem.stop_maintenance()
    assert mem._vacuum is None
    assert mem._maintenance is None


@pytest.mark.asyncio
async def test_cognee_start_maintenance_vacuum_noop_when_disabled(
    tmp_path: Path,
) -> None:
    """No vacuum scheduler is created when vacuum maintenance is disabled."""
    mem = CogneeMemory(
        _enabled_memory_settings(str(tmp_path), maintenance_vacuum_enabled=False)
    )
    mem.start_maintenance()
    assert mem._vacuum is None
    # The LanceDB scheduler still starts (its own toggle is separate).
    assert mem._maintenance is not None
    await mem.stop_maintenance()


# ---------------------------------------------------------------------------
# Retention-pruning guard: pruning may only touch the relational bookkeeping
# tables (pipeline_runs / results / queries) — never the memory graph tables
# (Entity nodes / EdgeType edges).  No retention-pruning job exists in the
# codebase yet; these tests pin the contract a future one MUST honour by
# exercising a mock pruning function.
# ---------------------------------------------------------------------------

# cognee's SQLite bookkeeping tables — safe to prune.
_RELATIONAL_TABLES = ("pipeline_runs", "results", "queries")
# cognee's memory graph tables — nodes and edges; pruning must NEVER touch them.
_MEMORY_TABLES = ("Entity", "EdgeType")


def _make_multi_table_cognee_db(db_path: Path, *, auto_vacuum: int = 2) -> None:
    """Create a ``cognee_db`` with both relational and memory node/edge tables.

    Relational (bookkeeping) tables get 2000 rows each; the memory graph tables
    (``Entity`` nodes, ``EdgeType`` edges) get 500 rows each.  ``auto_vacuum``
    is set before any table is created (SQLite only honours it then).
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA auto_vacuum={auto_vacuum}")
        for name in _RELATIONAL_TABLES:
            conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, payload TEXT)")
            conn.executemany(
                f"INSERT INTO {name} (payload) VALUES (?)",  # noqa: S608
                [(f"row-{i}",) for i in range(2000)],
            )
        for name in _MEMORY_TABLES:
            conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, payload TEXT)")
            conn.executemany(
                f"INSERT INTO {name} (payload) VALUES (?)",  # noqa: S608
                [(f"node-{i}",) for i in range(500)],
            )
        conn.commit()
    finally:
        conn.close()


def _row_count(db_path: Path, table: str) -> int:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        query = f"SELECT count(*) FROM {table}"  # noqa: S608
        return int(conn.execute(query).fetchone()[0])
    finally:
        conn.close()


def _prune_relational_tables(
    db_path: Path,
    *,
    n: int = 1500,
    tables: tuple[str, ...] = _RELATIONAL_TABLES,
) -> None:
    """Mock retention-pruning: delete rows ONLY from bookkeeping tables.

    Represents the contract a future retention-pruning job must honour — it
    refuses (raises) if asked to target a memory node/edge table, and only ever
    deletes rows whose ``id`` is ``<= n`` from the named relational tables.
    """
    assert all(t not in _MEMORY_TABLES for t in tables), (
        "retention pruning must never target memory node/edge tables"
    )
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE id <= ?", (n,))  # noqa: S608
        conn.commit()
    finally:
        conn.close()


def test_retention_pruning_only_touches_relational_tables(tmp_path: Path) -> None:
    """Pruning removes rows from bookkeeping tables; memory rows are untouched."""
    db = tmp_path / "cognee_db"
    _make_multi_table_cognee_db(db)
    memory_before = {t: _row_count(db, t) for t in _MEMORY_TABLES}

    _prune_relational_tables(db, n=1500)

    for table in _RELATIONAL_TABLES:
        assert _row_count(db, table) == 500  # 2000 - 1500 pruned
    for table in _MEMORY_TABLES:
        assert _row_count(db, table) == memory_before[table]


def test_retention_pruning_refuses_to_target_memory_tables(tmp_path: Path) -> None:
    """A pruning call that names a memory node/edge table raises and deletes 0."""
    db = tmp_path / "cognee_db"
    _make_multi_table_cognee_db(db)

    with pytest.raises(AssertionError, match="memory node/edge"):
        _prune_relational_tables(db, tables=("Entity",))

    # The guard fires before any DELETE — memory graph is intact.
    for table in _MEMORY_TABLES:
        assert _row_count(db, table) == 500


def test_vacuum_after_pruning_preserves_memory_rows(tmp_path: Path) -> None:
    """VACUUM reclaims pruned relational space without touching memory rows."""
    db = tmp_path / "cognee_db"
    _make_multi_table_cognee_db(db, auto_vacuum=0)
    memory_before = {t: _row_count(db, t) for t in _MEMORY_TABLES}
    _prune_relational_tables(db, n=1500)
    size_before = db.stat().st_size

    result = vacuum_cognee_db(db, mode="vacuum")

    assert result.error is None
    assert result.freelist_after == 0
    assert result.size_after < size_before
    # Memory node/edge rows survived both the prune and the vacuum rebuild.
    for table in _MEMORY_TABLES:
        assert _row_count(db, table) == memory_before[table]


# ---------------------------------------------------------------------------
# Memory node/edge protection: LanceDB compaction must never change the row
# count of the Entity (node) / EdgeType (edge) vector tables.
# ---------------------------------------------------------------------------

# The memory graph LanceDB tables produced by cognee.
_MEMORY_LANCE_TABLES = ("EdgeType_relationship_name.lance", "Entity_name.lance")


def test_optimize_preserves_memory_node_edge_row_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """optimize_lancedb_store leaves Entity/EdgeType row counts unchanged."""
    tables = _big_tables()
    rows_before = {name: table.rows for name, table in tables.items()}
    _install_fake_lancedb(monkeypatch, tables)

    results = optimize_lancedb_store(
        "/store/cognee.lancedb", cleanup_older_than=timedelta(hours=1)
    )

    by_name = {r.name: r for r in results}
    for name in _MEMORY_LANCE_TABLES:
        result = by_name[name]
        assert result.error is None
        # The result records an invariant row count...
        assert result.rows_before == result.rows_after == rows_before[name]
        # ...and the underlying table was not mutated in row count.
        assert tables[name].rows == rows_before[name]


# ---------------------------------------------------------------------------
# Concurrent maintenance + write availability: maintenance must never
# permanently block memory writes — a write succeeds immediately once a pass
# finishes, and any backlog of writes queued behind an in-flight pass drains.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_succeeds_immediately_after_maintenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Once a maintenance pass completes the write lock is free for writers."""
    store = tmp_path / "cognee.lancedb"
    store.mkdir()
    _install_fake_lancedb(monkeypatch, _big_tables())
    lock = asyncio.Lock()
    sched = LanceDbMaintenanceScheduler(
        store_path=store,
        write_lock=lock,
        interval_seconds=3600,
        cleanup_older_than=timedelta(hours=1),
    )

    await sched.run_once()

    # The pass released the lock — a write acquires without blocking.
    assert not lock.locked()

    async def _write() -> str:
        async with lock:
            return "written"

    assert await asyncio.wait_for(_write(), timeout=1.0) == "written"


@pytest.mark.asyncio
async def test_backlog_writes_drain_after_maintenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Writes queued behind an in-flight maintenance pass all drain afterward."""
    store = tmp_path / "cognee.lancedb"
    store.mkdir()
    lock = asyncio.Lock()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    maintenance_holding_lock = asyncio.Event()

    def _blocking_optimize(*_a: Any, **_k: Any) -> list[TableMaintenanceResult]:
        # Runs in the to_thread worker while the pass holds the write lock.
        loop.call_soon_threadsafe(maintenance_holding_lock.set)
        assert release.wait(timeout=5.0), "maintenance was never released"
        return []

    monkeypatch.setattr(
        maintenance_module, "optimize_lancedb_store", _blocking_optimize
    )
    sched = LanceDbMaintenanceScheduler(
        store_path=store,
        write_lock=lock,
        interval_seconds=3600,
        cleanup_older_than=timedelta(hours=1),
    )

    drained: list[int] = []

    async def _writer(i: int) -> None:
        async with lock:
            drained.append(i)

    maint = asyncio.create_task(sched.run_once())
    await asyncio.wait_for(maintenance_holding_lock.wait(), timeout=2.0)

    # Queue a backlog of writes while maintenance holds the lock.
    writers = [asyncio.create_task(_writer(i)) for i in range(3)]
    await asyncio.sleep(0)  # let the writers block on the held lock
    assert drained == []  # nothing drains while maintenance holds the lock

    release.set()  # let the pass finish and release the lock
    await asyncio.wait_for(maint, timeout=2.0)
    await asyncio.wait_for(asyncio.gather(*writers), timeout=2.0)

    assert sorted(drained) == [0, 1, 2]  # every backlog write drained


@pytest.mark.asyncio
async def test_vacuum_write_succeeds_immediately_after_pass(tmp_path: Path) -> None:
    """A write acquires the lock immediately after a vacuum pass completes."""
    db = tmp_path / "cognee_db"
    _make_multi_table_cognee_db(db, auto_vacuum=2)
    lock = asyncio.Lock()
    sched = CogneeDbVacuumScheduler(
        db_path=db,
        write_lock=lock,
        interval_seconds=3600,
        mode="incremental_vacuum",
        off_peak_window=None,
    )

    result = await sched.run_once()
    assert result is not None and result.error is None
    assert not lock.locked()

    async def _write() -> str:
        async with lock:
            return "written"

    assert await asyncio.wait_for(_write(), timeout=1.0) == "written"


# ---------------------------------------------------------------------------
# consolidated store-size metrics
# ---------------------------------------------------------------------------


def _make_store_layout(
    data_dir: Path,
    *,
    with_lancedb: bool = True,
) -> None:
    """Build a minimal cognee store tree under ``<data_dir>/system/databases``.

    Creates a cognee_db SQLite file with the three metric tables, a stand-in
    ladybug graph file, and (optionally) a LanceDB-shaped table directory with
    one fragment data file and one deletion file.
    """
    import sqlite3

    databases = data_dir / "system" / "databases"
    databases.mkdir(parents=True, exist_ok=True)

    db = databases / "cognee_db"
    conn = sqlite3.connect(str(db))
    try:
        for table in COGNEE_DB_METRIC_TABLES:
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, payload TEXT)")
            conn.executemany(
                f"INSERT INTO {table} (payload) VALUES (?)",  # noqa: S608
                [(f"{table}-{i}",) for i in range(5)],
            )
        conn.commit()
    finally:
        conn.close()

    (databases / "cognee_graph_ladybug").write_bytes(b"x" * 4096)
    (databases / "cognee_graph_ladybug.wal").write_bytes(b"y" * 256)

    if with_lancedb:
        table_dir = databases / "cognee.lancedb" / "Entity.lance"
        (table_dir / "data").mkdir(parents=True)
        (table_dir / "data" / "frag-1.lance").write_bytes(b"z" * 2048)
        (table_dir / "_deletions").mkdir(parents=True)
        (table_dir / "_deletions" / "del-1.arrow").write_bytes(b"d" * 128)


def test_emit_store_metrics_collects_all_dimensions(tmp_path: Path) -> None:
    """emit_store_metrics reports cognee_db, graph and LanceDB sizes/counts."""
    _make_store_layout(tmp_path)

    metrics = emit_store_metrics(tmp_path, graph_nodes=7, graph_edges=11)

    assert isinstance(metrics, StoreMetrics)
    assert metrics.error is None
    assert metrics.cognee_db_size > 0
    # Per-table row counts for the three named bookkeeping tables.
    assert set(metrics.table_rows) == set(COGNEE_DB_METRIC_TABLES)
    assert all(count == 5 for count in metrics.table_rows.values())
    # Graph store size (main file + wal) and injected node/edge counts.
    assert metrics.graph_size >= 4096 + 256
    assert metrics.graph_nodes == 7
    assert metrics.graph_edges == 11
    # LanceDB total size plus fragment/deletion file counts.
    assert metrics.lancedb_size >= 2048 + 128
    assert metrics.lancedb_fragments == 1
    assert metrics.lancedb_deletion_files == 1


def test_emit_store_metrics_handles_missing_stores(tmp_path: Path) -> None:
    """A wholly-empty data_dir yields zeros and no error (never raises)."""
    metrics = emit_store_metrics(tmp_path)

    assert metrics.error is None
    assert metrics.cognee_db_size == 0
    assert metrics.table_rows == {}
    assert metrics.graph_size == 0
    assert metrics.graph_nodes is None
    assert metrics.graph_edges is None
    assert metrics.lancedb_size == 0
    assert metrics.lancedb_fragments == 0
    assert metrics.lancedb_deletion_files == 0


@pytest.mark.asyncio
async def test_store_metrics_scheduler_uses_graph_provider(tmp_path: Path) -> None:
    """run_once folds the async graph-stats provider into the emitted metrics."""
    _make_store_layout(tmp_path)

    async def _provider() -> tuple[int, int] | None:
        return (3, 9)

    sched = StoreMetricsScheduler(
        data_dir=tmp_path,
        interval_seconds=3600,
        graph_stats_provider=_provider,
    )
    metrics = await sched.run_once()

    assert metrics.graph_nodes == 3
    assert metrics.graph_edges == 9
    assert metrics.cognee_db_size > 0


@pytest.mark.asyncio
async def test_store_metrics_scheduler_survives_provider_error(tmp_path: Path) -> None:
    """A failing graph provider degrades counts to None, never raises."""
    _make_store_layout(tmp_path)

    async def _boom() -> tuple[int, int] | None:
        raise RuntimeError("graph engine unavailable")

    sched = StoreMetricsScheduler(
        data_dir=tmp_path,
        interval_seconds=3600,
        graph_stats_provider=_boom,
    )
    metrics = await sched.run_once()

    assert metrics.graph_nodes is None
    assert metrics.graph_edges is None
    assert metrics.error is None


@pytest.mark.asyncio
async def test_store_metrics_scheduler_start_idempotent_and_stop(
    tmp_path: Path,
) -> None:
    """Start launches one task; a second start is a no-op; stop cancels it."""
    sched = StoreMetricsScheduler(data_dir=tmp_path, interval_seconds=3600)
    sched.start()
    first = sched._task
    sched.start()
    assert sched._task is first
    await sched.stop()
    assert sched._task is None


@pytest.mark.asyncio
async def test_cognee_start_maintenance_starts_metrics_scheduler(
    tmp_path: Path,
) -> None:
    """CogneeMemory wires the metrics scheduler to its resolved data_dir."""
    mem = CogneeMemory(_enabled_memory_settings(str(tmp_path)))
    mem.start_maintenance()
    try:
        sched = mem._metrics
        assert sched is not None
        assert sched.data_dir == tmp_path.resolve()
        assert sched.interval_seconds == 21600.0
        assert sched._graph_stats_provider == mem._graph_stats
    finally:
        await mem.stop_maintenance()
    assert mem._metrics is None


@pytest.mark.asyncio
async def test_cognee_start_maintenance_metrics_noop_when_disabled(
    tmp_path: Path,
) -> None:
    """No metrics scheduler is created when metrics emission is disabled."""
    mem = CogneeMemory(
        _enabled_memory_settings(str(tmp_path), maintenance_metrics_enabled=False)
    )
    mem.start_maintenance()
    try:
        assert mem._metrics is None
        # The other schedulers keep their independent toggles.
        assert mem._maintenance is not None
        assert mem._vacuum is not None
    finally:
        await mem.stop_maintenance()

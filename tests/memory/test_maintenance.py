"""Tests for the periodic LanceDB maintenance scheduler.

``lancedb`` is not installed (it ships only with the ``memory`` extra), so a
fake ``lancedb`` module is injected into :data:`sys.modules`; the fake records
every ``optimize`` call and simulates compaction by collapsing fragment/version
counts while leaving the row count untouched.
"""

from __future__ import annotations

import asyncio
import sys
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
    COGNEE_DB_RELATIVE_PATH,
    LANCEDB_RELATIVE_PATH,
    CogneeDbVacuumResult,
    CogneeDbVacuumScheduler,
    LanceDbMaintenanceScheduler,
    TableMaintenanceResult,
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

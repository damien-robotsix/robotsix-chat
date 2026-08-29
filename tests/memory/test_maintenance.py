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
from datetime import timedelta
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
    LANCEDB_RELATIVE_PATH,
    LanceDbMaintenanceScheduler,
    TableMaintenanceResult,
    optimize_lancedb_store,
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

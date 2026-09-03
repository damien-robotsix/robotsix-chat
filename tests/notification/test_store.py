"""Tests for the persistent, bounded notification store.

Covers the store's append/list/mark-delivered/mark-read helpers and the
bounded retention contract (at most 200 newest events, nothing older than
30 days).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from robotsix_chat.notification.store import (
    MAX_EVENTS,
    READ_RETENTION_DAYS,
    RETENTION_DAYS,
    NotificationStore,
)


def _days_ago(days: float) -> str:
    """Return an ISO-8601 UTC timestamp *days* days in the past."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# append / list
# ---------------------------------------------------------------------------


def test_append_returns_record_with_expected_fields(tmp_path):
    """Appending returns a fully-populated record with default flags unset."""
    store = NotificationStore(tmp_path / "notifications.json")

    record = store.append(
        title="Build failed",
        body="main broke on CI",
        source_session="sess-1",
    )

    assert record.id
    assert record.title == "Build failed"
    assert record.body == "main broke on CI"
    assert record.source_session == "sess-1"
    assert record.delivered is False
    assert record.read is False
    # ISO-8601 UTC timestamp is parsed from the returned string.
    datetime.fromisoformat(record.ts)


def test_list_returns_records_with_expected_fields(tmp_path):
    """List returns full records with all documented fields, newest first."""
    store = NotificationStore(tmp_path / "notifications.json")
    first = store.append(title="one", body="first", source_session="sess-a")
    second = store.append(title="two", body="second", source_session="sess-b")

    records = store.list()
    assert len(records) == 2
    assert [r.id for r in records] == [second.id, first.id]

    newest = records[0]
    assert newest.title == "two"
    assert newest.body == "second"
    assert newest.source_session == "sess-b"
    assert newest.delivered is False
    assert newest.read is False
    assert newest.id == second.id


def test_list_filters_by_session(tmp_path):
    """List filters to a single source session when requested."""
    store = NotificationStore(tmp_path / "notifications.json")
    a = store.append(title="a", body="x", source_session="sess-a")
    store.append(title="b", body="x", source_session="sess-b")
    c = store.append(title="c", body="x", source_session="sess-a")

    records = store.list(session_id="sess-a")
    assert {r.id for r in records} == {a.id, c.id}
    assert all(r.source_session == "sess-a" for r in records)


def test_list_respects_limit(tmp_path):
    """List caps the number of returned records to ``limit``."""
    store = NotificationStore(tmp_path / "notifications.json")
    for i in range(5):
        store.append(title=str(i), body="x", source_session="sess")
    assert len(store.list(limit=3)) == 3


# ---------------------------------------------------------------------------
# bounded retention — count
# ---------------------------------------------------------------------------


def test_append_250_keeps_only_newest_200(tmp_path):
    """Appending 250 events leaves exactly the newest 200 in the store."""
    store = NotificationStore(tmp_path / "notifications.json")
    ids = [
        store.append(title=f"n{i}", body="x", source_session="sess").id
        for i in range(250)
    ]

    records = store.list()
    assert len(records) == MAX_EVENTS == 200
    # The oldest 50 were evicted; the newest 200 remain, newest first.
    assert [r.id for r in records] == list(reversed(ids[50:]))


def test_store_durable_across_reopen_within_bounds(tmp_path):
    """Persisted records survive reopening the store, subject to retention."""
    path = tmp_path / "notifications.json"
    store = NotificationStore(path)
    for i in range(10):
        store.append(title=f"n{i}", body="x", source_session="sess")

    reopened = NotificationStore(path)
    assert len(reopened.list()) == 10


# ---------------------------------------------------------------------------
# bounded retention — age
# ---------------------------------------------------------------------------


def test_event_older_than_30_days_is_removed(tmp_path):
    """Records older than the retention window are dropped on write."""
    store = NotificationStore(tmp_path / "notifications.json")
    old = store.append(
        title="old",
        body="stale",
        source_session="sess",
        ts=_days_ago(RETENTION_DAYS + 1),
    )
    fresh = store.append(
        title="new",
        body="x",
        source_session="sess",
        ts=_days_ago(1),
    )

    records = store.list()
    assert [r.id for r in records] == [fresh.id]
    assert old.id not in {r.id for r in records}


def test_recent_event_within_retention_is_kept(tmp_path):
    """Records inside the retention window are retained, not dropped."""
    store = NotificationStore(tmp_path / "notifications.json")
    recent = store.append(
        title="recent",
        body="x",
        source_session="sess",
        ts=_days_ago(RETENTION_DAYS - 1),
    )
    assert [r.id for r in store.list()] == [recent.id]


# ---------------------------------------------------------------------------
# bounded retention — read lifetime
# ---------------------------------------------------------------------------


def test_mark_read_stamps_read_ts(tmp_path):
    """Marking a record read stamps a ``read_ts`` timestamp."""
    store = NotificationStore(tmp_path / "notifications.json")
    one = store.append(title="one", body="x", source_session="sess")
    assert store.list()[0].read_ts is None

    store.mark_read([one.id])
    read = store.list()[0]
    assert read.read is True
    assert read.read_ts is not None
    datetime.fromisoformat(read.read_ts)


def test_read_record_older_than_read_retention_is_purged(tmp_path):
    """A record read longer than ``read_retention_days`` ago is purged.

    The record is published well within the absolute lifetime, so only the
    read-lifetime rule can drop it.
    """
    path = tmp_path / "notifications.json"
    # Seed a record that was read long ago but published within the
    # absolute lifetime, then reopen so pruning runs on the next write.
    raw = [
        {
            "id": "old-read",
            "ts": _days_ago(RETENTION_DAYS - 5),
            "title": "read-long-ago",
            "body": "b",
            "source_session": "sess",
            "delivered": True,
            "read": True,
            "read_ts": _days_ago(READ_RETENTION_DAYS + 1),
        }
    ]
    path.write_text(json.dumps(raw), encoding="utf-8")

    store = NotificationStore(path)
    # A fresh append triggers _prune, which evaluates the read-lifetime rule.
    fresh = store.append(title="new", body="x", source_session="sess")

    ids = {r.id for r in store.list()}
    assert ids == {fresh.id}
    assert "old-read" not in ids


def test_recently_read_record_within_read_retention_is_kept(tmp_path):
    """A record read within ``read_retention_days`` is retained."""
    path = tmp_path / "notifications.json"
    raw = [
        {
            "id": "recent-read",
            "ts": _days_ago(RETENTION_DAYS - 5),
            "title": "read-recently",
            "body": "b",
            "source_session": "sess",
            "delivered": True,
            "read": True,
            "read_ts": _days_ago(READ_RETENTION_DAYS - 1),
        }
    ]
    path.write_text(json.dumps(raw), encoding="utf-8")

    store = NotificationStore(path)
    store.append(title="new", body="x", source_session="sess")

    assert "recent-read" in {r.id for r in store.list()}


def test_unread_record_not_subject_to_read_retention(tmp_path):
    """An unread record older than the read window survives (still unread)."""
    store = NotificationStore(tmp_path / "notifications.json")
    kept = store.append(
        title="unread",
        body="x",
        source_session="sess",
        ts=_days_ago(READ_RETENTION_DAYS + 5),
    )
    assert kept.id in {r.id for r in store.list()}


def test_size_reports_record_count(tmp_path):
    """``size`` returns the current number of stored records."""
    store = NotificationStore(tmp_path / "notifications.json")
    assert store.size() == 0
    for i in range(3):
        store.append(title=str(i), body="x", source_session="sess")
    assert store.size() == 3


# ---------------------------------------------------------------------------
# mark-delivered / mark-read
# ---------------------------------------------------------------------------


def test_mark_delivered(tmp_path):
    """mark_delivered flips the delivered flag and persists it."""
    store = NotificationStore(tmp_path / "notifications.json")
    one = store.append(title="one", body="x", source_session="sess")
    two = store.append(title="two", body="x", source_session="sess")

    assert store.mark_delivered([one.id]) == 1
    reopened = NotificationStore(tmp_path / "notifications.json")
    by_id = {r.id: r for r in reopened.list()}
    assert by_id[one.id].delivered is True
    assert by_id[two.id].delivered is False
    # Marking again is a no-op change.
    assert store.mark_delivered([one.id]) == 0


def test_mark_read(tmp_path):
    """mark_read flips the read flag and persists it."""
    store = NotificationStore(tmp_path / "notifications.json")
    one = store.append(title="one", body="x", source_session="sess")
    two = store.append(title="two", body="x", source_session="sess")

    assert store.mark_read([one.id, two.id]) == 2
    reopened = NotificationStore(tmp_path / "notifications.json")
    assert all(r.read for r in reopened.list())


def test_mark_unknown_ids_returns_zero(tmp_path):
    """Marking unknown ids is a no-op returning 0."""
    store = NotificationStore(tmp_path / "notifications.json")
    store.append(title="x", body="x", source_session="sess")
    assert store.mark_read(["nope"]) == 0
    assert store.mark_delivered(["nope"]) == 0


# ---------------------------------------------------------------------------
# persistence robustness
# ---------------------------------------------------------------------------


def test_missing_file_starts_empty(tmp_path):
    """A store over a non-existent file starts with no records."""
    store = NotificationStore(tmp_path / "missing" / "notifications.json")
    assert store.list() == []


def test_corrupt_file_starts_empty(tmp_path):
    """A corrupt store file is logged and treated as empty, not a crash."""
    path = tmp_path / "notifications.json"
    path.write_text("{ not json !!", encoding="utf-8")
    store = NotificationStore(path)
    assert store.list() == []


@pytest.mark.parametrize("method", ["mark_delivered", "mark_read"])
def test_publish_then_flag_durable(tmp_path, method):
    """A record appended then flagged survives a full store reopen."""
    path = tmp_path / "notifications.json"
    store = NotificationStore(path)
    record = store.append(title="t", body="b", source_session="sess")
    getattr(store, method)([record.id])

    reopened = NotificationStore(path)
    assert len(reopened.list()) == 1
    flag = "delivered" if method == "mark_delivered" else "read"
    assert getattr(reopened.list()[0], flag)


# ---------------------------------------------------------------------------
# corrupted-record resilience
# ---------------------------------------------------------------------------


def _valid_raw(record_id: str) -> dict[str, object]:
    """Build a fully-populated raw record dict for direct file seeding."""
    return {
        "id": record_id,
        "ts": _days_ago(1),
        "title": "ok",
        "body": "b",
        "source_session": "sess",
        "delivered": False,
        "read": False,
    }


def test_corrupt_records_are_dropped_valid_ones_survive(tmp_path):
    """A mix of valid and corrupt records loads only the valid ones."""
    path = tmp_path / "notifications.json"
    raw = [
        _valid_raw("good-1"),
        {"id": "bad-missing-fields"},  # missing required fields
        "not-a-dict",  # not even an object
        {**_valid_raw("bad-type"), "title": None},  # null required field
        _valid_raw("good-2"),
    ]
    path.write_text(json.dumps(raw), encoding="utf-8")

    store = NotificationStore(path)

    ids = {r.id for r in store.list()}
    assert ids == {"good-1", "good-2"}


def test_non_list_json_starts_empty(tmp_path):
    """A JSON object (not a list) at the store path is treated as empty."""
    path = tmp_path / "notifications.json"
    path.write_text(json.dumps({"unexpected": "shape"}), encoding="utf-8")

    store = NotificationStore(path)

    assert store.list() == []


def test_recovery_after_corruption_persists_clean_file(tmp_path):
    """After dropping corrupt records, a new append rewrites a clean file."""
    path = tmp_path / "notifications.json"
    raw = [_valid_raw("good"), {"id": "corrupt"}]
    path.write_text(json.dumps(raw), encoding="utf-8")

    store = NotificationStore(path)
    fresh = store.append(title="new", body="b", source_session="sess")

    # Reopening reads the rewritten file: only the surviving valid record
    # plus the freshly appended one, no corruption re-introduced.
    reopened = NotificationStore(path)
    assert {r.id for r in reopened.list()} == {"good", fresh.id}


def test_list_survives_corrupt_records(tmp_path):
    """``list`` never raises on a corrupt file, enabling graceful API replies."""
    path = tmp_path / "notifications.json"
    path.write_text(json.dumps([{"id": "corrupt"}]), encoding="utf-8")

    store = NotificationStore(path)

    # No exception — a corrupt backing file yields an empty listing, which
    # the unread API serialises as an empty page rather than a 500.
    assert store.list() == []

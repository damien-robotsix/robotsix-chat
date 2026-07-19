"""Tests for the ``SubsessionRegistry`` (state, inboxes, SSE, persistence)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from robotsix_chat.chat.events import (
    SSE_SUBSESSION_CLOSED_TYPE,
    SSE_SUBSESSION_FAILED_TYPE,
    SSE_SUBSESSION_MESSAGE_TYPE,
    SSE_SUBSESSION_STARTED_TYPE,
    SSE_SUBSESSION_UPDATED_TYPE,
)
from robotsix_chat.subsessions import (
    SubsessionInfo,
    SubsessionKind,
    SubsessionRegistry,
    SubsessionStatus,
)
from tests.common.subsession_fakes import FakeClock, RecordingSink


def _create(
    registry: SubsessionRegistry,
    *,
    owner: str = "sess-1",
    kind: SubsessionKind = SubsessionKind.TASK,
    parent_id: str | None = None,
    title: str = "job",
    **kwargs: object,
) -> SubsessionInfo:
    """Register a subsession with sensible defaults."""
    return registry.create(
        kind=kind,
        owner_session_id=owner,
        parent_id=parent_id,
        depth=1,
        title=title,
        prompt="do the thing",
        model_level=3,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# create / set_status
# ---------------------------------------------------------------------------


def test_create_publishes_started_frame_to_owner() -> None:
    """``create`` publishes a ``subsession_started`` frame to the owner."""
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)

    info = _create(registry, owner="sess-A")

    assert len(sink.frames) == 1
    session_id, frame = sink.frames[0]
    assert session_id == "sess-A"
    assert frame["type"] == SSE_SUBSESSION_STARTED_TYPE
    assert frame["subsession_id"] == info.id
    assert frame["status"] == SubsessionStatus.RUNNING.value
    assert frame["kind"] == SubsessionKind.TASK.value


def test_set_status_publishes_updated_frame() -> None:
    """``set_status`` mutates scheduling fields and publishes an update."""
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)

    registry.set_status(
        info.id,
        SubsessionStatus.SLEEPING,
        runs=2,
        next_run_at=500.0,
        last_result="ok",
    )

    assert info.status is SubsessionStatus.SLEEPING
    assert info.runs == 2
    assert info.next_run_at == 500.0
    assert info.last_result == "ok"

    _, frame = sink.of_type(SSE_SUBSESSION_UPDATED_TYPE)[-1]
    assert frame["subsession_id"] == info.id
    assert frame["status"] == SubsessionStatus.SLEEPING.value
    assert frame["runs"] == 2
    assert frame["next_run_at"] == 500.0
    assert frame["last_result"] == "ok"


def test_set_status_refuses_reviving_terminal_entries() -> None:
    """A terminal subsession cannot be flipped back to an active status."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry)
    registry.mark_closed(info.id, summary="done", reason="completed")

    registry.set_status(info.id, SubsessionStatus.RUNNING)

    assert info.status is SubsessionStatus.CLOSED


def test_set_status_unknown_id_is_noop() -> None:
    """``set_status`` for an unknown id does not raise or publish."""
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)

    registry.set_status("ghost", SubsessionStatus.RUNNING)

    assert sink.frames == []


# ---------------------------------------------------------------------------
# transcript / inbox
# ---------------------------------------------------------------------------


def test_append_transcript_caps_entries_and_publishes() -> None:
    """The transcript is capped at ``transcript_max_entries``, newest kept."""
    sink = RecordingSink()
    registry = SubsessionRegistry(
        event_sink=sink, store_path=None, transcript_max_entries=3
    )
    info = _create(registry)

    for i in range(5):
        registry.append_transcript(info.id, "assistant", f"line {i}")

    assert [e.text for e in info.transcript] == ["line 2", "line 3", "line 4"]
    message_frames = sink.of_type(SSE_SUBSESSION_MESSAGE_TYPE)
    assert len(message_frames) == 5
    _, last = message_frames[-1]
    assert last["subsession_id"] == info.id
    assert last["role"] == "assistant"
    assert last["text"] == "line 4"


def test_append_turn_history_caps_entries_and_persists(tmp_path: Path) -> None:
    """turn_history is capped at _MAX_TURN_HISTORY_ENTRIES, newest kept."""
    from robotsix_chat.subsessions.registry import _MAX_TURN_HISTORY_ENTRIES

    store_path = tmp_path / "subsessions.json"
    registry = SubsessionRegistry(store_path=store_path)
    info = _create(registry)

    for i in range(_MAX_TURN_HISTORY_ENTRIES + 5):
        registry.append_turn_history(info.id, f"in {i}", f"out {i}")

    assert len(info.turn_history) == _MAX_TURN_HISTORY_ENTRIES
    assert info.turn_history[0] == ("in 5", "out 5")
    assert info.turn_history[-1] == (
        f"in {_MAX_TURN_HISTORY_ENTRIES + 4}",
        f"out {_MAX_TURN_HISTORY_ENTRIES + 4}",
    )

    # Persisted as list-of-lists (JSON has no tuples).
    raw = json.loads(store_path.read_text())
    entry = next(e for e in raw if e["subsession_id"] == info.id)
    assert entry["turn_history"][0] == ["in 5", "out 5"]


def test_append_turn_history_unknown_id_is_noop() -> None:
    """``append_turn_history`` for an unknown id does not raise."""
    registry = SubsessionRegistry(store_path=None)
    registry.append_turn_history("ghost", "in", "out")  # no error


def test_enqueue_message_unknown_or_terminal_returns_false() -> None:
    """Messages cannot be queued for unknown or terminal subsessions."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry)
    registry.mark_closed(info.id, summary="done", reason="completed")

    assert registry.enqueue_message("ghost", "user", "hi") is False
    assert registry.enqueue_message(info.id, "user", "hi") is False


def test_enqueue_message_transcripts_immediately_and_wakes() -> None:
    """A queued message is transcripted at once and sets the wake event."""
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)
    info = _create(registry)

    assert registry.enqueue_message(info.id, "user", "steer this way") is True

    assert [e.text for e in info.transcript] == ["steer this way"]
    assert info.transcript[0].role == "user"
    _, frame = sink.of_type(SSE_SUBSESSION_MESSAGE_TYPE)[-1]
    assert frame["text"] == "steer this way"
    assert registry._wake_events[info.id].is_set()


def test_drain_inbox_returns_and_clears_messages() -> None:
    """``drain_inbox`` empties the inbox and resets the wake event."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry)
    registry.enqueue_message(info.id, "user", "one")
    registry.enqueue_message(info.id, "parent", "two")

    messages = registry.drain_inbox(info.id)

    assert [(m.role, m.text) for m in messages] == [("user", "one"), ("parent", "two")]
    assert registry.drain_inbox(info.id) == []
    assert not registry._wake_events[info.id].is_set()


@pytest.mark.asyncio
async def test_wait_for_inbox_times_out_false() -> None:
    """``wait_for_inbox`` returns False when no message arrives in time."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry)

    assert await registry.wait_for_inbox(info.id, timeout=0.01) is False


@pytest.mark.asyncio
async def test_wait_for_inbox_woken_by_message_true() -> None:
    """``wait_for_inbox`` returns True when a message wakes it."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry)

    waiter = asyncio.create_task(registry.wait_for_inbox(info.id, timeout=2.0))
    await asyncio.sleep(0.01)
    registry.enqueue_message(info.id, "user", "wake up")

    assert await waiter is True


# ---------------------------------------------------------------------------
# terminal transitions
# ---------------------------------------------------------------------------


def test_mark_closed_only_once() -> None:
    """The first ``mark_closed`` wins; a second call returns None."""
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)
    info = _create(registry)

    closed = registry.mark_closed(info.id, summary="all done", reason="completed")

    assert closed is info
    assert info.status is SubsessionStatus.CLOSED
    assert info.summary == "all done"
    assert info.close_reason == "completed"
    _, frame = sink.of_type(SSE_SUBSESSION_CLOSED_TYPE)[-1]
    assert frame["subsession_id"] == info.id
    assert frame["reason"] == "completed"
    assert frame["closed_by"] == "agent"

    assert registry.mark_closed(info.id, summary="again", reason="completed") is None
    assert info.summary == "all done"


@pytest.mark.asyncio
async def test_cancel_and_close_cancels_task_and_builds_summary() -> None:
    """``cancel_and_close`` cancels the worker and summarises the last state."""
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)
    info = _create(registry)
    registry.append_transcript(info.id, "assistant", "step 5 done")

    task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(30))
    registry.attach_task(info.id, task)

    closed = registry.cancel_and_close(
        info.id, reason="closed by user", closed_by="user"
    )

    assert closed is info
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "closed by user"
    assert info.summary is not None
    assert info.summary.startswith("Closed by user.")
    assert "Last state: step 5 done" in info.summary
    _, frame = sink.of_type(SSE_SUBSESSION_CLOSED_TYPE)[-1]
    assert frame["closed_by"] == "user"

    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()

    # Idempotent: a second call is a no-op.
    assert registry.cancel_and_close(info.id, reason="again", closed_by="user") is None


def test_fail_sets_failed_state_and_summary() -> None:
    """``fail`` records the error and publishes a ``subsession_failed`` frame."""
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)
    info = _create(registry)
    registry.append_transcript(info.id, "assistant", "made progress")

    failed = registry.fail(info.id, error="boom")

    assert failed is info
    assert info.status is SubsessionStatus.FAILED
    assert info.error == "boom"
    assert info.summary is not None
    assert info.summary.startswith("Failed: boom")
    assert "Last state: made progress" in info.summary
    _, frame = sink.of_type(SSE_SUBSESSION_FAILED_TYPE)[-1]
    assert frame["subsession_id"] == info.id
    assert frame["error"] == "boom"

    # Terminal → a second fail is a no-op.
    assert registry.fail(info.id, error="again") is None


def test_mark_interrupted_sets_terminal_state() -> None:
    """``mark_interrupted`` publishes a closed frame with system attribution."""
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)
    info = _create(registry)

    interrupted = registry.mark_interrupted(info.id, summary="restart happened")

    assert interrupted is info
    assert info.status is SubsessionStatus.INTERRUPTED
    assert info.close_reason == "interrupted"
    assert info.summary == "restart happened"
    _, frame = sink.of_type(SSE_SUBSESSION_CLOSED_TYPE)[-1]
    assert frame["reason"] == "interrupted"
    assert frame["closed_by"] == "system"


def test_close_all_for_owner_counts_only_active() -> None:
    """``close_all_for_owner`` closes active entries and skips terminal ones."""
    registry = SubsessionRegistry(store_path=None)
    a = _create(registry, owner="sess-X")
    b = _create(registry, owner="sess-X")
    c = _create(registry, owner="sess-X")
    _create(registry, owner="sess-other")
    registry.mark_closed(c.id, summary="done", reason="completed")

    closed = registry.close_all_for_owner("sess-X", reason="session closed")

    assert closed == 2
    assert a.status is SubsessionStatus.CLOSED
    assert b.status is SubsessionStatus.CLOSED
    other = registry.list_for_owner("sess-other")[0]
    assert other.is_active


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def test_list_for_owner_sorted_by_created_at() -> None:
    """``list_for_owner`` returns the owner's tree oldest-first."""
    clock = FakeClock()
    registry = SubsessionRegistry(store_path=None, clock=clock)
    first = _create(registry, owner="sess-1", title="first")
    clock.advance(10.0)
    second = _create(registry, owner="sess-1", title="second")
    clock.advance(10.0)
    third = _create(registry, owner="sess-1", title="third")
    _create(registry, owner="sess-2", title="foreign")

    infos = registry.list_for_owner("sess-1")

    assert [i.id for i in infos] == [first.id, second.id, third.id]


def test_list_descendants_is_transitive() -> None:
    """``list_descendants`` returns children and grandchildren, not siblings."""
    registry = SubsessionRegistry(store_path=None)
    root = _create(registry, owner="sess-1", title="root")
    child = _create(registry, owner="sess-1", parent_id=root.id, title="child")
    grandchild = _create(
        registry, owner="sess-1", parent_id=child.id, title="grandchild"
    )
    sibling = _create(registry, owner="sess-1", title="sibling")

    descendants = {i.id for i in registry.list_descendants(root.id)}

    assert descendants == {child.id, grandchild.id}
    assert sibling.id not in descendants
    assert registry.list_descendants("ghost") == []


def test_count_active_ignores_terminal_entries() -> None:
    """``count_active`` counts running/waiting/sleeping entries only."""
    registry = SubsessionRegistry(store_path=None)
    a = _create(registry)
    _create(registry)
    registry.mark_closed(a.id, summary="done", reason="completed")

    assert registry.count_active() == 1


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_mutations_rewrite_json_store(tmp_path: Path) -> None:
    """Every mutation rewrites the JSON store with full snapshots."""
    store_path = tmp_path / "subsessions.json"
    registry = SubsessionRegistry(store_path=store_path)
    info = _create(registry)

    raw = json.loads(store_path.read_text(encoding="utf-8"))
    assert [e["subsession_id"] for e in raw] == [info.id]
    assert raw[0]["status"] == SubsessionStatus.RUNNING.value

    registry.append_transcript(info.id, "assistant", "progress")
    registry.mark_closed(info.id, summary="done", reason="completed")

    raw = json.loads(store_path.read_text(encoding="utf-8"))
    assert raw[0]["status"] == SubsessionStatus.CLOSED.value
    assert raw[0]["summary"] == "done"
    (entry,) = raw[0]["transcript"]
    assert entry["role"] == "assistant"
    assert entry["text"] == "progress"
    assert isinstance(entry["timestamp"], float)


def test_load_persisted_round_trips(tmp_path: Path) -> None:
    """A fresh registry on the same path reads back the persisted entries."""
    store_path = tmp_path / "subsessions.json"
    registry = SubsessionRegistry(store_path=store_path)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)
    registry.set_status(info.id, SubsessionStatus.SLEEPING, runs=4)

    entries = SubsessionRegistry(store_path=store_path).load_persisted()

    assert len(entries) == 1
    entry = entries[0]
    assert entry["subsession_id"] == info.id
    assert entry["kind"] == SubsessionKind.PERIODIC.value
    assert entry["status"] == SubsessionStatus.SLEEPING.value
    assert entry["runs"] == 4
    assert entry["interval_seconds"] == 60.0


def test_load_persisted_missing_or_corrupt_returns_empty(tmp_path: Path) -> None:
    """A missing or unparsable store yields an empty entry list."""
    missing = SubsessionRegistry(store_path=tmp_path / "nope.json")
    assert missing.load_persisted() == []

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{not json", encoding="utf-8")
    corrupt = SubsessionRegistry(store_path=corrupt_path)
    assert corrupt.load_persisted() == []

    disabled = SubsessionRegistry(store_path=None)
    assert disabled.load_persisted() == []


def test_terminal_pruning_keeps_most_recent_50(tmp_path: Path) -> None:
    """Old terminal entries beyond the retention cap are pruned oldest-first."""
    clock = FakeClock()
    store_path = tmp_path / "subsessions.json"
    registry = SubsessionRegistry(store_path=store_path, clock=clock)

    terminal_ids: list[str] = []
    for i in range(55):
        info = _create(registry, title=f"job-{i}")
        registry.mark_closed(info.id, summary="done", reason="completed")
        terminal_ids.append(info.id)
        clock.advance(1.0)

    # Pruning runs on create — a new entry evicts the oldest terminal ones.
    survivor = _create(registry, title="fresh")

    remaining = {i.id for i in registry.list_all()}
    terminal_remaining = [tid for tid in terminal_ids if tid in remaining]
    assert len(terminal_remaining) == 50
    # The oldest five terminal entries are gone, the newest 50 remain.
    assert terminal_remaining == terminal_ids[5:]
    assert survivor.id in remaining


def test_restore_noop_on_duplicate_id() -> None:
    """``restore`` does not overwrite an already-registered id."""
    registry = SubsessionRegistry(store_path=None)
    original = _create(registry, title="original")

    duplicate = SubsessionInfo(
        id=original.id,
        kind=SubsessionKind.TASK,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="impostor",
        prompt="p",
        model_level=3,
        status=SubsessionStatus.CLOSED,
        created_at=0.0,
        last_activity_at=0.0,
    )
    registry.restore(duplicate)

    assert registry.get(original.id) is original
    assert registry.get(original.id).title == "original"  # type: ignore[union-attr]


def test_restore_registers_new_entry_without_publishing() -> None:
    """``restore`` re-registers a record silently (no frames, no persist)."""
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)
    info = SubsessionInfo(
        id="restored-1",
        kind=SubsessionKind.TASK,
        owner_session_id="sess-9",
        parent_id=None,
        depth=1,
        title="old job",
        prompt="p",
        model_level=3,
        status=SubsessionStatus.CLOSED,
        created_at=1.0,
        last_activity_at=2.0,
    )

    registry.restore(info)

    assert registry.get("restored-1") is info
    assert registry.list_for_owner("sess-9") == [info]
    assert sink.frames == []


# ---------------------------------------------------------------------------
# idempotent create
# ---------------------------------------------------------------------------


def test_create_with_existing_sub_id_returns_original() -> None:
    """``create`` with an existing sub_id returns the original record.

    The existing record is returned without overwriting or publishing a
    second frame.
    """
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)

    first = _create(registry, sub_id="dup-1", title="first")
    first_publish_count = len(sink.frames)

    second = _create(registry, sub_id="dup-1", title="second")

    # Returns the SAME object, not a new record.
    assert second is first
    assert second.title == "first"
    # No additional frame published.
    assert len(sink.frames) == first_publish_count


# ---------------------------------------------------------------------------
# claim_run
# ---------------------------------------------------------------------------


def test_claim_run_returns_true_for_new_run() -> None:
    """``claim_run`` returns True the first time a run number is claimed."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)

    assert registry.claim_run(info.id, 1) is True
    assert 1 in info.completed_runs


def test_claim_run_returns_false_for_duplicate() -> None:
    """``claim_run`` returns False when the run number was already claimed."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)

    assert registry.claim_run(info.id, 1) is True
    assert registry.claim_run(info.id, 1) is False


def test_claim_run_returns_false_for_terminal_subsession() -> None:
    """``claim_run`` returns False for a subsession that is no longer active."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)
    registry.mark_closed(info.id, summary="done", reason="completed")

    assert registry.claim_run(info.id, 1) is False


def test_claim_run_returns_false_for_unknown_id() -> None:
    """``claim_run`` returns False for an unknown subsession id."""
    registry = SubsessionRegistry(store_path=None)

    assert registry.claim_run("ghost", 1) is False


# ---------------------------------------------------------------------------
# reap_orphans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reap_orphans_cancels_tasks_without_tree_membership() -> None:
    """``reap_orphans`` cancels workers not in any owner's tree.

    Workers whose subsession has no tree membership are cancelled and
    marked as FAILED.
    """
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, owner="sess-A", kind=SubsessionKind.PERIODIC)

    # Attach a fake task.
    task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(30))
    registry.attach_task(info.id, task)

    # Remove from the owner's tree.
    registry._by_owner["sess-A"].discard(info.id)

    reaped = registry.reap_orphans()
    assert reaped >= 1

    with contextlib.suppress(asyncio.CancelledError):
        _ = await task
    assert task.cancelled()
    # The subsession must be terminal (FAILED) so it no longer consumes
    # a concurrency slot.
    assert info.status is SubsessionStatus.FAILED
    assert info.error == "orphaned_timer_reaped"


@pytest.mark.asyncio
async def test_reap_orphans_skips_tasks_with_tree_membership() -> None:
    """``reap_orphans`` skips workers that are still in a tree.

    Workers whose subsession is still in a conversation tree are not
    cancelled.
    """
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, owner="sess-A", kind=SubsessionKind.PERIODIC)

    task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(30))
    registry.attach_task(info.id, task)

    reaped = registry.reap_orphans()
    assert reaped == 0

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        _ = await task


# ---------------------------------------------------------------------------
# reassign_owner
# ---------------------------------------------------------------------------


def test_reassign_owner_moves_tree_and_publishes_to_new_owner() -> None:
    """The whole tree moves to the new owner and started frames are pushed."""
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)
    a = _create(registry, owner="sess-old", title="one")
    b = _create(registry, owner="sess-old", title="two")
    other = _create(registry, owner="sess-other", title="unrelated")

    moved = registry.reassign_owner("sess-old", "sess-new")

    assert moved == 2
    assert a.owner_session_id == "sess-new"
    assert b.owner_session_id == "sess-new"
    assert other.owner_session_id == "sess-other"
    assert {i.id for i in registry.list_for_owner("sess-new")} == {a.id, b.id}
    assert registry.list_for_owner("sess-old") == []
    started_for_new = [
        frame
        for session_id, frame in sink.of_type(SSE_SUBSESSION_STARTED_TYPE)
        if session_id == "sess-new"
    ]
    assert {frame["subsession_id"] for frame in started_for_new} == {a.id, b.id}


def test_reassign_owner_same_or_unknown_owner_is_a_noop() -> None:
    """Same-owner and unknown-owner reassignments move nothing."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, owner="sess-A")

    assert registry.reassign_owner("sess-A", "sess-A") == 0
    assert registry.reassign_owner("ghost", "sess-B") == 0
    assert info.owner_session_id == "sess-A"
    assert [i.id for i in registry.list_for_owner("sess-A")] == [info.id]


def test_reassign_owner_persists_new_owner(tmp_path: Path) -> None:
    """The new owner_session_id is written to the JSON store."""
    store_path = tmp_path / "subsessions.json"
    registry = SubsessionRegistry(store_path=store_path)
    info = _create(registry, owner="sess-old")

    registry.reassign_owner("sess-old", "sess-new")

    raw = json.loads(store_path.read_text(encoding="utf-8"))
    entries = raw if isinstance(raw, list) else list(raw.values())
    stored = [
        e
        for e in entries
        if e.get("id") == info.id or e.get("subsession_id") == info.id
    ]
    assert stored, f"subsession {info.id} not found in store: {raw!r}"
    assert stored[0]["owner_session_id"] == "sess-new"


# ---------------------------------------------------------------------------
# dedup key tracking
# ---------------------------------------------------------------------------


def test_is_dedup_key_active_returns_sub_id_when_key_active() -> None:
    """``is_dedup_key_active`` returns the active subsession id for a known key."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.USER_CHAT,
        dedup_key="asyncio.run-crash",
    )

    active_id = registry.is_dedup_key_active("asyncio.run-crash")

    assert active_id == info.id


def test_is_dedup_key_active_returns_none_for_unknown_key() -> None:
    """``is_dedup_key_active`` returns None when the key is not tracked."""
    registry = SubsessionRegistry(store_path=None)

    assert registry.is_dedup_key_active("nonexistent") is None


def test_is_dedup_key_active_returns_none_when_subsession_is_terminal() -> None:
    """When the tracked subsession is terminal, ``is_dedup_key_active`` returns None.

    Additionally, the stale key is proactively cleaned from the internal map.
    """
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.USER_CHAT,
        dedup_key="stale-key",
    )
    # Manually close the subsession outside the normal close path that
    # would clean up the dedup key (simulates a race or direct mutation).
    registry.mark_closed(info.id, summary="done", reason="completed")

    active_id = registry.is_dedup_key_active("stale-key")

    assert active_id is None
    # The stale key should have been cleaned up.
    assert "stale-key" not in registry._active_dedup_keys


def test_close_clears_dedup_key_from_active_map() -> None:
    """``mark_closed`` removes the dedup key so a new side-chat can be spawned."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.USER_CHAT,
        dedup_key="reboot-required",
    )

    assert registry.is_dedup_key_active("reboot-required") == info.id

    registry.mark_closed(info.id, summary="resolved", reason="completed")

    assert registry.is_dedup_key_active("reboot-required") is None


def test_fail_clears_dedup_key_from_active_map() -> None:
    """``fail`` removes the dedup key so a new side-chat can be spawned."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.USER_CHAT,
        dedup_key="crash-loop",
    )

    assert registry.is_dedup_key_active("crash-loop") == info.id

    registry.fail(info.id, error="something went wrong")

    assert registry.is_dedup_key_active("crash-loop") is None


def test_cancel_and_close_clears_dedup_key_from_active_map() -> None:
    """``cancel_and_close`` removes the dedup key from the active map."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.USER_CHAT,
        dedup_key="external-close",
    )

    assert registry.is_dedup_key_active("external-close") == info.id

    registry.cancel_and_close(info.id, reason="parent override", closed_by="parent")

    assert registry.is_dedup_key_active("external-close") is None


def test_dedup_key_on_task_is_tracked_but_spawn_filtered() -> None:
    """The registry tracks dedup keys for all kinds, but spawn_subsession
    only checks the dedup guard for user_chat kinds.

    ``is_dedup_key_active`` is a low-level lookup that returns whatever
    is in the map — kind filtering happens at the spawn_subsession layer.
    """
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.TASK,
        dedup_key="task-dedup",
    )

    # is_dedup_key_active does NOT filter by kind — it returns the id.
    assert registry.is_dedup_key_active("task-dedup") == info.id


def test_dedup_key_not_tracked_when_none() -> None:
    """When dedup_key is None (default), no entry is added to _active_dedup_keys."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.USER_CHAT)

    assert info.dedup_key is None
    # No key is added to the active map.
    assert len(registry._active_dedup_keys) == 0


def test_new_spawn_with_same_dedup_key_after_close_succeeds() -> None:
    """After the original subsession closes, a new spawn with the same key works."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(
        registry,
        kind=SubsessionKind.USER_CHAT,
        dedup_key="unique-issue",
        title="first",
    )

    assert registry.is_dedup_key_active("unique-issue") == first.id

    registry.mark_closed(first.id, summary="done", reason="completed")

    # The key is now free — a second create should succeed.
    second = _create(
        registry,
        kind=SubsessionKind.USER_CHAT,
        dedup_key="unique-issue",
        title="second",
    )
    assert second.id != first.id
    assert registry.is_dedup_key_active("unique-issue") == second.id

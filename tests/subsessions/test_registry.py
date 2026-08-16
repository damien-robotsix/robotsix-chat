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


def test_dedup_key_on_task_is_tracked() -> None:
    """The registry tracks dedup keys for all subsession kinds.

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


def test_create_raises_dedup_error_for_active_dedup_key() -> None:
    """``create`` raises ``SubsessionDedupError`` when dedup_key is already active.

    The check in ``create`` is defense-in-depth — the primary guard lives
    in ``spawn_subsession``, but a direct ``create`` call (or a future
    code path that bypasses the pre-check) must still be prevented from
    creating a duplicate.
    """
    from robotsix_chat.subsessions import SubsessionDedupError

    registry = SubsessionRegistry(store_path=None)
    first = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        dedup_key="ticket-5f1c",
        title="monitor-5f1c",
    )
    assert registry.is_dedup_key_active("ticket-5f1c") == first.id

    with pytest.raises(SubsessionDedupError) as exc_info:
        _create(
            registry,
            kind=SubsessionKind.PERIODIC,
            dedup_key="ticket-5f1c",
            title="duplicate-monitor-5f1c",
        )
    assert exc_info.value.existing_id == first.id


# ---------------------------------------------------------------------------
# find_active_periodic_by_ticket_id
# ---------------------------------------------------------------------------


def test_find_active_periodic_by_ticket_id_returns_match_via_checkpoint() -> None:
    """Return sub id when a PERIODIC monitor's checkpoint carries the ticket_id."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        title="monitor-5f1c",
    )
    # Simulate the first run setting the checkpoint.
    registry.update_checkpoint(
        info.id, {"ticket_id": "5f1c", "last_known_state": "open"}
    )

    result = registry.find_active_periodic_by_ticket_id("5f1c")
    assert result == info.id


def test_find_active_periodic_by_ticket_id_returns_none_when_no_match() -> None:
    """Return None when no active PERIODIC sub has the ticket_id in its checkpoint."""
    registry = SubsessionRegistry(store_path=None)
    _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        title="monitor-5f1c",
    )
    # No checkpoint set, and ticket_id doesn't match anyway.
    result = registry.find_active_periodic_by_ticket_id("7691")
    assert result is None


def test_find_active_periodic_by_ticket_id_skips_terminal_subsessions() -> None:
    """A terminal PERIODIC sub is not returned even if its checkpoint matches."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        title="monitor-5f1c",
    )
    registry.update_checkpoint(info.id, {"ticket_id": "5f1c"})
    registry.mark_closed(info.id, summary="done", reason="completed")

    result = registry.find_active_periodic_by_ticket_id("5f1c")
    assert result is None


def test_find_active_periodic_by_ticket_id_skips_non_periodic() -> None:
    """A TASK sub with a matching checkpoint ticket_id is not returned."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.TASK,
        title="task-with-checkpoint",
    )
    registry.update_checkpoint(info.id, {"ticket_id": "5f1c"})

    result = registry.find_active_periodic_by_ticket_id("5f1c")
    assert result is None


# ---------------------------------------------------------------------------
# update_checkpoint ticket_id preservation for WAIT_FOR_EVENT
# ---------------------------------------------------------------------------


def test_update_checkpoint_preserves_ticket_id_for_wait_for_event() -> None:
    """Replacing a WAIT_FOR_EVENT checkpoint never drops its existing ticket_id."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        title="event monitor",
        checkpoint={"ticket_id": "tick-1", "last_known_state": "open"},
    )

    registry.update_checkpoint(info.id, {"last_known_state": "in_progress"})

    refreshed = registry.get(info.id)
    assert refreshed is not None
    assert refreshed.checkpoint == {
        "ticket_id": "tick-1",
        "last_known_state": "in_progress",
    }


def test_update_checkpoint_recovers_ticket_id_from_dedup_key() -> None:
    """A WAIT_FOR_EVENT checkpoint replacement falls back to dedup_key for ticket_id."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        title="event monitor",
        dedup_key="tick-2",
        checkpoint=None,
    )

    registry.update_checkpoint(info.id, {"last_known_state": "open"})

    refreshed = registry.get(info.id)
    assert refreshed is not None
    assert refreshed.checkpoint == {
        "ticket_id": "tick-2",
        "last_known_state": "open",
    }


def test_update_checkpoint_respects_explicit_ticket_id_override() -> None:
    """An explicit valid ticket_id in the replacement is kept, not overwritten."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        title="event monitor",
        checkpoint={"ticket_id": "tick-old"},
    )

    registry.update_checkpoint(
        info.id, {"ticket_id": "tick-new", "last_known_state": "open"}
    )

    refreshed = registry.get(info.id)
    assert refreshed is not None
    assert refreshed.checkpoint == {
        "ticket_id": "tick-new",
        "last_known_state": "open",
    }


def test_update_checkpoint_does_not_fabricate_ticket_id_for_wait_for_event() -> None:
    """No ticket_id is injected when neither checkpoint nor dedup_key has one."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        title="event monitor",
        checkpoint=None,
        dedup_key=None,
    )

    registry.update_checkpoint(info.id, {"last_known_state": "open"})

    refreshed = registry.get(info.id)
    assert refreshed is not None
    assert refreshed.checkpoint == {"last_known_state": "open"}


# ---------------------------------------------------------------------------
# update_checkpoint auto_stop_no_change_runs preservation for PERIODIC
# ---------------------------------------------------------------------------


def test_update_checkpoint_preserves_auto_stop_no_change_runs_for_periodic() -> None:
    """Replacing a PERIODIC checkpoint never drops its per-spawn override."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        title="long-lived monitor",
        interval_seconds=60.0,
        checkpoint={
            "ticket_id": "tick-1",
            "auto_stop_no_change_runs": 50,
            "last_known_state": "open",
        },
    )

    registry.update_checkpoint(
        info.id, {"last_known_state": "code_review", "pr_number": 42}
    )

    refreshed = registry.get(info.id)
    assert refreshed is not None
    assert refreshed.checkpoint == {
        "auto_stop_no_change_runs": 50,
        "last_known_state": "code_review",
        "pr_number": 42,
    }


def test_update_checkpoint_keeps_explicit_auto_stop_override() -> None:
    """An explicit valid override in the replacement is kept, not overwritten."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        title="long-lived monitor",
        interval_seconds=60.0,
        checkpoint={"auto_stop_no_change_runs": 50},
    )

    registry.update_checkpoint(info.id, {"auto_stop_no_change_runs": 75})

    refreshed = registry.get(info.id)
    assert refreshed is not None
    assert refreshed.checkpoint == {"auto_stop_no_change_runs": 75}


def test_update_checkpoint_does_not_fabricate_auto_stop_no_change_runs() -> None:
    """No override is injected when the PERIODIC checkpoint never had one."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        title="default monitor",
        interval_seconds=60.0,
        checkpoint={"ticket_id": "tick-1"},
    )

    registry.update_checkpoint(info.id, {"last_known_state": "open"})

    refreshed = registry.get(info.id)
    assert refreshed is not None
    assert refreshed.checkpoint == {"last_known_state": "open"}


def test_update_checkpoint_does_not_preserve_invalid_auto_stop_override() -> None:
    """A non-int / bool override in the prior checkpoint is not preserved."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        title="bogus monitor",
        interval_seconds=60.0,
        checkpoint={"auto_stop_no_change_runs": 2.5},
    )

    registry.update_checkpoint(info.id, {"last_known_state": "open"})

    refreshed = registry.get(info.id)
    assert refreshed is not None
    assert refreshed.checkpoint == {"last_known_state": "open"}


def test_update_checkpoint_preserves_no_change_pause_count_for_periodic() -> None:
    """Replacing a PERIODIC checkpoint never drops the no-change pause counter."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        title="paused monitor",
        interval_seconds=60.0,
        checkpoint={
            "ticket_id": "tick-1",
            "no_change_pause_count": 2,
            "last_known_state": "open",
        },
    )

    registry.update_checkpoint(info.id, {"last_known_state": "code_review"})

    refreshed = registry.get(info.id)
    assert refreshed is not None
    assert refreshed.checkpoint == {
        "no_change_pause_count": 2,
        "last_known_state": "code_review",
    }


def test_update_checkpoint_preserves_zero_no_change_pause_count() -> None:
    """A zero counter (progress observed) is preserved, not treated as absent."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        title="paused monitor",
        interval_seconds=60.0,
        checkpoint={"no_change_pause_count": 0},
    )

    registry.update_checkpoint(info.id, {"last_known_state": "open"})

    refreshed = registry.get(info.id)
    assert refreshed is not None
    assert refreshed.checkpoint == {
        "no_change_pause_count": 0,
        "last_known_state": "open",
    }


def test_update_checkpoint_does_not_fabricate_no_change_pause_count() -> None:
    """No counter is injected when the PERIODIC checkpoint never had one."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        title="default monitor",
        interval_seconds=60.0,
        checkpoint={"ticket_id": "tick-1"},
    )

    registry.update_checkpoint(info.id, {"last_known_state": "open"})

    refreshed = registry.get(info.id)
    assert refreshed is not None
    assert refreshed.checkpoint == {"last_known_state": "open"}


def test_create_raises_dedup_error_for_checkpoint_ticket_id_match() -> None:
    """``create`` raises ``SubsessionDedupError`` on checkpoint ticket_id match.

    The original PERIODIC monitor was spawned without a dedup_key, but
    its checkpoint carries the watched ticket_id after the first run.
    A second spawn WITH a matching dedup_key must be caught.
    """
    from robotsix_chat.subsessions import SubsessionDedupError

    registry = SubsessionRegistry(store_path=None)
    first = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        title="monitor-5f1c",
        # No dedup_key — agent forgot to set it.
    )
    # After first run, checkpoint carries the watched ticket_id.
    registry.update_checkpoint(first.id, {"ticket_id": "5f1c"})

    # A second spawn for the same ticket WITH dedup_key should be caught.
    with pytest.raises(SubsessionDedupError) as exc_info:
        _create(
            registry,
            kind=SubsessionKind.PERIODIC,
            dedup_key="5f1c",
            title="duplicate-monitor-5f1c",
        )
    assert exc_info.value.existing_id == first.id


@pytest.mark.asyncio
async def test_create_dedup_self_match_on_resume_with_checkpoint() -> None:
    """A resumed periodic with dedup_key==checkpoint.ticket_id does NOT self-match.

    Regression: before the dedup checks were moved ahead of registry
    insertion, ``find_active_periodic_by_ticket_id(dedup_key)`` matched the
    entry that ``create()`` had just inserted (same checkpoint.ticket_id)
    and raised ``SubsessionDedupError``.  ``spawn_subsession`` caught the
    error and returned the existing id — but no worker task was ever
    launched, leaving a RUNNING zombie that sat idle forever.

    The resume path calls ``spawn_subsession`` with ``sub_id``,
    ``checkpoint``, and ``dedup_key`` all carried over from the persisted
    store.  This test simulates that path and asserts a worker IS attached.
    """
    from robotsix_chat.subsessions.worker import spawn_subsession
    from tests.common.subsession_fakes import build_env, make_settings

    registry = SubsessionRegistry(store_path=None)
    env = build_env(registry=registry, settings=make_settings())

    ticket_id = "ticket-self-match-99"
    sub_id = "periodic-resume-self"
    checkpoint = {"ticket_id": ticket_id, "last_known_state": "open"}

    # Simulate _resume_periodic_entry: dedup_key == checkpoint.ticket_id.
    result_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-main",
        parent_id=None,
        depth=1,
        title="monitor-self-match",
        prompt="check ticket",
        model_level=3,
        interval_seconds=60.0,
        max_runs=5,
        sub_id=sub_id,
        runs=1,
        completed_runs={1},
        checkpoint=checkpoint,
        dedup_key=ticket_id,
    )

    # The spawn must return the sub_id (a worker was launched).
    assert result_id == sub_id

    # The registry entry must be present and RUNNING.
    info = registry.get(sub_id)
    assert info is not None
    assert info.is_active

    # A worker task must be attached — not a zombie.
    task = registry._running.get(sub_id)
    assert task is not None, (
        "_running has no task for sub_id; registry entry is a zombie "
        "(half-registered RUNNING with no worker)."
    )

    # Clean up the worker so the test doesn't leak an asyncio task.
    registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 2.0)


# ---------------------------------------------------------------------------
# is_duplicate_ticket_terminal
# ---------------------------------------------------------------------------


def test_is_duplicate_ticket_terminal_true_when_prior_closed_terminal() -> None:
    """True when another CLOSED subsession already reported the ticket terminal."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})
    registry.mark_closed(first.id, summary="done", reason="ticket_terminal")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_ticket_terminal("T-123", second.id) is True


def test_is_duplicate_ticket_terminal_false_for_different_ticket_id() -> None:
    """Returns False when the prior CLOSED subsession has a different ticket_id."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-AAA"})
    registry.mark_closed(first.id, summary="done", reason="ticket_terminal")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-BBB"})

    assert registry.is_duplicate_ticket_terminal("T-BBB", second.id) is False


def test_is_duplicate_ticket_terminal_false_when_prior_not_closed() -> None:
    """Returns False when the prior subsession with the same ticket is still active."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    # first is still RUNNING — not a terminal reporter yet.
    assert registry.is_duplicate_ticket_terminal("T-123", second.id) is False


def test_is_duplicate_ticket_terminal_false_when_prior_closed_non_terminal() -> None:
    """False when the prior CLOSED subsession closed for a non-terminal reason."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})
    registry.mark_closed(first.id, summary="paused", reason="paused")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_ticket_terminal("T-123", second.id) is False


def test_is_duplicate_ticket_terminal_false_when_prior_failed() -> None:
    """Returns False when the prior subsession with the same ticket is FAILED."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})
    registry.fail(first.id, error="something went wrong")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_ticket_terminal("T-123", second.id) is False


def test_is_duplicate_ticket_terminal_false_when_prior_interrupted() -> None:
    """Returns False when the prior subsession with the same ticket is INTERRUPTED."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})
    # ``mark_interrupted`` only accepts active subsessions; close it first
    # via the internal path to set INTERRUPTED directly.
    registry._close_and_publish(
        first,
        status=SubsessionStatus.INTERRUPTED,
        summary="restarted",
    )

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_ticket_terminal("T-123", second.id) is False


def test_is_duplicate_ticket_terminal_false_when_no_other_subsession() -> None:
    """Returns False when the registry contains only the excluded subsession."""
    registry = SubsessionRegistry(store_path=None)
    only = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(only.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_ticket_terminal("T-123", only.id) is False


def test_is_duplicate_ticket_terminal_false_when_prior_no_checkpoint() -> None:
    """Returns False when the prior CLOSED subsession has no checkpoint."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.mark_closed(first.id, summary="done", reason="ticket_terminal")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_ticket_terminal("T-123", second.id) is False


def test_is_duplicate_ticket_terminal_false_when_prior_ckpt_no_ticket_id() -> None:
    """False when the prior CLOSED subsession's checkpoint lacks a ticket_id."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"other_key": "value"})
    registry.mark_closed(first.id, summary="done", reason="ticket_terminal")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_ticket_terminal("T-123", second.id) is False


def test_is_duplicate_ticket_terminal_true_when_prior_reason_completed() -> None:
    """Returns True when the prior subsession's close_reason is 'completed'."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})
    registry.mark_closed(first.id, summary="done", reason="completed")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_ticket_terminal("T-123", second.id) is True


# -- is_duplicate_auto_pause -------------------------------------------------


def test_is_duplicate_auto_pause_true_when_prior_closed_paused() -> None:
    """True when another CLOSED subsession already auto-paused the same ticket."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})
    registry.mark_closed(first.id, summary="paused", reason="paused")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_auto_pause("T-123", second.id) is True


def test_is_duplicate_auto_pause_true_when_prior_no_change_auto_stop() -> None:
    """True when prior CLOSED subsession has reason 'no_change_auto_stop'."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})
    registry.mark_closed(first.id, summary="auto-stopped", reason="no_change_auto_stop")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_auto_pause("T-123", second.id) is True


def test_is_duplicate_auto_pause_true_when_prior_terminal() -> None:
    """True when prior subsession already reported the ticket as terminal."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})
    registry.mark_closed(first.id, summary="done", reason="ticket_terminal")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_auto_pause("T-123", second.id) is True


def test_is_duplicate_auto_pause_true_when_prior_completed() -> None:
    """True when prior subsession has reason 'completed'."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})
    registry.mark_closed(first.id, summary="done", reason="completed")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_auto_pause("T-123", second.id) is True


def test_is_duplicate_auto_pause_false_for_different_ticket_id() -> None:
    """Returns False when the prior CLOSED subsession has a different ticket_id."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-AAA"})
    registry.mark_closed(first.id, summary="paused", reason="paused")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-BBB"})

    assert registry.is_duplicate_auto_pause("T-BBB", second.id) is False


def test_is_duplicate_auto_pause_false_when_prior_not_closed() -> None:
    """Returns False when the prior subsession is still active."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_auto_pause("T-123", second.id) is False


def test_is_duplicate_auto_pause_false_when_prior_closed_non_terminal() -> None:
    """False when prior CLOSED for non-terminal/pause reason (max_runs)."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})
    registry.mark_closed(first.id, summary="done", reason="max_runs")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_auto_pause("T-123", second.id) is False


def test_is_duplicate_auto_pause_false_when_prior_failed() -> None:
    """Returns False when the prior subsession is FAILED."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"ticket_id": "T-123"})
    registry.fail(first.id, error="something went wrong")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_auto_pause("T-123", second.id) is False


def test_is_duplicate_auto_pause_false_when_no_other_subsession() -> None:
    """Returns False when the registry contains only the excluded subsession."""
    registry = SubsessionRegistry(store_path=None)
    only = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(only.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_auto_pause("T-123", only.id) is False


def test_is_duplicate_auto_pause_false_when_prior_no_checkpoint() -> None:
    """Returns False when the prior CLOSED subsession has no checkpoint."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.mark_closed(first.id, summary="paused", reason="paused")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_auto_pause("T-123", second.id) is False


def test_is_duplicate_auto_pause_false_when_prior_ckpt_no_ticket_id() -> None:
    """False when the prior CLOSED subsession's checkpoint lacks a ticket_id."""
    registry = SubsessionRegistry(store_path=None)
    first = _create(registry, kind=SubsessionKind.TASK, title="monitor-1")
    registry.update_checkpoint(first.id, {"other_key": "value"})
    registry.mark_closed(first.id, summary="paused", reason="paused")

    second = _create(registry, kind=SubsessionKind.TASK, title="monitor-2")
    registry.update_checkpoint(second.id, {"ticket_id": "T-123"})

    assert registry.is_duplicate_auto_pause("T-123", second.id) is False


# -- reopen ---------------------------------------------------------------


def test_reopen_transitions_paused_to_running() -> None:
    """``reopen`` transitions a paused CLOSED subsession back to RUNNING."""
    sink = RecordingSink()
    registry = SubsessionRegistry(event_sink=sink, store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)
    registry.mark_closed(
        info.id, summary="paused after idle", reason="paused", closed_by="system"
    )

    # Clear frames so we only see the reopen frame.
    sink.frames.clear()

    reopened = registry.reopen(info.id)

    assert reopened is not None
    assert reopened.status is SubsessionStatus.RUNNING
    assert reopened.close_reason is None
    assert reopened.summary is None
    # Verify the updated frame was published.
    updates = sink.of_type(SSE_SUBSESSION_UPDATED_TYPE)
    assert len(updates) == 1
    _, frame = updates[0]
    assert frame["subsession_id"] == info.id
    assert frame["status"] == "running"


def test_reopen_returns_none_for_unknown_id() -> None:
    """``reopen`` returns None for an unknown subsession id."""
    registry = SubsessionRegistry(store_path=None)
    assert registry.reopen("nonexistent") is None


def test_reopen_returns_none_for_active_subsession() -> None:
    """``reopen`` returns None when the subsession is still active."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)

    assert registry.reopen(info.id) is None


def test_reopen_returns_none_for_non_paused_closed() -> None:
    """``reopen`` returns None when closed with an unrecognised reason."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)
    registry.mark_closed(
        info.id, summary="done", reason="completed", closed_by="system"
    )

    assert registry.reopen(info.id) is None


def test_reopen_transitions_human_approval_timeout_to_running() -> None:
    """``reopen`` transitions a ``human_approval_timeout`` subsession to RUNNING."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)
    info.checkpoint = {
        "ticket_id": "abc123",
        "last_known_state": "human_issue_approval",
        "human_approval_since": 999999.0,
    }
    registry.mark_closed(
        info.id,
        summary="human approval timeout",
        reason="human_approval_timeout",
        closed_by="system",
    )

    reopened = registry.reopen(info.id)

    assert reopened is not None
    assert reopened.status is SubsessionStatus.RUNNING
    assert reopened.close_reason is None
    assert reopened.summary is None
    # human_approval_since must be cleared so the monitor doesn't
    # immediately time out again.
    assert reopened.checkpoint is not None
    assert "human_approval_since" not in reopened.checkpoint
    assert reopened.checkpoint["ticket_id"] == "abc123"


def test_reopen_returns_none_for_non_periodic() -> None:
    """``reopen`` returns None for a non-periodic paused subsession."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.TASK)
    # Manually set it to CLOSED with reason paused (simulating a corner case)
    info.status = SubsessionStatus.CLOSED
    info.close_reason = "paused"

    assert registry.reopen(info.id) is None


def test_reopen_is_idempotent() -> None:
    """Reopening an already-reopened subsession returns None."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)
    registry.mark_closed(
        info.id, summary="paused after idle", reason="paused", closed_by="system"
    )

    first = registry.reopen(info.id)
    assert first is not None

    second = registry.reopen(info.id)
    assert second is None


# -- find_paused_periodic --------------------------------------------------


def test_find_paused_periodic_returns_paused_monitors() -> None:
    """Returns periodic subsessions CLOSED with 'paused' or 'human_approval_timeout'."""
    registry = SubsessionRegistry(store_path=None)
    p1 = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        title="monitor-1",
    )
    p2 = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        title="monitor-2",
    )
    p3 = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        title="monitor-3",
    )
    # Non-periodic — should not appear.
    t1 = _create(registry, kind=SubsessionKind.TASK, title="task-1")
    # Active periodic — should not appear.
    p4 = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        title="monitor-4",
    )

    # Pause two periodic monitors, timeout-escalate one.
    registry.mark_closed(p1.id, summary="paused", reason="paused", closed_by="system")
    registry.mark_closed(p2.id, summary="paused", reason="paused", closed_by="system")
    registry.mark_closed(
        p3.id, summary="timeout", reason="human_approval_timeout", closed_by="system"
    )
    # Close the task with max_runs (not watched).
    registry.mark_closed(t1.id, summary="done", reason="max_runs", closed_by="system")

    paused = registry.find_paused_periodic()
    paused_ids = {info.id for info in paused}

    assert len(paused) == 3
    assert p1.id in paused_ids
    assert p2.id in paused_ids
    assert p3.id in paused_ids
    assert p4.id not in paused_ids
    assert t1.id not in paused_ids


def test_find_paused_periodic_empty_when_no_paused() -> None:
    """Returns an empty list when no subsessions are paused."""
    registry = SubsessionRegistry(store_path=None)
    _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)

    assert registry.find_paused_periodic() == []


def test_find_paused_periodic_by_ticket_id_matches_only_live_paused() -> None:
    """Returns live PAUSED periodic monitors tracking the ticket, not legacy closed."""
    registry = SubsessionRegistry(store_path=None)
    p1 = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "t-1"},
    )
    p2 = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "t-2"},
    )
    legacy = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "t-1"},
    )
    registry.mark_paused(p1.id, summary="paused", reason="paused")
    registry.mark_paused(p2.id, summary="paused", reason="paused")
    registry.mark_closed(
        legacy.id, summary="paused", reason="paused", closed_by="system"
    )

    matches = registry.find_paused_periodic_by_ticket_id("t-1")

    assert [info.id for info in matches] == [p1.id]


def test_route_mill_event_wakes_paused_periodic_for_ticket() -> None:
    """A mill state-change event wakes a PAUSED periodic monitor tracking the ticket."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "t-1"},
    )
    registry.mark_paused(info.id, summary="auto-paused", reason="paused")

    woken = registry.route_mill_event(
        "t-1",
        {"ticket_id": "t-1", "old_state": "open", "new_state": "closed"},
    )

    assert woken == 1
    messages = registry.drain_inbox(info.id)
    assert len(messages) == 1
    assert messages[0].role == "system"
    assert "ticket t-1 state changed from 'open' to 'closed'" in messages[0].text
    assert registry._wake_events[info.id].is_set() is False


def test_route_mill_event_ignores_paused_periodic_for_other_ticket() -> None:
    """A mill event for an untracked ticket does not wake a paused monitor."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "t-1"},
    )
    registry.mark_paused(info.id, summary="auto-paused", reason="paused")

    woken = registry.route_mill_event(
        "t-other",
        {"ticket_id": "t-other", "old_state": "open", "new_state": "closed"},
    )

    assert woken == 0
    assert registry.drain_inbox(info.id) == []


def test_route_mill_event_ignores_paused_periodic_without_ticket_id() -> None:
    """A paused monitor with no checkpoint ticket_id is not woken."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
    )
    registry.mark_paused(info.id, summary="auto-paused", reason="paused")

    woken = registry.route_mill_event(
        "t-1",
        {"ticket_id": "t-1", "old_state": "open", "new_state": "closed"},
    )

    assert woken == 0
    assert registry.drain_inbox(info.id) == []


def test_find_paused_periodic_includes_pre_authorized_approval() -> None:
    """``pre_authorized_approval`` monitors are included in the paused set."""
    registry = SubsessionRegistry(store_path=None)
    p = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        title="pre-auth-monitor",
    )
    registry.mark_closed(
        p.id,
        summary="pre-authorized escalation",
        reason="pre_authorized_approval",
        closed_by="system",
    )
    paused = registry.find_paused_periodic()
    assert len(paused) == 1
    assert paused[0].id == p.id


def test_reopen_pre_authorized_approval() -> None:
    """``reopen`` transitions a ``pre_authorized_approval`` subsession to RUNNING."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        title="pre-auth-monitor",
    )
    registry.mark_closed(
        info.id,
        summary="pre-authorized escalation",
        reason="pre_authorized_approval",
        closed_by="system",
    )
    reopened = registry.reopen(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.RUNNING
    assert reopened.close_reason is None
    # Second reopen is a no-op (already active).
    assert registry.reopen(info.id) is None


def test_reopen_max_runs_resets_run_counter() -> None:
    """``reopen`` resets runs to 0 for a max_runs-closed monitor."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        title="max-runs-monitor",
    )
    # Simulate 60 runs then max_runs close.
    info.runs = 60
    registry.mark_closed(
        info.id,
        summary="Reached the 60-run limit.",
        reason="max_runs",
        closed_by="system",
    )
    reopened = registry.reopen(info.id)
    assert reopened is not None
    assert reopened.status is SubsessionStatus.RUNNING
    assert reopened.close_reason is None
    assert reopened.runs == 0


def test_reopen_max_runs_with_escalation_count_resets_run_counter() -> None:
    """``reopen`` resets runs when checkpoint carries max_runs_exhausted_count."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        title="escalation-monitor",
    )
    info.runs = 60
    info.checkpoint = {"max_runs_exhausted_count": 2}
    registry.mark_closed(
        info.id,
        summary="paused after idle",
        reason="paused",
        closed_by="system",
    )
    reopened = registry.reopen(info.id)
    assert reopened is not None
    assert reopened.runs == 0


def test_find_paused_periodic_includes_max_runs() -> None:
    """``find_paused_periodic`` returns monitors closed with reason 'max_runs'."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        title="max-runs-monitor",
    )
    registry.mark_closed(
        info.id,
        summary="Reached the 60-run limit.",
        reason="max_runs",
        closed_by="system",
    )
    paused = registry.find_paused_periodic()
    ids = [p.id for p in paused]
    assert info.id in ids


def test_reopen_returns_none_for_max_runs_escalated() -> None:
    """``reopen`` returns None for 'max_runs_escalated' — no auto-reopen."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
    )
    registry.mark_closed(
        info.id,
        summary="escalated after repeated budget exhaustion",
        reason="max_runs_escalated",
        closed_by="system",
    )
    assert registry.reopen(info.id) is None


# ---------------------------------------------------------------------------
# update_periodic_config
# ---------------------------------------------------------------------------


def test_update_periodic_config_updates_prompt() -> None:
    """``update_periodic_config`` updates the prompt/instructions."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)

    ok = registry.update_periodic_config(info.id, prompt="new instructions")

    assert ok is True
    assert info.prompt == "new instructions"


def test_update_periodic_config_updates_interval() -> None:
    """``update_periodic_config`` updates interval_seconds."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)

    ok = registry.update_periodic_config(info.id, interval_seconds=120.0)

    assert ok is True
    assert info.interval_seconds == 120.0


def test_update_periodic_config_updates_max_runs() -> None:
    """``update_periodic_config`` updates max_runs."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)

    ok = registry.update_periodic_config(info.id, max_runs=15)

    assert ok is True
    assert info.max_runs == 15


def test_update_periodic_config_multiple_fields() -> None:
    """``update_periodic_config`` can update several fields at once."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)

    ok = registry.update_periodic_config(
        info.id,
        prompt="watch T-99",
        interval_seconds=300.0,
        max_runs=5,
    )

    assert ok is True
    assert info.prompt == "watch T-99"
    assert info.interval_seconds == 300.0
    assert info.max_runs == 5


def test_update_periodic_config_none_leaves_field_unchanged() -> None:
    """Fields left at None are not touched."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)
    info.prompt = "original"
    info.max_runs = 10

    ok = registry.update_periodic_config(
        info.id,
        interval_seconds=90.0,
    )

    assert ok is True
    assert info.prompt == "original"  # unchanged
    assert info.interval_seconds == 90.0
    assert info.max_runs == 10  # unchanged


def test_update_periodic_config_does_not_reset_runs() -> None:
    """The runs counter is never reset by update_periodic_config."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(
        registry,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        runs=5,
    )

    registry.update_periodic_config(
        info.id, prompt="new", interval_seconds=30.0, max_runs=3
    )

    assert info.runs == 5  # unchanged


def test_update_periodic_config_rejects_non_periodic() -> None:
    """Returns False for task/user_chat subsessions."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.TASK)

    ok = registry.update_periodic_config(info.id, prompt="new")

    assert ok is False
    assert info.prompt == "do the thing"  # unchanged


def test_update_periodic_config_rejects_inactive() -> None:
    """Returns False for closed/terminal subsessions."""
    registry = SubsessionRegistry(store_path=None)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)
    registry.mark_closed(info.id, summary="done", reason="completed")

    ok = registry.update_periodic_config(info.id, prompt="new")

    assert ok is False
    assert info.prompt == "do the thing"  # unchanged


def test_update_periodic_config_rejects_unknown_id() -> None:
    """Returns False for unknown subsession ids."""
    registry = SubsessionRegistry(store_path=None)

    ok = registry.update_periodic_config("nonexistent", prompt="new")

    assert ok is False


def test_update_periodic_config_persists(tmp_path: Path) -> None:
    """Changes are persisted to the JSON store."""
    store_path = tmp_path / "subsessions.json"
    registry = SubsessionRegistry(store_path=store_path)
    info = _create(registry, kind=SubsessionKind.PERIODIC, interval_seconds=60.0)

    registry.update_periodic_config(
        info.id, prompt="persisted prompt", interval_seconds=42.0
    )

    # Re-load from disk — the change should survive.
    raw = json.loads(store_path.read_text())
    entry = next(e for e in raw if e["subsession_id"] == info.id)
    assert entry["prompt"] == "persisted prompt"
    assert entry["interval_seconds"] == 42.0

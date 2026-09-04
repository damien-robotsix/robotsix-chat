"""Tests for the subsession data model (enums, dataclasses, snapshots)."""

from __future__ import annotations

from robotsix_chat.subsessions import (
    ACTIVE_STATUSES,
    SubsessionInfo,
    SubsessionKind,
    SubsessionStatus,
)
from robotsix_chat.subsessions.models import InboxMessage, TranscriptEntry


def _info(**overrides: object) -> SubsessionInfo:
    """Build a fully-populated ``SubsessionInfo`` for snapshot tests."""
    defaults: dict[str, object] = {
        "id": "sub-123",
        "kind": SubsessionKind.PERIODIC,
        "owner_session_id": "sess-1",
        "parent_id": "parent-1",
        "depth": 2,
        "title": "watch CI",
        "prompt": "check the build",
        "model_level": 2,
        "status": SubsessionStatus.SLEEPING,
        "created_at": 100.0,
        "last_activity_at": 200.0,
        "interval_seconds": 60.0,
        "next_run_at": 260.0,
        "include_previous_result": True,
        "runs": 3,
        "max_runs": 10,
        "last_result": "green",
        "summary": None,
        "close_reason": None,
        "error": None,
    }
    defaults.update(overrides)
    return SubsessionInfo(**defaults)  # type: ignore[arg-type]


def test_snapshot_round_trips_every_field() -> None:
    """``snapshot()`` exposes every field under the documented keys."""
    snapshot = _info().snapshot()

    assert snapshot == {
        "subsession_id": "sub-123",
        "kind": "periodic",
        "owner_session_id": "sess-1",
        "parent_id": "parent-1",
        "depth": 2,
        "title": "watch CI",
        "prompt": "check the build",
        "model_level": 2,
        "status": "sleeping",
        "created_at": 100.0,
        "last_activity_at": 200.0,
        "interval_seconds": 60.0,
        "anchor_time": None,
        "next_run_at": 260.0,
        "include_previous_result": True,
        "runs": 3,
        "max_runs": 10,
        "last_result": "green",
        "summary": None,
        "close_reason": None,
        "error": None,
        "completed_runs": [],
        "turn_history": [],
        "checkpoint": None,
        "dedup_key": None,
        "depends_on_ticket_id": None,
        "consecutive_no_change": 0,
        "consecutive_errored_runs": 0,
        "retry_count": 0,
        "event_timeout_seconds": None,
        "run_timeout_seconds": None,
    }


def test_snapshot_exposes_anchor_time() -> None:
    """A periodic anchor is surfaced under the ``anchor_time`` key."""
    snapshot = _info(anchor_time="09:00 Europe/Paris").snapshot()

    assert snapshot["anchor_time"] == "09:00 Europe/Paris"


def test_snapshot_serialises_enums_as_values() -> None:
    """``kind`` and ``status`` are serialised as their string values."""
    snapshot = _info(
        kind=SubsessionKind.USER_CHAT, status=SubsessionStatus.FAILED
    ).snapshot()

    assert snapshot["kind"] == SubsessionKind.USER_CHAT.value
    assert snapshot["status"] == SubsessionStatus.FAILED.value
    assert isinstance(snapshot["kind"], str)
    assert isinstance(snapshot["status"], str)


def test_snapshot_omits_transcript_by_default() -> None:
    """The (potentially large) transcript is not included by default."""
    info = _info()
    info.transcript.append(TranscriptEntry(role="assistant", text="hi", timestamp=1.0))

    assert "transcript" not in info.snapshot()


def test_snapshot_serialises_turn_history_as_lists() -> None:
    """``turn_history`` (tuples in memory) round-trips as JSON-safe lists."""
    info = _info()
    info.turn_history.append(("sweep the board", "approved 3 MRs"))

    snapshot = info.snapshot()

    assert snapshot["turn_history"] == [["sweep the board", "approved 3 MRs"]]


def test_snapshot_with_transcript_includes_entries() -> None:
    """``with_transcript=True`` appends the serialised transcript entries."""
    info = _info()
    info.transcript.append(TranscriptEntry(role="user", text="hello", timestamp=1.5))
    info.transcript.append(TranscriptEntry(role="assistant", text="hi", timestamp=2.5))

    snapshot = info.snapshot(with_transcript=True)

    assert snapshot["transcript"] == [
        {"role": "user", "text": "hello", "timestamp": 1.5},
        {"role": "assistant", "text": "hi", "timestamp": 2.5},
    ]


def test_transcript_entry_as_dict() -> None:
    """``TranscriptEntry.as_dict`` returns the JSON-serialisable form."""
    entry = TranscriptEntry(role="parent", text="steer left", timestamp=9.0)

    assert entry.as_dict() == {
        "role": "parent",
        "text": "steer left",
        "timestamp": 9.0,
    }


def test_inbox_message_as_dict() -> None:
    """``InboxMessage.as_dict`` returns the JSON-serialisable form."""
    message = InboxMessage(role="user", text="deploy the fix", timestamp=9.5)

    assert message.as_dict() == {
        "role": "user",
        "text": "deploy the fix",
        "timestamp": 9.5,
    }


def test_on_close_enum_value() -> None:
    """``SubsessionKind.ON_CLOSE`` has the string value ``"on_close"``."""
    assert SubsessionKind.ON_CLOSE.value == "on_close"
    assert isinstance(SubsessionKind.ON_CLOSE.value, str)


def test_snapshot_on_close_serialises_kind() -> None:
    """``ON_CLOSE`` kind is serialised as ``"on_close"`` in snapshots."""
    snapshot = _info(kind=SubsessionKind.ON_CLOSE).snapshot()
    assert snapshot["kind"] == "on_close"


def test_is_active_matches_active_statuses() -> None:
    """``is_active`` is True exactly for the ``ACTIVE_STATUSES`` members."""
    for status in SubsessionStatus:
        info = _info(status=status)
        assert info.is_active is (status in ACTIVE_STATUSES)


def test_active_statuses_membership() -> None:
    """Running/waiting/sleeping are active; terminal statuses are not."""
    assert SubsessionStatus.RUNNING in ACTIVE_STATUSES
    assert SubsessionStatus.WAITING in ACTIVE_STATUSES
    assert SubsessionStatus.SLEEPING in ACTIVE_STATUSES
    assert SubsessionStatus.CLOSED not in ACTIVE_STATUSES
    assert SubsessionStatus.FAILED not in ACTIVE_STATUSES
    assert SubsessionStatus.INTERRUPTED not in ACTIVE_STATUSES

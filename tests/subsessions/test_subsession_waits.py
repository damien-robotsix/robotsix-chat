"""Unit tests for the wait_for_event monitor prompt builder."""

from robotsix_chat.subsessions.models import (
    SubsessionInfo,
    SubsessionKind,
    SubsessionStatus,
)
from robotsix_chat.subsessions.subsession_waits import _build_wait_for_event_input


def _make_info() -> SubsessionInfo:
    return SubsessionInfo(
        id="sub-wfe",
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="event monitor",
        prompt="monitor ticket T-123",
        model_level=3,
        status=SubsessionStatus.RUNNING,
        created_at=0.0,
        last_activity_at=0.0,
        dedup_key="T-123",
        checkpoint={"ticket_id": "T-123"},
        event_timeout_seconds=5.0,
    )


def test_wait_for_event_prompt_requires_history_merge_evidence_check() -> None:
    """Require a history merge-evidence check before a 'without PR' verdict.

    Before classifying a closure as 'without a PR', the monitor must
    cross-reference the ticket history for merge/implementation evidence.
    """
    result = _build_wait_for_event_input(
        _make_info(), previous_result=None, steering=[]
    )

    assert "MERGE-EVIDENCE CHECK" in result
    # Missing PR metadata is indeterminate, not proof the ticket was dropped.
    assert "INDETERMINATE" in result
    # Must consult the ticket state-change history for merge evidence.
    assert "/tickets/{id}/history" in result
    assert "IMPLEMENT_COMPLETE" in result
    # Indeterminate cases prompt verification rather than asserting a drop.
    assert "verification recommended" in result

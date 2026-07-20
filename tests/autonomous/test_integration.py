"""Integration tests for the autonomous session lifecycle.

Covers: approval gate, marker detection, auto-continue, max_auto_turns,
approve/reject endpoints, and AutonomousRunner wiring.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from robotsix_chat.autonomous import (
    AutonomousRunner,
    AutonomousState,
)
from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import EventBus
from robotsix_chat.config.models import AutonomousSettings
from tests.conftest import MockAgent, mock_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conv_store() -> ConversationStore:
    """Return a fresh, non-persisting ConversationStore."""
    return ConversationStore(max_history_turns=10, max_conversations=50)


@pytest.fixture
def event_bus() -> EventBus:
    """Return a fresh EventBus."""
    return EventBus()


@pytest.fixture
def autonomous_settings() -> AutonomousSettings:
    """Return AutonomousSettings with default markers and low max_auto_turns."""
    return AutonomousSettings(
        enabled=True,
        max_auto_turns=5,
        completion_marker="---AUTONOMOUS COMPLETE---",
        approval_marker="---AWAITING APPROVAL---",
    )


@pytest.fixture
def runner(
    conv_store: ConversationStore,
    event_bus: EventBus,
    autonomous_settings: AutonomousSettings,
) -> AutonomousRunner:
    """Return an AutonomousRunner wired to a fresh store/bus."""
    agent = MagicMock()
    agent_factory = MagicMock(return_value=agent)
    return AutonomousRunner(
        conversation_store=conv_store,
        event_bus=event_bus,
        agent_factory=agent_factory,
        settings=autonomous_settings,
    )


# ---------------------------------------------------------------------------
# Approval gate: chat endpoint rejects messages in awaiting_approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_rejects_awaiting_approval_session(
    conv_store: ConversationStore,
    event_bus: EventBus,
    autonomous_settings: AutonomousSettings,
) -> None:
    """POST /chat on an autonomous session in awaiting_approval returns 409."""
    runner = AutonomousRunner(
        conversation_store=conv_store,
        event_bus=event_bus,
        agent_factory=MagicMock(),
        settings=autonomous_settings,
    )

    # Create an owner with an autonomous session.
    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="autonomous")
    session_id: str = str(session["session_id"])

    # Transition to awaiting_approval.
    runner.transition_state(session_id, AutonomousState.AWAITING_APPROVAL)

    async with mock_app(
        conversation_store=conv_store,
        event_bus=event_bus,
        autonomous_enabled=True,
        autonomous_runner=runner,
    ) as f:
        response = await f.client.post(
            "/chat",
            json={
                "message": "do something",
                "session_id": session_id,
                "owner_id": owner_id,
            },
        )

    assert response.status_code == 409
    data = response.json()
    assert "awaiting plan approval" in data["error"]


@pytest.mark.asyncio
async def test_chat_endpoint_allows_non_autonomous_session(
    conv_store: ConversationStore,
    event_bus: EventBus,
    autonomous_settings: AutonomousSettings,
) -> None:
    """POST /chat on a regular chat session is not blocked by the gate."""
    runner = AutonomousRunner(
        conversation_store=conv_store,
        event_bus=event_bus,
        agent_factory=MagicMock(return_value=MockAgent("ok")),
        settings=autonomous_settings,
    )

    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="chat")
    session_id: str = str(session["session_id"])

    async with mock_app(
        conversation_store=conv_store,
        event_bus=event_bus,
        autonomous_enabled=True,
        autonomous_runner=runner,
    ) as f:
        response = await f.client.post(
            "/chat",
            json={
                "message": "hello",
                "session_id": session_id,
                "owner_id": owner_id,
            },
        )

    # Should succeed (200 + SSE), not 409.
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Marker detection
# ---------------------------------------------------------------------------


def test_approval_marker_detection(
    runner: AutonomousRunner,
    conv_store: ConversationStore,
) -> None:
    """The approval marker in a reply triggers transition to awaiting_approval."""
    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="autonomous")
    session_id: str = str(session["session_id"])

    # State should be None initially.
    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state is None

    reply = "Here is my plan:\n1. Do X\n2. Do Y\n\n---AWAITING APPROVAL---"
    new_state = runner.check_reply_for_markers(session_id, reply)

    assert new_state == AutonomousState.AWAITING_APPROVAL
    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state == AutonomousState.AWAITING_APPROVAL.value
    # Plan text should be stored.
    assert s.autonomous_plan is not None
    assert "Here is my plan" in s.autonomous_plan
    assert "---AWAITING APPROVAL---" not in s.autonomous_plan


def test_completion_marker_detection(
    runner: AutonomousRunner,
    conv_store: ConversationStore,
) -> None:
    """The completion marker in a reply triggers transition to completed."""
    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="autonomous")
    session_id: str = str(session["session_id"])

    # Transition to executing first.
    runner.transition_state(session_id, AutonomousState.EXECUTING)
    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state == AutonomousState.EXECUTING.value

    reply = "All done. The work is complete.\n\n---AUTONOMOUS COMPLETE---"
    new_state = runner.check_reply_for_markers(session_id, reply)

    assert new_state == AutonomousState.COMPLETED
    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state == AutonomousState.COMPLETED.value


def test_marker_ignored_in_wrong_state(
    runner: AutonomousRunner,
    conv_store: ConversationStore,
) -> None:
    """The completion marker is ignored when not in executing state."""
    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="autonomous")
    session_id: str = str(session["session_id"])

    # In selecting_subject, completion marker should be ignored.
    runner.transition_state(session_id, AutonomousState.SELECTING_SUBJECT)
    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state == AutonomousState.SELECTING_SUBJECT.value

    reply = "---AUTONOMOUS COMPLETE---"
    new_state = runner.check_reply_for_markers(session_id, reply)

    assert new_state is None  # NOT transitioned
    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state == AutonomousState.SELECTING_SUBJECT.value


def test_no_marker_no_transition(
    runner: AutonomousRunner,
    conv_store: ConversationStore,
) -> None:
    """A reply without any marker does not trigger a state change."""
    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="autonomous")
    session_id: str = str(session["session_id"])

    runner.transition_state(session_id, AutonomousState.EXECUTING)
    reply = "Working on step 3..."
    new_state = runner.check_reply_for_markers(session_id, reply)

    assert new_state is None
    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state == AutonomousState.EXECUTING.value


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def test_transition_state_publishes_event(
    runner: AutonomousRunner,
    conv_store: ConversationStore,
    event_bus: EventBus,
) -> None:
    """transition_state publishes an SSE event on the session's channel."""
    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="autonomous")
    session_id: str = str(session["session_id"])

    # Subscribe to the session's events.
    import asyncio

    queue: asyncio.Queue[dict[str, object]] = event_bus.subscribe(session_id)

    ok = runner.transition_state(session_id, AutonomousState.EXECUTING)
    assert ok

    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state == AutonomousState.EXECUTING.value

    # Event should be on the queue.
    frame = queue.get_nowait()
    assert frame["type"] == "autonomous_state_changed"
    assert frame["session_id"] == session_id
    assert frame["new_state"] == "executing"


def test_transition_state_ignores_non_autonomous(
    runner: AutonomousRunner,
    conv_store: ConversationStore,
) -> None:
    """transition_state returns False for non-autonomous sessions."""
    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="chat")
    session_id: str = str(session["session_id"])

    ok = runner.transition_state(session_id, AutonomousState.EXECUTING)
    assert not ok


# ---------------------------------------------------------------------------
# max_auto_turns enforcement
# ---------------------------------------------------------------------------


def test_max_auto_turns_exceeded(
    runner: AutonomousRunner,
    conv_store: ConversationStore,
) -> None:
    """When max_auto_turns is exceeded, session reverts to awaiting_approval."""
    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="autonomous")
    session_id: str = str(session["session_id"])

    runner.transition_state(session_id, AutonomousState.EXECUTING)

    # 5 turns should be fine (max_auto_turns=5).
    for _ in range(5):
        exceeded = runner.count_execution_turn(session_id)
        assert not exceeded
    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state == AutonomousState.EXECUTING.value

    # The 6th turn exceeds the limit.
    exceeded = runner.count_execution_turn(session_id)
    assert exceeded
    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state == AutonomousState.AWAITING_APPROVAL.value


def test_count_execution_turn_ignores_non_executing(
    runner: AutonomousRunner,
    conv_store: ConversationStore,
) -> None:
    """count_execution_turn is a no-op when not in executing state."""
    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="autonomous")
    session_id: str = str(session["session_id"])

    # Not yet executing.
    exceeded = runner.count_execution_turn(session_id)
    assert not exceeded

    # In selecting_subject.
    runner.transition_state(session_id, AutonomousState.SELECTING_SUBJECT)
    exceeded = runner.count_execution_turn(session_id)
    assert not exceeded


# ---------------------------------------------------------------------------
# Approve / reject endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_endpoint_transitions_to_executing(
    conv_store: ConversationStore,
    event_bus: EventBus,
    autonomous_settings: AutonomousSettings,
) -> None:
    """POST /sessions/{id}/approve transitions awaiting_approval → executing."""
    runner = AutonomousRunner(
        conversation_store=conv_store,
        event_bus=event_bus,
        agent_factory=MagicMock(),
        settings=autonomous_settings,
    )

    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="autonomous")
    session_id: str = str(session["session_id"])

    # Transition to awaiting_approval.
    runner.transition_state(session_id, AutonomousState.AWAITING_APPROVAL)

    async with mock_app(
        conversation_store=conv_store,
        event_bus=event_bus,
        autonomous_enabled=True,
        autonomous_runner=runner,
    ) as f:
        response = await f.client.post(f"/sessions/{session_id}/approve")

    assert response.status_code == 200
    data = response.json()
    assert data["approved"] is True
    assert data["session_id"] == session_id
    assert data["new_state"] == "executing"

    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state == AutonomousState.EXECUTING.value


@pytest.mark.asyncio
async def test_reject_endpoint_transitions_to_selecting_subject(
    conv_store: ConversationStore,
    event_bus: EventBus,
    autonomous_settings: AutonomousSettings,
) -> None:
    """POST /sessions/{id}/reject transitions awaiting_approval → selecting_subject."""
    runner = AutonomousRunner(
        conversation_store=conv_store,
        event_bus=event_bus,
        agent_factory=MagicMock(),
        settings=autonomous_settings,
    )

    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="autonomous")
    session_id: str = str(session["session_id"])

    runner.transition_state(session_id, AutonomousState.AWAITING_APPROVAL)

    async with mock_app(
        conversation_store=conv_store,
        event_bus=event_bus,
        autonomous_enabled=True,
        autonomous_runner=runner,
    ) as f:
        response = await f.client.post(f"/sessions/{session_id}/reject")

    assert response.status_code == 200
    data = response.json()
    assert data["rejected"] is True
    assert data["new_state"] == "selecting_subject"

    s = conv_store.get_session(session_id)
    assert s is not None
    assert s.autonomous_state == AutonomousState.SELECTING_SUBJECT.value


@pytest.mark.asyncio
async def test_approve_endpoint_409_when_not_awaiting_approval(
    conv_store: ConversationStore,
    event_bus: EventBus,
    autonomous_settings: AutonomousSettings,
) -> None:
    """Approving a session not in awaiting_approval returns 409."""
    runner = AutonomousRunner(
        conversation_store=conv_store,
        event_bus=event_bus,
        agent_factory=MagicMock(),
        settings=autonomous_settings,
    )

    owner_id = "owner-1"
    session = conv_store.create_session(owner_id, kind="autonomous")
    session_id: str = str(session["session_id"])

    # Session is in selecting_subject (default after create + transition)
    runner.transition_state(session_id, AutonomousState.SELECTING_SUBJECT)

    async with mock_app(
        conversation_store=conv_store,
        event_bus=event_bus,
        autonomous_enabled=True,
        autonomous_runner=runner,
    ) as f:
        response = await f.client.post(f"/sessions/{session_id}/approve")

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# AutonomousRunner wiring
# ---------------------------------------------------------------------------


def test_runner_stored_in_app_state(
    conv_store: ConversationStore,
    event_bus: EventBus,
    autonomous_settings: AutonomousSettings,
) -> None:
    """The AutonomousRunner is accessible via app.state.autonomous_runner."""
    runner = AutonomousRunner(
        conversation_store=conv_store,
        event_bus=event_bus,
        agent_factory=MagicMock(),
        settings=autonomous_settings,
    )

    from robotsix_chat.chat.server import create_app

    app = create_app(
        MagicMock(return_value=MockAgent("ok")),
        autonomous_enabled=True,
        autonomous_runner=runner,
        conversation_store=conv_store,
        event_bus=event_bus,
    )

    assert app.state.autonomous_runner is runner
    assert app.state.autonomous_enabled is True


def test_resume_autonomous_sessions(
    runner: AutonomousRunner,
    conv_store: ConversationStore,
) -> None:
    """resume_autonomous_sessions re-publishes active sessions."""
    owner_id = "owner-1"

    # Create an autonomous session in AWAITING_APPROVAL.
    s1 = conv_store.create_session(owner_id, kind="autonomous")
    sid1: str = str(s1["session_id"])
    runner.transition_state(sid1, AutonomousState.AWAITING_APPROVAL)

    # Create a COMPLETED session.
    s2 = conv_store.create_session(owner_id, kind="autonomous")
    sid2: str = str(s2["session_id"])
    runner.transition_state(sid2, AutonomousState.COMPLETED)

    # Create a regular chat session (should be ignored).
    s3 = conv_store.create_session(owner_id, kind="chat")
    _sid3: str = str(s3["session_id"])

    # resume_autonomous_sessions should not raise.
    runner.resume_autonomous_sessions()

    # The COMPLETED session should get auto-closed eventually (async task).
    # The AWAITING_APPROVAL session should still be in that state.
    s = conv_store.get_session(sid1)
    assert s is not None
    assert s.autonomous_state == AutonomousState.AWAITING_APPROVAL.value


# ---------------------------------------------------------------------------
# ConversationStore public methods
# ---------------------------------------------------------------------------


def test_owner_for_session(
    conv_store: ConversationStore,
) -> None:
    """owner_for_session returns the correct owner_id."""
    conv_store.create_session("alice", kind="chat")
    s = conv_store.create_session("alice", kind="chat")
    sid: str = str(s["session_id"])
    conv_store.create_session("bob", kind="chat")

    assert conv_store.owner_for_session(sid) == "alice"
    assert conv_store.owner_for_session("nonexistent") is None


def test_iter_sessions(
    conv_store: ConversationStore,
) -> None:
    """iter_sessions returns all tracked sessions."""
    s1 = conv_store.create_session("alice", kind="chat")
    s2 = conv_store.create_session("bob", kind="autonomous")

    sessions = conv_store.iter_sessions()
    session_ids = {s.session_id for s in sessions}
    assert s1["session_id"] in session_ids
    assert s2["session_id"] in session_ids

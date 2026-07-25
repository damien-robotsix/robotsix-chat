"""Integration tests for autonomous session endpoints (approve/reject)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from robotsix_chat.autonomous.models import AutonomousState
from robotsix_chat.autonomous.runner import AutonomousRunner
from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import SSE_AUTONOMOUS_STATE_TYPE
from robotsix_chat.chat.server.app import create_app
from robotsix_chat.chat.server.routes.chat import RunSerializer
from robotsix_chat.llm import LlmioChatAgent


@pytest.fixture
def owner_id() -> str:
    """Return a unique owner ID to prevent collisions under xdist parallelism."""
    return f"owner-{uuid.uuid4().hex}"


@pytest.fixture
def other_owner_id() -> str:
    """Return a unique owner ID representing a different owner."""
    return f"owner-{uuid.uuid4().hex}"


@pytest.fixture
def store() -> ConversationStore:
    """Fresh in-memory conversation store."""
    return ConversationStore()


@pytest.fixture
def mock_agent() -> LlmioChatAgent:
    """Mock agent that never actually streams."""
    agent = MagicMock(spec=LlmioChatAgent)
    agent.stream = MagicMock()
    return agent


@pytest.fixture
def autonomous_runner(store, tmp_path, monkeypatch) -> AutonomousRunner:
    """Runner wired to the mock store with default markers.

    Persistence and background-task methods are mocked at the class
    level *before* construction so that :meth:`AutonomousRunner.__init__`
    never calls the real ``_load_sessions`` (which reads from disk).
    Instance-level mocking after construction (as done previously) still
    lets the constructor touch the filesystem and leaves a window for
    cross-test leakage under CI parallelism / filesystem contention.
    """
    # Prevent background tasks (from approve/reject) from leaking across
    # xdist workers or between tests sharing an event loop.
    monkeypatch.setattr(AutonomousRunner, "_schedule_background", MagicMock())
    settings = MagicMock()
    settings.autonomous.proposal_marker = "---PROPOSAL READY---"
    settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
    settings.autonomous.max_auto_turns = 20
    settings.autonomous.session_color = "#ff0000"
    settings.autonomous.persist_path = str(tmp_path / "autonomous_sessions.json")
    settings.autonomous.initial_task = ""
    runner = AutonomousRunner(
        settings=settings,
        conversation_store=store,
        agent_factory=MagicMock(),
        run_serializer=RunSerializer(),
    )
    return runner


@pytest_asyncio.fixture
async def client(mock_agent, store, autonomous_runner):
    """Async HTTP client pointed at a create_app instance with autonomous runner."""
    app = create_app(
        mock_agent,
        conversation_store=store,
        autonomous_runner=autonomous_runner,
        serve_ui=False,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestProposalMessageTriggersExecution:
    """POST /chat triggers execution when autonomous session is in proposal state."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_chat_succeeds_when_proposal(
        self, client, autonomous_runner, store, mock_agent, owner_id
    ):
        """Messages to a proposal session are accepted (triggers execution)."""
        sid = store.create_session(owner_id)["session_id"]
        aq = autonomous_runner.create_session(
            owner_id, session_id=sid, schedule_kickoff=False
        )
        aq.state = AutonomousState.proposal
        r = await client.post(
            "/chat",
            json={
                "message": "Hello",
                "session_id": sid,
                "owner_id": owner_id,
            },
        )
        assert r.status_code == 200  # proposal sessions accept messages

    @pytest.mark.asyncio
    async def test_chat_allows_when_not_proposal(
        self, client, autonomous_runner, store, owner_id
    ):
        """Messages to a non-awaiting autonomous session are not blocked."""
        sid = store.create_session(owner_id)["session_id"]
        autonomous_runner.create_session(
            owner_id, session_id=sid, schedule_kickoff=False
        )
        r = await client.post(
            "/chat",
            json={
                "message": "Hello",
                "session_id": sid,
                "owner_id": owner_id,
            },
        )
        assert r.status_code != 409

    @pytest.mark.asyncio
    async def test_chat_allows_non_autonomous_session(self, client, store, owner_id):
        """Messages to a non-autonomous session are never blocked."""
        sid = store.create_session(owner_id)["session_id"]
        r = await client.post(
            "/chat",
            json={
                "message": "Hello",
                "session_id": sid,
                "owner_id": owner_id,
            },
        )
        assert r.status_code != 409


class TestRunnerWiring:
    """Verify the autonomous runner is wired into the app correctly."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_runner_on_app_state(self, client):
        """App starts with autonomous runner on state and health passes."""
        r = await client.get("/health")
        assert r.status_code == 200


class TestSessionsListAutonomousAnnotation:
    """GET /sessions returns autonomous annotations for autonomous sessions."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_sessions_list_includes_autonomous_fields(
        self, client, autonomous_runner, store, owner_id
    ):
        """GET /sessions returns 200 with autonomous annotations."""
        sid = store.create_session(owner_id)["session_id"]
        aq = autonomous_runner.create_session(owner_id, session_id=sid)
        aq.state = AutonomousState.proposal
        aq.plan_text = "Draft plan text"
        aq.auto_turn_count = 3

        r = await client.get(f"/sessions?owner_id={owner_id}")
        assert r.status_code == 200
        sessions = r.json()["sessions"]
        assert len(sessions) == 1
        s = sessions[0]
        assert s["session_id"] == sid
        assert s["autonomous"] is True
        assert s[SSE_AUTONOMOUS_STATE_TYPE] == "proposal"
        assert s["autonomous_plan_text"] == "Draft plan text"
        assert s["autonomous_turn_count"] == 3
        assert s["autonomous_max_turns"] == 20
        assert s["autonomous_session_color"] == "#ff0000"

    @pytest.mark.asyncio
    async def test_autonomous_session_listed_without_prior_store_session(
        self, client, autonomous_runner, store, owner_id
    ):
        """An autonomous session appears in GET /sessions with no prior store session.

        Regression for the UI-invisibility bug: the runner used to only call
        ``store.begin`` (global registration, not owner-linked), so unless an
        ordinary store session already existed for the owner, the autonomous
        session never showed up in ``list_sessions``.  Creating it via the
        runner alone must be enough.
        """
        # NOTE: no store.create_session(owner_id) here — the runner must
        # register the session under the owner by itself.
        aq = autonomous_runner.create_session(
            owner_id, session_id=None, schedule_kickoff=False
        )

        r = await client.get(f"/sessions?owner_id={owner_id}")
        assert r.status_code == 200
        sessions = r.json()["sessions"]
        match = [s for s in sessions if s["session_id"] == aq.session_id]
        assert len(match) == 1
        assert match[0]["autonomous"] is True

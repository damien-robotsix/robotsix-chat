"""Integration tests for autonomous session endpoints (approve/reject)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from robotsix_chat.autonomous.models import AutonomousState
from robotsix_chat.autonomous.runner import AutonomousRunner
from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.server.app import create_app
from robotsix_chat.chat.server.routes.chat import RunSerializer
from robotsix_chat.llm import LlmioChatAgent


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
def autonomous_runner(store) -> AutonomousRunner:
    """Runner wired to the mock store with default markers."""
    settings = MagicMock()
    settings.autonomous.approval_marker = "---AWAITING APPROVAL---"
    settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
    settings.autonomous.max_auto_turns = 20
    return AutonomousRunner(
        settings=settings,
        conversation_store=store,
        agent_factory=MagicMock(),
        run_serializer=RunSerializer(),
    )


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


class TestApproveEndpoint:
    """POST /sessions/{id}/approve tests."""

    @pytest.mark.asyncio
    async def test_approve_requires_owner_id(self, client, autonomous_runner, store):
        """Missing owner_id returns 400."""
        sid = store.create_session("owner1")["session_id"]
        aq = autonomous_runner.create_session("owner1", session_id=sid)
        aq.state = AutonomousState.awaiting_approval
        r = await client.post(f"/sessions/{sid}/approve")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_approve_success(self, client, autonomous_runner, store):
        """Valid approve transitions to executing and returns 200."""
        sid = store.create_session("owner1")["session_id"]
        aq = autonomous_runner.create_session("owner1", session_id=sid)
        aq.state = AutonomousState.awaiting_approval
        r = await client.post(f"/sessions/{sid}/approve?owner_id=owner1")
        assert r.status_code == 200
        data = r.json()
        assert data["approved"] is True
        assert aq.state is AutonomousState.executing

    @pytest.mark.asyncio
    async def test_approve_wrong_owner_returns_403(
        self, client, autonomous_runner, store
    ):
        """Mismatched owner_id returns 403."""
        sid = store.create_session("owner1")["session_id"]
        aq = autonomous_runner.create_session("owner1", session_id=sid)
        aq.state = AutonomousState.awaiting_approval
        r = await client.post(f"/sessions/{sid}/approve?owner_id=owner2")
        assert r.status_code == 403
        data = r.json()
        assert "owner_id mismatch" in data["error"]

    @pytest.mark.asyncio
    async def test_approve_unknown_session_returns_404(self, client):
        """Unknown session returns 404."""
        r = await client.post("/sessions/nonexistent/approve?owner_id=owner1")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_approve_wrong_state_returns_409(
        self, client, autonomous_runner, store
    ):
        """Approving when not awaiting_approval returns 409."""
        sid = store.create_session("owner1")["session_id"]
        autonomous_runner.create_session("owner1", session_id=sid)
        r = await client.post(f"/sessions/{sid}/approve?owner_id=owner1")
        assert r.status_code == 409


class TestRejectEndpoint:
    """POST /sessions/{id}/reject tests."""

    @pytest.mark.asyncio
    async def test_reject_success(self, client, autonomous_runner, store):
        """Valid reject resets to selecting_subject and returns 200."""
        sid = store.create_session("owner1")["session_id"]
        aq = autonomous_runner.create_session("owner1", session_id=sid)
        aq.state = AutonomousState.awaiting_approval
        r = await client.post(f"/sessions/{sid}/reject?owner_id=owner1")
        assert r.status_code == 200
        data = r.json()
        assert data["rejected"] is True
        assert aq.state is AutonomousState.selecting_subject

    @pytest.mark.asyncio
    async def test_reject_wrong_owner_returns_403(
        self, client, autonomous_runner, store
    ):
        """Mismatched owner_id returns 403."""
        sid = store.create_session("owner1")["session_id"]
        aq = autonomous_runner.create_session("owner1", session_id=sid)
        aq.state = AutonomousState.awaiting_approval
        r = await client.post(f"/sessions/{sid}/reject?owner_id=owner2")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_reject_unknown_session_returns_404(self, client):
        """Unknown session returns 404."""
        r = await client.post("/sessions/nonexistent/reject?owner_id=owner1")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_reject_wrong_state_returns_409(
        self, client, autonomous_runner, store
    ):
        """Rejecting when not awaiting_approval returns 409."""
        sid = store.create_session("owner1")["session_id"]
        autonomous_runner.create_session("owner1", session_id=sid)
        r = await client.post(f"/sessions/{sid}/reject?owner_id=owner1")
        assert r.status_code == 409


class TestApprovalGate409:
    """POST /chat returns 409 when autonomous session is awaiting approval."""

    @pytest.mark.asyncio
    async def test_chat_returns_409_when_awaiting_approval(
        self, client, autonomous_runner, store, mock_agent
    ):
        """Messages to an awaiting_approval session are rejected with 409."""
        sid = store.create_session("owner1")["session_id"]
        aq = autonomous_runner.create_session("owner1", session_id=sid)
        aq.state = AutonomousState.awaiting_approval
        r = await client.post(
            "/chat",
            json={"message": "Hello", "session_id": sid, "owner_id": "owner1"},
        )
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_chat_allows_when_not_awaiting_approval(
        self, client, autonomous_runner, store
    ):
        """Messages to a non-awaiting autonomous session are not blocked."""
        sid = store.create_session("owner1")["session_id"]
        autonomous_runner.create_session("owner1", session_id=sid)
        r = await client.post(
            "/chat",
            json={"message": "Hello", "session_id": sid, "owner_id": "owner1"},
        )
        assert r.status_code != 409

    @pytest.mark.asyncio
    async def test_chat_allows_non_autonomous_session(self, client, store):
        """Messages to a non-autonomous session are never blocked."""
        sid = store.create_session("owner1")["session_id"]
        r = await client.post(
            "/chat",
            json={"message": "Hello", "session_id": sid, "owner_id": "owner1"},
        )
        assert r.status_code != 409


class TestRunnerWiring:
    """Verify the autonomous runner is wired into the app correctly."""

    @pytest.mark.asyncio
    async def test_runner_on_app_state(self, client):
        """App starts with autonomous runner on state and health passes."""
        r = await client.get("/health")
        assert r.status_code == 200

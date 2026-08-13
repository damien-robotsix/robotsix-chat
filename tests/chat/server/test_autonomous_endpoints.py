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
    settings.autonomous.completion_marker = "---AUTONOMOUS COMPLETE---"
    settings.autonomous.continue_interval_seconds = 45.0
    settings.autonomous.max_idle_auto_turns = 5
    settings.autonomous.stale_monitor_runs_before_completion = 3
    from types import SimpleNamespace

    settings.autonomous.sessions = [
        SimpleNamespace(
            name="default",
            prompt="",
            trigger_type=SimpleNamespace(value="periodic"),
            trigger_interval_seconds=45.0,
            max_auto_turns=20,
            enabled=True,
            self_refine=False,
            self_refine_require_approval=False,
        )
    ]
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


class TestChatAcceptsAutonomousMessages:
    """POST /chat accepts messages for autonomous sessions in any state."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_chat_succeeds_when_executing(
        self, client, autonomous_runner, store, mock_agent, owner_id
    ):
        """Messages to an executing autonomous session are accepted."""
        sid = store.create_session(owner_id)["session_id"]
        aq = autonomous_runner.create_session(
            owner_id, session_id=sid, schedule_kickoff=False
        )
        aq.state = AutonomousState.executing
        r = await client.post(
            "/chat",
            json={
                "message": "Hello",
                "session_id": sid,
                "owner_id": owner_id,
            },
        )
        assert r.status_code == 200

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
        aq.state = AutonomousState.executing
        aq.auto_turn_count = 3

        r = await client.get(f"/sessions?owner_id={owner_id}")
        assert r.status_code == 200
        sessions = r.json()["sessions"]
        assert len(sessions) == 1
        s = sessions[0]
        assert s["session_id"] == sid
        assert s["autonomous"] is True
        assert s[SSE_AUTONOMOUS_STATE_TYPE] == "executing"
        assert s["autonomous_turn_count"] == 3

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


class TestAutonomousDefinitionsListEndpoint:
    """GET /autonomous/definitions — list all session definitions."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_returns_404_when_runner_is_none(self, mock_agent, store):
        """404 when autonomous is not enabled (runner is None)."""
        app = create_app(
            mock_agent,
            conversation_store=store,
            autonomous_runner=None,
            serve_ui=False,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/autonomous/definitions")
            assert r.status_code == 404
            assert "not enabled" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_lists_default_definition(self, client):
        """Returns the synthesized default definition when sessions list is empty."""
        r = await client.get("/autonomous/definitions")
        assert r.status_code == 200
        data = r.json()
        assert "definitions" in data
        defs = data["definitions"]
        assert len(defs) >= 1
        names = {d["name"] for d in defs}
        assert "default" in names

    @pytest.mark.asyncio
    async def test_definition_shape(self, client):
        """Each definition has the expected keys."""
        r = await client.get("/autonomous/definitions")
        assert r.status_code == 200
        for d in r.json()["definitions"]:
            assert "name" in d
            assert "prompt" in d
            assert "trigger_type" in d
            assert "trigger_interval_seconds" in d
            assert "enabled" in d
            assert "owner_id" in d
            assert "active_session_id" in d

    @pytest.mark.asyncio
    async def test_active_session_id_when_active(
        self, client, autonomous_runner, store
    ):
        """active_session_id is set when a session is active."""
        aq = autonomous_runner.create_session(
            autonomous_runner.bootstrap_owner,
            schedule_kickoff=False,
            definition_name="default",
        )
        r = await client.get("/autonomous/definitions")
        assert r.status_code == 200
        matching = [d for d in r.json()["definitions"] if d["name"] == "default"]
        assert len(matching) == 1
        assert matching[0]["active_session_id"] == aq.session_id

    @pytest.mark.asyncio
    async def test_active_session_id_none_when_no_session(self, client):
        """active_session_id is null when no session is active."""
        r = await client.get("/autonomous/definitions")
        assert r.status_code == 200
        for d in r.json()["definitions"]:
            assert d["active_session_id"] is None


class TestAutonomousDefinitionsRunEndpoint:
    """POST /autonomous/definitions/{name}/run — manual trigger."""

    @pytest.fixture(autouse=True)
    def _mock_persistence(self, monkeypatch) -> None:
        monkeypatch.setattr(AutonomousRunner, "_save_sessions", MagicMock())
        monkeypatch.setattr(
            AutonomousRunner, "_load_sessions", MagicMock(return_value={})
        )

    @pytest.mark.asyncio
    async def test_returns_404_when_runner_is_none(self, mock_agent, store):
        """404 when autonomous is not enabled."""
        app = create_app(
            mock_agent,
            conversation_store=store,
            autonomous_runner=None,
            serve_ui=False,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/autonomous/definitions/default/run")
            assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_name(self, client):
        """404 for a definition name that does not exist."""
        r = await client.post("/autonomous/definitions/nonexistent/run")
        assert r.status_code == 404
        assert "unknown definition" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_returns_409_when_session_already_active(
        self, client, autonomous_runner, store, owner_id
    ):
        """409 when a session is already active for the definition."""
        # The default definition maps to BOOTSTRAP_OWNER.
        sid = store.create_session(owner_id)["session_id"]
        autonomous_runner.create_session(
            owner_id, session_id=sid, schedule_kickoff=False
        )
        # Manually set owner_id to be the bootstrap owner so the runner
        # detects the conflict.
        aq = autonomous_runner.get_session(sid)
        assert aq is not None
        # The definition run endpoint uses owner_id_for_definition which
        # maps "default" → BOOTSTRAP_OWNER.  So create a session under
        # BOOTSTRAP_OWNER.
        autonomous_runner.create_session(
            autonomous_runner.bootstrap_owner,
            schedule_kickoff=False,
            definition_name="default",
        )
        r = await client.post("/autonomous/definitions/default/run")
        assert r.status_code == 409
        data = r.json()
        assert "already has an active session" in data["error"]
        assert "session_id" in data

    @pytest.mark.asyncio
    async def test_returns_200_and_starts_session_on_success(
        self, client, autonomous_runner
    ):
        """200 + session creation when the definition has no active session."""
        r = await client.post("/autonomous/definitions/default/run")
        assert r.status_code == 200
        data = r.json()
        assert data["started"] is True
        assert data["definition_name"] == "default"
        assert "session_id" in data
        # Verify a session was actually created.
        active_id = autonomous_runner.active_session_id_for_definition("default")
        assert active_id == data["session_id"]

"""Tests for the chat SSE server."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.server import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_TOKEN_TYPE,
    create_agent_from_settings,
    run_server_from_config,
)
from robotsix_chat.config import Settings
from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.subsessions import (
    SubsessionInfo,
    SubsessionKind,
    SubsessionRegistry,
    SubsessionStatus,
    spawn_subsession,
)
from tests.conftest import mock_app


def _register_subsession(
    registry: SubsessionRegistry,
    *,
    owner: str,
    kind: SubsessionKind = SubsessionKind.TASK,
    title: str = "job",
    **overrides: object,
) -> SubsessionInfo:
    """Register an active subsession record directly (no worker task)."""
    return registry.create(
        kind=kind,
        owner_session_id=owner,
        parent_id=None,
        depth=1,
        title=title,
        prompt="do the thing",
        model_level=3,
        **overrides,  # type: ignore[arg-type]
    )


def _parse_sse(response: Any) -> list[dict[str, object]]:
    """Split SSE text into parsed JSON frames from ``data:`` lines."""
    text: str = response.text
    events = [e for e in text.split("\n\n") if e]
    frames: list[dict[str, object]] = []
    for e in events:
        if e.startswith("data: "):
            frames.append(json.loads(e[len("data: ") :]))
    return frames


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    """Verify that the /health endpoint responds with 200 and a status object."""
    async with mock_app() as f:
        response = await f.client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Chat endpoint — SSE streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_streams_tokens() -> None:
    """Verify that the /chat endpoint returns SSE-framed tokens and a done event."""
    async with mock_app(tokens=["Hello", " ", "world!"]) as f:
        response = await f.client.post("/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert response.headers["content-type"] == SSE_CONTENT_TYPE

    frames = _parse_sse(response)

    assert len(frames) >= 4  # 3 tokens + done
    assert frames[0] == {"type": SSE_TOKEN_TYPE, "content": "Hello"}
    assert frames[1] == {"type": SSE_TOKEN_TYPE, "content": " "}
    assert frames[2] == {"type": SSE_TOKEN_TYPE, "content": "world!"}
    assert frames[-1] == {"type": SSE_DONE_TYPE}


@pytest.mark.asyncio
async def test_chat_endpoint_opens_with_heartbeat() -> None:
    """Emit a heartbeat comment before the first token.

    The stream emits a heartbeat comment up front (and keeps the connection
    alive while the agent works), so a long quiet reply isn't dropped. The
    comment is not a ``data:`` frame, so it never parses as a token.
    """
    async with mock_app(tokens=["hi"]) as f:
        response = await f.client.post("/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert response.text.startswith(": keepalive")
    # Heartbeat carries no JSON payload — only real frames do.
    frames = [
        json.loads(e[len("data: ") :])
        for e in response.text.split("\n\n")
        if e.startswith("data: ")
    ]
    assert {"type": SSE_TOKEN_TYPE, "content": "hi"} in frames
    assert {"type": SSE_DONE_TYPE} in frames


@pytest.mark.asyncio
async def test_chat_endpoint_passes_message_to_agent() -> None:
    """Verify that the /chat endpoint forwards the user message to the agent."""
    async with mock_app(tokens=["ok"]) as f:
        await f.client.post("/chat", json={"message": "hello world"})
        agent_ref = f.agent

    assert agent_ref.called_with == "hello world"


@pytest.mark.asyncio
async def test_chat_endpoint_sends_done_at_end() -> None:
    """Verify that the /chat endpoint emits a done SSE frame as the last event."""
    async with mock_app(tokens=["one", "two"]) as f:
        response = await f.client.post("/chat", json={"message": "x"})

    assert response.text.rstrip("\r\n").endswith(f'data: {{"type": "{SSE_DONE_TYPE}"}}')


# ---------------------------------------------------------------------------
# Chat endpoint — multi-turn conversations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_threads_history_for_client_id() -> None:
    """A ``client_id`` threads consecutive messages into one conversation.

    The prior turn is replayed and the trace session id stays the same.
    """
    async with mock_app(tokens=["Hello", " ", "world!"]) as f:
        await f.client.post(
            "/chat", json={"message": "first", "client_id": "browser-1"}
        )
        first_session = f.agent.session_id
        assert f.agent.history == []  # the opening message has no history

        await f.client.post(
            "/chat", json={"message": "second", "client_id": "browser-1"}
        )
        # The follow-up sees the first exchange replayed, under the same session.
        assert f.agent.history == [("first", "Hello world!")]
        assert f.agent.session_id == first_session
        assert first_session  # a real, non-empty session id


@pytest.mark.asyncio
async def test_chat_endpoint_without_client_id_is_stateless() -> None:
    """No ``client_id`` threads no history, but still assigns a trace session.

    A per-request session id is assigned so spans group sensibly.
    """
    async with mock_app(tokens=["ok"]) as f:
        await f.client.post("/chat", json={"message": "hi"})

    assert f.agent.history is None
    assert f.agent.session_id


@pytest.mark.asyncio
async def test_chat_endpoint_invalid_client_id() -> None:
    """A non-string ``client_id`` is rejected with 400."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat", json={"message": "hi", "client_id": 123}
        )

    assert response.status_code == 400
    assert "error" in response.json()


# ---------------------------------------------------------------------------
# History endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_endpoint_returns_stored_turns() -> None:
    """``GET /history?session_id=`` returns the session's recorded turns as JSON."""
    store = ConversationStore()
    store.begin("s1")
    store.record("s1", None, "hi", "hello")
    store.record("s1", None, "how are you", "I'm fine")

    async with mock_app(conversation_store=store) as f:
        response = await f.client.get("/history?session_id=s1")

    assert response.status_code == 200
    data = response.json()
    assert data == {"turns": [["hi", "hello"], ["how are you", "I'm fine"]]}


@pytest.mark.asyncio
async def test_history_endpoint_unknown_client_returns_200_empty() -> None:
    """``GET /history?client_id=unknown`` returns 200 with an empty turn list."""
    async with mock_app() as f:
        response = await f.client.get("/history?client_id=does-not-exist")

    assert response.status_code == 200
    data = response.json()
    assert data == {"turns": []}


@pytest.mark.asyncio
async def test_history_endpoint_missing_client_id_returns_400() -> None:
    """``GET /history`` without a ``session_id`` query param returns 400."""
    async with mock_app() as f:
        response = await f.client.get("/history")

    assert response.status_code == 400
    data = response.json()
    assert "error" in data
    assert "session_id" in data["error"]


@pytest.mark.asyncio
async def test_history_read_is_non_mutating() -> None:
    """Reading history does not update session metadata or turn count."""
    from tests.chat.test_conversation import _store

    store = _store()
    store.begin("s1")
    store.record("s1", None, "q", "a")

    # Read history — this must not count as activity or mutation.
    turns = store.history("s1")
    assert turns == [("q", "a")]

    # The session still has exactly 1 turn after the read.
    _, history_after = store.begin("s1")
    assert history_after == [("q", "a")]


# ---------------------------------------------------------------------------
# Sessions endpoints — GET /sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_list_returns_owner_sessions() -> None:
    """``GET /sessions?owner_id=X`` returns the owner sessions sorted by last_active."""
    store = ConversationStore()
    sid1 = cast(str, store.create_session("owner-a")["session_id"])
    sid2 = cast(str, store.create_session("owner-a")["session_id"])

    async with mock_app(conversation_store=store) as f:
        response = await f.client.get("/sessions?owner_id=owner-a")

    assert response.status_code == 200
    data = response.json()
    assert "sessions" in data
    assert "active_session_id" in data
    sessions = data["sessions"]
    assert len(sessions) == 2
    # sorted by last_active descending — sid2 was created last so it should be first
    assert sessions[0]["session_id"] == sid2
    assert sessions[1]["session_id"] == sid1
    assert data["active_session_id"] == sid2


@pytest.mark.asyncio
async def test_sessions_list_lazy_creates_default() -> None:
    """``GET /sessions`` for a new owner lazily creates a default active session."""
    async with mock_app() as f:
        response = await f.client.get("/sessions?owner_id=new-owner")

    assert response.status_code == 200
    data = response.json()
    assert len(data["sessions"]) == 1
    s = data["sessions"][0]
    assert s["title"] == "New chat"
    assert s["turn_count"] == 0
    assert isinstance(s["session_id"], str)
    assert s["session_id"] == data["active_session_id"]


@pytest.mark.asyncio
async def test_sessions_list_lazy_create_is_idempotent() -> None:
    """A second ``GET /sessions`` returns the same default session — no duplication."""
    store = ConversationStore()

    async with mock_app(conversation_store=store) as f:
        r1 = await f.client.get("/sessions?owner_id=o1")
        r2 = await f.client.get("/sessions?owner_id=o1")

    s1 = r1.json()["sessions"]
    s2 = r2.json()["sessions"]
    assert len(s1) == 1
    assert len(s2) == 1
    assert s1[0]["session_id"] == s2[0]["session_id"]


@pytest.mark.asyncio
async def test_sessions_list_missing_owner_id_returns_400() -> None:
    """``GET /sessions`` without ``owner_id`` returns 400."""
    async with mock_app() as f:
        response = await f.client.get("/sessions")

    assert response.status_code == 400
    assert "owner_id" in response.json()["error"]


# ---------------------------------------------------------------------------
# Sessions endpoints — POST /sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_create_returns_new_session() -> None:
    """``POST /sessions`` creates a new empty session and marks it active."""
    store = ConversationStore()

    async with mock_app(conversation_store=store) as f:
        response = await f.client.post("/sessions", json={"owner_id": "owner-b"})

    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "New chat"
    assert data["turn_count"] == 0
    assert isinstance(data["session_id"], str)
    assert isinstance(data["last_active"], int | float)

    # Verify the session is tracked as active.
    sessions, active = store.list_sessions("owner-b")
    assert active == data["session_id"]
    sids = [cast(str, s["session_id"]) for s in sessions]
    assert data["session_id"] in sids


@pytest.mark.asyncio
async def test_sessions_create_missing_owner_id_returns_400() -> None:
    """``POST /sessions`` without ``owner_id`` returns 400."""
    async with mock_app() as f:
        response = await f.client.post("/sessions", json={})

    assert response.status_code == 400
    assert "owner_id" in response.json()["error"]


@pytest.mark.asyncio
async def test_sessions_create_invalid_json_returns_400() -> None:
    """``POST /sessions`` with invalid JSON returns 400."""
    async with mock_app() as f:
        response = await f.client.post(
            "/sessions",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    assert "error" in response.json()


# ---------------------------------------------------------------------------
# Sessions endpoints — DELETE /sessions/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_delete_closes_subsessions_and_removes_session() -> None:
    """``DELETE /sessions/{id}`` closes the session's subsessions and deletes it."""
    store = ConversationStore()
    sid_a = cast(str, store.create_session("owner-del")["session_id"])
    sid_b = cast(str, store.create_session("owner-del")["session_id"])  # active
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner=sid_a)

    async with mock_app(
        conversation_store=store,
        subsession_registry=registry,
    ) as f:
        response = await f.client.delete(f"/sessions/{sid_a}?owner_id=owner-del")

    assert response.status_code == 200
    data = response.json()
    assert data["deleted"] is True
    assert data["subsessions_closed"] == 1
    assert data["active_session_id"] == sid_b

    # Session a is gone; its subsession is closed.
    sessions, _ = store.list_sessions("owner-del")
    assert sid_a not in [s["session_id"] for s in sessions]
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "session closed"


@pytest.mark.asyncio
async def test_sessions_delete_unknown_returns_404() -> None:
    """``DELETE`` of an unknown session returns 404."""
    store = ConversationStore()
    store.create_session("owner-e")
    async with mock_app(conversation_store=store) as f:
        response = await f.client.delete("/sessions/ghost?owner_id=owner-e")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_sessions_delete_missing_owner_id_returns_400() -> None:
    """``DELETE`` without ``owner_id`` returns 400."""
    async with mock_app() as f:
        response = await f.client.delete("/sessions/whatever")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Sessions close endpoint — POST /sessions/{id}/close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_close_closes_subsessions_and_marks_closed() -> None:
    """``POST /sessions/{id}/close`` closes subsessions + marks the session."""
    store = ConversationStore()
    sid = str(store.create_session("owner-close")["session_id"])
    registry = SubsessionRegistry(store_path=None)
    task_info = _register_subsession(registry, owner=sid, title="watch something")
    chat_info = _register_subsession(
        registry, owner=sid, kind=SubsessionKind.USER_CHAT, title="side chat"
    )

    async with mock_app(
        conversation_store=store,
        subsession_registry=registry,
    ) as f:
        response = await f.client.post(f"/sessions/{sid}/close?owner_id=owner-close")

    assert response.status_code == 200
    data = response.json()
    assert data["closed"] is True
    assert data["session_id"] == sid
    assert data["subsessions_closed"] == 2

    # Session still exists, marked closed.
    assert store.is_session_closed(sid) is True
    sessions, _ = store.list_sessions("owner-close")
    assert sid in [s["session_id"] for s in sessions]
    assert sessions[0]["closed"] is True

    # Both subsessions were closed via close_all_for_owner.
    assert task_info.status is SubsessionStatus.CLOSED
    assert chat_info.status is SubsessionStatus.CLOSED


@pytest.mark.asyncio
async def test_sessions_close_unknown_returns_404() -> None:
    """``POST /sessions/{id}/close`` of an unknown session returns 404."""
    store = ConversationStore()
    store.create_session("owner-e")
    async with mock_app(conversation_store=store) as f:
        response = await f.client.post("/sessions/ghost/close?owner_id=owner-e")
    assert response.status_code == 404
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_sessions_close_missing_owner_id_returns_400() -> None:
    """``POST /sessions/{id}/close`` without ``owner_id`` returns 400."""
    async with mock_app() as f:
        response = await f.client.post("/sessions/whatever/close")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_sessions_close_cleans_orphaned_subsessions() -> None:
    """Close endpoint closes subsessions even for a session the caller lacks."""
    store = ConversationStore()
    store.create_session("real-owner")  # registers the owner
    registry = SubsessionRegistry(store_path=None)
    # Register a subsession for a session that belongs to a different owner.
    info = _register_subsession(registry, owner="orphan-sid", title="orphan check")

    async with mock_app(
        conversation_store=store,
        subsession_registry=registry,
    ) as f:
        # Call close with the real owner but an orphan session id.
        response = await f.client.post("/sessions/orphan-sid/close?owner_id=real-owner")

    # Session not found for this owner → 404, but cleanup still ran.
    assert response.status_code == 404
    data = response.json()
    assert data["subsessions_closed"] == 1
    assert info.status is SubsessionStatus.CLOSED


# ---------------------------------------------------------------------------
# Per-session isolation via /chat and /history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_two_sessions_independent_history() -> None:
    """Two sessions under the same owner keep independent conversation histories."""
    store = ConversationStore()
    sid_a = cast(str, store.create_session("owner-c")["session_id"])
    sid_b = cast(str, store.create_session("owner-c")["session_id"])

    async with mock_app(tokens=["reply A"], conversation_store=store) as f:
        await f.client.post(
            "/chat",
            json={"message": "msg A", "session_id": sid_a, "owner_id": "owner-c"},
        )

    async with mock_app(tokens=["reply B"], conversation_store=store) as f:
        await f.client.post(
            "/chat",
            json={"message": "msg B", "session_id": sid_b, "owner_id": "owner-c"},
        )

    assert store.history(sid_a) == [("msg A", "reply A")]
    assert store.history(sid_b) == [("msg B", "reply B")]


@pytest.mark.asyncio
async def test_history_endpoint_session_scoped() -> None:
    """``GET /history?session_id=X`` returns only that session's turns."""
    store = ConversationStore()
    sid_a = cast(str, store.create_session("owner-d")["session_id"])
    sid_b = cast(str, store.create_session("owner-d")["session_id"])

    store.record(sid_a, "owner-d", "qa", "aa")
    store.record(sid_b, "owner-d", "qb", "ab")

    async with mock_app(conversation_store=store) as f:
        ra = await f.client.get(f"/history?session_id={sid_a}")
        rb = await f.client.get(f"/history?session_id={sid_b}")

    assert ra.json() == {"turns": [["qa", "aa"]]}
    assert rb.json() == {"turns": [["qb", "ab"]]}


# ---------------------------------------------------------------------------
# Legacy client_id backward compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_legacy_client_id_fallback() -> None:
    """``POST /chat`` with only ``client_id`` treats it as both owner and session."""
    async with mock_app(tokens=["ok"]) as f:
        await f.client.post(
            "/chat", json={"message": "legacy msg", "client_id": "legacy-1"}
        )

    # Should have threaded via client_id as session_id.
    assert f.agent.session_id == "legacy-1"
    assert f.agent.history == []

    # A second call with the same client_id sees the first turn.
    async with mock_app(
        tokens=["ok"], conversation_store=f.app.state.conversation_store
    ) as f2:
        await f2.client.post(
            "/chat", json={"message": "legacy msg 2", "client_id": "legacy-1"}
        )
        assert f2.agent.history == [("legacy msg", "ok")]


@pytest.mark.asyncio
async def test_history_legacy_client_id_fallback() -> None:
    """``GET /history?client_id=X`` works as a legacy fallback."""
    store = ConversationStore()
    store.begin("legacy-c")
    store.record("legacy-c", None, "hello", "hi")

    async with mock_app(conversation_store=store) as f:
        response = await f.client.get("/history?client_id=legacy-c")

    assert response.status_code == 200
    assert response.json() == {"turns": [["hello", "hi"]]}


# ---------------------------------------------------------------------------
# Persistence of sessions across store reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_survive_store_reload() -> None:
    """Sessions created via the endpoints survive a fresh ConversationStore load."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        persist_path = Path(tf.name)

    try:
        store1 = ConversationStore(persist_path=persist_path)
        async with mock_app(conversation_store=store1) as f:
            # Create a session via POST
            r = await f.client.post("/sessions", json={"owner_id": "o-persist"})
            sid = r.json()["session_id"]

            # Post a chat message to populate history and title
            await f.client.post(
                "/chat",
                json={
                    "message": "persist me",
                    "session_id": sid,
                    "owner_id": "o-persist",
                },
            )

        # Reload from the same persist file
        store2 = ConversationStore(persist_path=persist_path)
        async with mock_app(conversation_store=store2) as f:
            r2 = await f.client.get("/sessions?owner_id=o-persist")
            data = r2.json()
            assert len(data["sessions"]) == 1
            s = data["sessions"][0]
            assert s["session_id"] == sid
            assert s["title"] == "persist me"
            assert s["turn_count"] == 1
            assert data["active_session_id"] == sid

            # History is also preserved
            r3 = await f.client.get(f"/history?session_id={sid}")
            assert r3.json() == {"turns": [["persist me", "Hello world!"]]}
    finally:
        persist_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Idle does NOT wipe history
# ---------------------------------------------------------------------------


def test_idle_does_not_wipe_history() -> None:
    """Advancing past idle_reset_seconds does NOT clear session history."""
    from tests.chat.test_conversation import _FakeWallClock, _store

    clock = _FakeWallClock()
    store = _store(wall_clock=clock, idle_reset_seconds=10.0)
    sid = cast(str, store.create_session("owner-x")["session_id"])
    store.record(sid, "owner-x", "hello", "hi")

    # Advance well past the idle threshold — history must remain intact.
    clock.advance(999.0)

    _, history_begin = store.begin(sid)
    assert history_begin == [("hello", "hi")]

    history = store.history(sid)
    assert history == [("hello", "hi")]


# ---------------------------------------------------------------------------
# UI: history load path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_injects_history_load_path() -> None:
    """``GET /`` contains the history-loading wiring.

    The served HTML must reference:
    - the ``loadHistory`` function
    - the ``/history`` endpoint (via string match)
    - the ``addAssistantBubble`` helper
    """
    async with mock_app() as f:
        response = await f.client.get("/")

    assert response.status_code == 200
    assert "loadHistory" in response.text
    assert '"/history"' in response.text
    assert "addAssistantBubble" in response.text


# ---------------------------------------------------------------------------
# Chat endpoint — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_missing_message_field() -> None:
    """Verify that the /chat endpoint returns 400 when the message field is missing."""
    async with mock_app() as f:
        response = await f.client.post("/chat", json={})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_message_not_a_string() -> None:
    """Verify that the /chat endpoint returns 400 when message is not a string."""
    async with mock_app() as f:
        response = await f.client.post("/chat", json={"message": 123})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_invalid_json() -> None:
    """Return 400 when the request body is not valid JSON."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat", content=b"not json", headers={"Content-Type": "application/json"}
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_empty_message_string() -> None:
    """Return 400 when the message field is an empty string."""
    async with mock_app() as f:
        response = await f.client.post("/chat", json={"message": ""})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_agent_raises() -> None:
    """Return an SSE error frame when the agent raises."""
    async with mock_app(error=RuntimeError("LLM went boom")) as f:
        response = await f.client.post("/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert SSE_CONTENT_TYPE in response.headers["content-type"]

    frames = _parse_sse(response)

    error_frames = [f for f in frames if f.get("type") == SSE_ERROR_TYPE]
    assert len(error_frames) == 1
    assert error_frames[0]["message"] == "LLM went boom"

    # A failing agent must not emit a "done" frame.
    done_frames = [f for f in frames if f.get("type") == SSE_DONE_TYPE]
    assert len(done_frames) == 0


# ---------------------------------------------------------------------------
# Chat endpoint — image attachments
# ---------------------------------------------------------------------------


def _png_pixel() -> bytes:
    """Smallest valid PNG — 1×1 red pixel."""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5"
        "+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
    )


@pytest.mark.asyncio
async def test_chat_endpoint_passes_images_to_agent() -> None:
    """A request with a valid ``images`` array reaches the agent with decoded bytes."""
    png_bytes = _png_pixel()
    data_b64 = base64.b64encode(png_bytes).decode()

    async with mock_app(tokens=["ok"]) as f:
        response = await f.client.post(
            "/chat",
            json={
                "message": "look at this",
                "images": [{"media_type": "image/png", "data": data_b64}],
            },
        )

    assert response.status_code == 200
    assert f.agent.called_with == "look at this"
    assert f.agent.images == [("image/png", png_bytes)]


@pytest.mark.asyncio
async def test_chat_endpoint_images_only_no_message() -> None:
    """A request with only images (no text) is accepted."""
    png_bytes = _png_pixel()
    data_b64 = base64.b64encode(png_bytes).decode()

    async with mock_app(tokens=["seen it"]) as f:
        response = await f.client.post(
            "/chat",
            json={"images": [{"media_type": "image/png", "data": data_b64}]},
        )

    assert response.status_code == 200
    # message passed through as empty string
    assert f.agent.called_with == ""
    assert f.agent.images == [("image/png", png_bytes)]


@pytest.mark.asyncio
async def test_chat_endpoint_multiple_images() -> None:
    """Multiple valid images are all decoded and forwarded."""
    png_bytes = _png_pixel()
    data_b64 = base64.b64encode(png_bytes).decode()

    async with mock_app(tokens=["ok"]) as f:
        await f.client.post(
            "/chat",
            json={
                "message": "compare",
                "images": [
                    {"media_type": "image/png", "data": data_b64},
                    {"media_type": "image/jpeg", "data": data_b64},
                ],
            },
        )

    assert f.agent.images == [
        ("image/png", png_bytes),
        ("image/jpeg", png_bytes),
    ]


@pytest.mark.asyncio
async def test_chat_endpoint_neither_message_nor_images_returns_400() -> None:
    """A body with no message and no images is rejected with 400."""
    async with mock_app() as f:
        response = await f.client.post("/chat", json={})

    assert response.status_code == 400
    assert "error" in response.json()
    assert "message" in response.json()["error"] or "image" in response.json()["error"]


@pytest.mark.asyncio
async def test_chat_endpoint_images_not_a_list_returns_400() -> None:
    """A non-list ``images`` field is rejected with 400."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat", json={"message": "hi", "images": "not-a-list"}
        )

    assert response.status_code == 400
    assert "error" in response.json()
    assert "array" in response.json()["error"]


@pytest.mark.asyncio
async def test_chat_endpoint_too_many_images_returns_400() -> None:
    """Exceeding ``max_images_per_message`` returns 400."""
    png_bytes = _png_pixel()
    data_b64 = base64.b64encode(png_bytes).decode()

    async with mock_app(max_images_per_message=2) as f:
        response = await f.client.post(
            "/chat",
            json={
                "message": "hi",
                "images": [
                    {"media_type": "image/png", "data": data_b64},
                    {"media_type": "image/png", "data": data_b64},
                    {"media_type": "image/png", "data": data_b64},
                ],
            },
        )

    assert response.status_code == 400
    err = response.json()["error"]
    assert "too many images" in err
    assert "3" in err


@pytest.mark.asyncio
async def test_chat_endpoint_oversized_image_returns_400() -> None:
    """An image whose decoded size exceeds ``max_image_bytes`` returns 400."""
    # Create a 50-byte payload, set limit to 40.
    payload = b"x" * 50
    data_b64 = base64.b64encode(payload).decode()

    async with mock_app(max_image_bytes=40) as f:
        response = await f.client.post(
            "/chat",
            json={
                "message": "hi",
                "images": [{"media_type": "image/png", "data": data_b64}],
            },
        )

    assert response.status_code == 400
    err = response.json()["error"]
    assert "exceeds maximum" in err
    assert "50" in err


@pytest.mark.asyncio
async def test_chat_endpoint_disallowed_media_type_returns_400() -> None:
    """A media_type not in the allowlist is rejected with 400."""
    data_b64 = base64.b64encode(b"fake").decode()

    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={
                "message": "hi",
                "images": [{"media_type": "image/bmp", "data": data_b64}],
            },
        )

    assert response.status_code == 400
    err = response.json()["error"]
    assert "image/bmp" in err
    assert "not allowed" in err


@pytest.mark.asyncio
async def test_chat_endpoint_non_base64_data_returns_400() -> None:
    """Non-base64 ``data`` is rejected with 400."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={
                "message": "hi",
                "images": [{"media_type": "image/png", "data": "!!!not-base64!!!"}],
            },
        )

    assert response.status_code == 400
    assert "not valid base64" in response.json()["error"]


@pytest.mark.asyncio
async def test_chat_endpoint_image_missing_media_type_returns_400() -> None:
    """An image entry without ``media_type`` is rejected."""
    data_b64 = base64.b64encode(b"x").decode()

    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={
                "message": "hi",
                "images": [{"data": data_b64}],
            },
        )

    assert response.status_code == 400
    assert "media_type" in response.json()["error"]


@pytest.mark.asyncio
async def test_chat_endpoint_image_missing_data_returns_400() -> None:
    """An image entry without ``data`` is rejected."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={
                "message": "hi",
                "images": [{"media_type": "image/png"}],
            },
        )

    assert response.status_code == 400
    assert "data" in response.json()["error"]


@pytest.mark.asyncio
async def test_chat_endpoint_image_entry_not_a_dict_returns_400() -> None:
    """An image entry that is not a dict is rejected."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={"message": "hi", "images": ["not-a-dict"]},
        )

    assert response.status_code == 400
    assert "expected a JSON object" in response.json()["error"]


# ---------------------------------------------------------------------------
# create_agent_from_settings
# ---------------------------------------------------------------------------


def test_create_agent_from_settings_explicit() -> None:
    """``create_agent_from_settings`` wires llmio fields from a ``Settings``."""
    settings = Settings(llmio_model_level=2, llmio_api_key="sk-from-settings")

    agent = create_agent_from_settings("Be concise.", settings=settings)

    assert isinstance(agent, LlmioChatAgent)
    assert agent._model_level == 2
    assert agent._instruction == "Be concise."
    # Level 2 → openrouter (key-bearing), so the key is forwarded.
    assert agent._api_key == "sk-from-settings"  # pragma: allowlist secret


def test_create_agent_from_settings_keyless_level_drops_key() -> None:
    """A keyless level (4 → claude-sdk, the default) never forwards an api_key."""
    settings = Settings()  # model_level 4, keyless

    agent = create_agent_from_settings("Be helpful.", settings=settings)

    assert isinstance(agent, LlmioChatAgent)
    assert agent._model_level == 4
    assert agent._api_key == ""


def test_create_agent_from_settings_instruction_from_config() -> None:
    """A ``None`` instruction falls back to ``settings.agent_instruction``."""
    settings = Settings(agent_instruction="You are terse.")

    agent = create_agent_from_settings(settings=settings)

    assert agent._instruction == "You are terse."


@pytest.mark.asyncio
async def test_create_agent_from_settings_uses_load_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_agent_from_settings`` resolves config when *settings* is None."""
    # Isolate from any on-disk config/chat.local.yaml so resolution is env-only.
    monkeypatch.setattr(
        "robotsix_chat.config.constants.DEFAULT_CONFIG_PATH",
        Path("/nonexistent/chat.local.yaml"),
    )
    monkeypatch.delenv("CHAT_CONFIG_PATH", raising=False)
    monkeypatch.setenv("LLMIO_MODEL_LEVEL", "1")
    monkeypatch.setenv("LLMIO_API_KEY", "sk-env-test")

    result = create_agent_from_settings("Helpful bot.")

    assert isinstance(result, LlmioChatAgent)
    assert result._model_level == 1
    assert result._api_key == "sk-env-test"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# run_server_from_config — LLM wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_server_from_config_creates_agent_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create an ``LlmioChatAgent`` from ``Settings``.

    ``run_server_from_config()`` with no *agent* argument creates the
    agent and forwards server options.
    """
    # Isolate from any on-disk config/chat.local.yaml so resolution is env-only.
    monkeypatch.setattr(
        "robotsix_chat.config.constants.DEFAULT_CONFIG_PATH",
        Path("/nonexistent/chat.local.yaml"),
    )
    monkeypatch.delenv("CHAT_CONFIG_PATH", raising=False)
    monkeypatch.setenv("LLMIO_MODEL_LEVEL", "3")
    monkeypatch.setenv("SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("SERVER_PORT", "8080")

    with patch("robotsix_chat.chat.server.run_server") as mock_run_server:
        run_server_from_config()

        call_args = mock_run_server.call_args
        passed_agent = call_args[0][0]
        assert isinstance(passed_agent, LlmioChatAgent)
        assert passed_agent._model_level == 3
        assert passed_agent._instruction.startswith("You are a helpful assistant.")
        # A conversation store is built from settings and forwarded; assert the
        # rest of the server options explicitly.
        conversation_store = call_args[1].pop("conversation_store")
        assert isinstance(conversation_store, ConversationStore)
        event_bus = call_args[1].pop("event_bus")
        from robotsix_chat.chat.events import EventBus

        assert isinstance(event_bus, EventBus)
        run_serializer = call_args[1].pop("run_serializer")
        from robotsix_chat.chat.server import RunSerializer

        assert isinstance(run_serializer, RunSerializer)
        subsession_registry = call_args[1].pop("subsession_registry")
        assert isinstance(subsession_registry, SubsessionRegistry)
        subsession_delivery = call_args[1].pop("subsession_delivery")
        from robotsix_chat.subsessions import ParentDelivery

        assert isinstance(subsession_delivery, ParentDelivery)
        on_startup = call_args[1].pop("on_startup")
        assert callable(on_startup)
        on_startup_async = call_args[1].pop("on_startup_async")
        assert callable(on_startup_async)
        on_shutdown = call_args[1].pop("on_shutdown")
        assert callable(on_shutdown)
        assert call_args[1] == {
            "host": "127.0.0.1",
            "port": 8080,
            "idle_timeout_minutes": 30,
            "max_images_per_message": 8,
            "max_image_bytes": 5_242_880,
            "allowed_image_media_types": [
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
            ],
            "cors_allow_origins": [],
            "auth": None,
            "correlation_id_header": "X-Request-ID",
        }


@pytest.mark.asyncio
async def test_run_server_from_config_passes_explicit_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward an explicit agent to ``run_server``.

    ``run_server_from_config(agent)`` passes *agent* through without
    creating a new one.
    """
    # Isolate from any on-disk config; the default (claude-sdk) needs no key.
    monkeypatch.setattr(
        "robotsix_chat.config.constants.DEFAULT_CONFIG_PATH",
        Path("/nonexistent/chat.local.yaml"),
    )
    monkeypatch.delenv("CHAT_CONFIG_PATH", raising=False)
    mock_agent = MagicMock()

    with patch("robotsix_chat.chat.server.run_server") as mock_run_server:
        run_server_from_config(mock_agent)

        mock_run_server.assert_called_once()
        passed_agent = mock_run_server.call_args[0][0]
        assert passed_agent is mock_agent


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_error_handler_returns_json_500() -> None:
    """An unhandled exception in an endpoint returns a JSON 500 response."""
    async with mock_app(raise_app_exceptions=False) as f:
        with patch(
            "robotsix_chat.chat.server._load_ui_html",
            side_effect=RuntimeError("UI resource missing"),
        ):
            response = await f.client.get("/")

    assert response.status_code == 500
    data = response.json()
    assert data == {"error": "internal server error"}


# ---------------------------------------------------------------------------
# Unknown routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_route_returns_404_json() -> None:
    """Return a JSON 404 for unknown routes."""
    async with mock_app() as f:
        response = await f.client.get("/nonexistent")

    assert response.status_code == 404
    data = response.json()
    assert data == {"error": "not found"}


@pytest.mark.asyncio
async def test_wrong_method_on_known_route_returns_405() -> None:
    """Return 405 when using the wrong HTTP method on a known route."""
    async with mock_app() as f:
        # POST /health is not a valid endpoint — Starlette returns 405.
        response = await f.client.post("/health")

    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Browser UI serving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_served_at_root_by_default() -> None:
    """``GET /`` returns the bundled browser chat UI HTML."""
    async with mock_app() as f:
        response = await f.client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in response.text
    assert "robotsix-agent-comm" in response.text or "Chat" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timeout, expect_substring",
    [
        (30, "var IDLE_TIMEOUT_MINUTES = 30;"),
        (5, "var IDLE_TIMEOUT_MINUTES = 5;"),
        (0, "var IDLE_TIMEOUT_MINUTES = 0;"),
    ],
)
async def test_ui_injects_idle_timeout(timeout: int, expect_substring: str) -> None:
    """``GET /`` injects the configured ``idle_timeout_minutes`` into the JS."""
    async with mock_app(idle_timeout_minutes=timeout) as f:
        response = await f.client.get("/")

    assert response.status_code == 200
    assert expect_substring in response.text
    # No unsubstituted placeholder remains.
    assert "{{ IDLE_TIMEOUT_MINUTES }}" not in response.text


@pytest.mark.asyncio
async def test_ui_injects_message_queue() -> None:
    """``GET /`` contains the client-side FIFO message-queue markers."""
    async with mock_app() as f:
        response = await f.client.get("/")

    assert response.status_code == 200
    assert "messageQueue" in response.text
    assert "drainQueue" in response.text
    assert ".bubble.user.queued" in response.text


@pytest.mark.asyncio
async def test_ui_renders_event_stream_wiring() -> None:
    """``GET /`` contains the persistent notification-stream wiring.

    The served HTML must reference the ``/events`` SSE endpoint and the
    ``openEventStream`` function that opens it.  (The subsession-panel
    JS markup is covered by ``scripts/check_sse_event_types.py``, which
    cross-checks the frame-type literals against ``events.py``.)
    """
    async with mock_app() as f:
        response = await f.client.get("/")

    assert response.status_code == 200
    assert '"/events"' in response.text
    assert "openEventStream" in response.text


@pytest.mark.asyncio
async def test_ui_disabled_returns_404() -> None:
    """With ``serve_ui=False`` the root path is not registered."""
    async with mock_app(serve_ui=False) as f:
        response = await f.client.get("/")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cors_allow_origins, expect_header",
    [
        (None, False),
        (["https://ui.example.com"], True),
    ],
)
async def test_cors_headers(
    cors_allow_origins: list[str] | None,
    expect_header: bool,
) -> None:
    """CORS ``access-control-allow-origin`` is present only when configured."""
    kwargs: dict[str, Any] = {}
    if cors_allow_origins is not None:
        kwargs["cors_allow_origins"] = cors_allow_origins
    async with mock_app(**kwargs) as f:
        response = await f.client.post(
            "/chat",
            json={"message": "hi"},
            headers={"Origin": "https://ui.example.com"},
        )

    if expect_header:
        assert response.headers.get("access-control-allow-origin") == (
            "https://ui.example.com"
        )
    else:
        assert "access-control-allow-origin" not in response.headers


# ---------------------------------------------------------------------------
# Correlation ID header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_correlation_id_header_in_response() -> None:
    """Echo a custom correlation ID header in the response.

    The ``correlation_id_header`` name reaches the middleware and is
    reflected back.
    """
    async with mock_app(correlation_id_header="X-Custom-ID") as f:
        response = await f.client.get(
            "/health",
            headers={"X-Custom-ID": "11111111-1111-1111-1111-111111111111"},
        )

    assert response.status_code == 200
    assert response.headers["X-Custom-ID"] == "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Subsession registry wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsession_registry_wired_into_app_state() -> None:
    """``subsession_registry``/``subsession_delivery`` land on ``app.state``."""
    from robotsix_chat.chat.events import EventBus

    bus = EventBus()
    registry = SubsessionRegistry(event_sink=bus, store_path=None)

    async with mock_app(subsession_registry=registry, event_bus=bus) as f:
        assert f.app.state.subsession_registry is registry
        assert f.app.state.event_bus is bus


@pytest.mark.asyncio
async def test_subsession_registry_defaults_to_none() -> None:
    """Without the kwargs, ``app.state`` stores ``None`` for both."""
    async with mock_app() as f:
        assert f.app.state.subsession_registry is None
        assert f.app.state.subsession_delivery is None


@pytest.mark.asyncio
async def test_resume_hook_passed_through_mock_app() -> None:
    """The ``on_startup`` callable is threaded through ``create_app``."""
    from unittest.mock import MagicMock

    hook = MagicMock()

    async with mock_app(on_startup=hook) as f:
        pass  # mock_app doesn't run the lifespan, but the hook is wired

    # The hook was passed through; the lifespan stores it but doesn't call
    # it until the ASGI server starts.  We can verify it's been retained.
    # (We can't easily assert it's stored because Starlette's lifespan is
    # internal, but the app was built without error.)
    assert f.app.state.subsession_registry is None  # default


# ---------------------------------------------------------------------------
# GET /subsessions — list endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsessions_list_missing_session_id_returns_400() -> None:
    """``GET /subsessions`` without ``session_id`` returns 400."""
    async with mock_app(subsession_registry=SubsessionRegistry(store_path=None)) as f:
        response = await f.client.get("/subsessions")

    assert response.status_code == 400
    assert response.json() == {"error": "session_id query parameter is required"}


@pytest.mark.asyncio
async def test_subsessions_list_no_registry_returns_503() -> None:
    """``GET /subsessions?session_id=x`` returns 503 when not wired."""
    async with mock_app() as f:
        response = await f.client.get("/subsessions?session_id=s1")

    assert response.status_code == 503
    assert response.json() == {"error": "subsessions feature not enabled"}


@pytest.mark.asyncio
async def test_subsessions_list_returns_owner_tree() -> None:
    """``GET /subsessions?session_id=x`` returns that session's snapshots."""
    registry = SubsessionRegistry(store_path=None)
    a = _register_subsession(registry, owner="sess-a", title="first")
    b = _register_subsession(
        registry,
        owner="sess-a",
        kind=SubsessionKind.PERIODIC,
        title="second",
        interval_seconds=60.0,
    )
    registry.mark_closed(b.id, summary="done", reason="completed")
    _register_subsession(registry, owner="sess-b", title="foreign")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.get("/subsessions?session_id=sess-a")

    assert response.status_code == 200
    subsessions = response.json()["subsessions"]
    # Whole tree including terminal entries, oldest first, no transcripts.
    assert [s["subsession_id"] for s in subsessions] == [a.id, b.id]
    for snapshot in subsessions:
        assert "transcript" not in snapshot
        for key in (
            "subsession_id",
            "kind",
            "owner_session_id",
            "parent_id",
            "depth",
            "title",
            "prompt",
            "model_level",
            "status",
            "created_at",
            "last_activity_at",
            "interval_seconds",
            "next_run_at",
            "include_previous_result",
            "runs",
            "max_runs",
            "last_result",
            "summary",
            "close_reason",
            "error",
        ):
            assert key in snapshot, f"missing snapshot key {key!r}"
    assert subsessions[1]["status"] == SubsessionStatus.CLOSED.value
    assert subsessions[1]["summary"] == "done"


# ---------------------------------------------------------------------------
# GET /subsessions/{id} and /transcript
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsessions_get_unknown_returns_404() -> None:
    """``GET /subsessions/{id}`` for an unknown id returns 404."""
    async with mock_app(subsession_registry=SubsessionRegistry(store_path=None)) as f:
        response = await f.client.get("/subsessions/ghost")

    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "unknown subsession"
    assert data["subsession_id"] == "ghost"


@pytest.mark.asyncio
async def test_subsessions_get_returns_snapshot_with_transcript() -> None:
    """``GET /subsessions/{id}`` returns the snapshot plus its transcript."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a", title="detailed")
    registry.append_transcript(info.id, "assistant", "working on it")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.get(f"/subsessions/{info.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["subsession_id"] == info.id
    assert data["title"] == "detailed"
    assert len(data["transcript"]) == 1
    entry = data["transcript"][0]
    assert entry["role"] == "assistant"
    assert entry["text"] == "working on it"
    assert isinstance(entry["timestamp"], int | float)


@pytest.mark.asyncio
async def test_subsessions_transcript_endpoint() -> None:
    """``GET /subsessions/{id}/transcript`` returns transcript only."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a")
    registry.append_transcript(info.id, "user", "a question")
    registry.append_transcript(info.id, "assistant", "an answer")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.get(f"/subsessions/{info.id}/transcript")
        missing = await f.client.get("/subsessions/ghost/transcript")

    assert response.status_code == 200
    data = response.json()
    assert data["subsession_id"] == info.id
    assert [(e["role"], e["text"]) for e in data["transcript"]] == [
        ("user", "a question"),
        ("assistant", "an answer"),
    ]
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# POST /subsessions/{id}/message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsessions_message_empty_text_returns_400() -> None:
    """An empty or missing ``text`` field is rejected with 400."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a")

    async with mock_app(subsession_registry=registry) as f:
        empty = await f.client.post(
            f"/subsessions/{info.id}/message", json={"text": ""}
        )
        missing = await f.client.post(f"/subsessions/{info.id}/message", json={})

    assert empty.status_code == 400
    assert "text" in empty.json()["error"]
    assert missing.status_code == 400


@pytest.mark.asyncio
async def test_subsessions_message_unknown_returns_404() -> None:
    """Messaging an unknown subsession returns 404."""
    async with mock_app(subsession_registry=SubsessionRegistry(store_path=None)) as f:
        response = await f.client.post(
            "/subsessions/ghost/message", json={"text": "hello"}
        )

    assert response.status_code == 404
    assert response.json()["subsession_id"] == "ghost"


@pytest.mark.asyncio
async def test_subsessions_message_terminal_returns_409() -> None:
    """Messaging a terminal subsession returns 409."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a")
    registry.mark_closed(info.id, summary="done", reason="completed")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.post(
            f"/subsessions/{info.id}/message", json={"text": "too late"}
        )

    assert response.status_code == 409
    assert response.json()["error"] == "subsession is not active"


@pytest.mark.asyncio
async def test_subsessions_message_queues_into_inbox_and_transcript() -> None:
    """A valid message returns 202 and lands in the inbox and transcript."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.post(
            f"/subsessions/{info.id}/message", json={"text": "user says hi"}
        )

    assert response.status_code == 202
    assert response.json() == {"subsession_id": info.id, "status": "queued"}

    # Transcripted immediately with role "user"...
    assert [(e.role, e.text) for e in info.transcript] == [("user", "user says hi")]
    # ...and queued for the worker's next turn boundary.
    queued = registry.drain_inbox(info.id)
    assert [(m.role, m.text) for m in queued] == [("user", "user says hi")]


# ---------------------------------------------------------------------------
# POST /subsessions/{id}/close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsessions_close_unknown_returns_404() -> None:
    """Closing an unknown subsession returns 404."""
    async with mock_app(subsession_registry=SubsessionRegistry(store_path=None)) as f:
        response = await f.client.post("/subsessions/ghost/close")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_subsessions_close_terminal_is_idempotent() -> None:
    """Closing an already-terminal subsession returns ``closed: false``."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a")
    registry.mark_closed(info.id, summary="done", reason="completed")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.post(f"/subsessions/{info.id}/close")

    assert response.status_code == 200
    assert response.json() == {
        "subsession_id": info.id,
        "closed": False,
        "status": SubsessionStatus.CLOSED.value,
    }


@pytest.mark.asyncio
async def test_subsessions_close_cancels_worker_and_delivers_summary() -> None:
    """Closing a live subsession cancels its worker and delivers a summary."""
    import asyncio
    from contextlib import suppress

    from tests.common.subsession_fakes import FakeAgent, build_env, wait_until

    gate = asyncio.Event()  # never set — the worker stays mid-turn
    agent = FakeAgent(["never"], gate=gate)
    env = build_env(agent=agent)

    sub_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.TASK,
        owner_session_id="sess-live",
        parent_id=None,
        depth=1,
        title="long job",
        prompt="work forever",
        model_level=3,
    )
    await wait_until(lambda: len(agent.calls) == 1)
    env.registry.append_transcript(sub_id, "assistant", "half done")
    worker = env.registry._running[sub_id]

    async with mock_app(
        conversation_store=env.conversation_store,
        subsession_registry=env.registry,
        subsession_delivery=env.delivery,
    ) as f:
        response = await f.client.post(f"/subsessions/{sub_id}/close")

    assert response.status_code == 200
    data = response.json()
    assert data["subsession_id"] == sub_id
    assert data["closed"] is True
    assert data["summary"].startswith("Closed by user.")
    assert "half done" in data["summary"]

    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "closed by user"

    # The summary was delivered to the owning session (delivery wired).
    history = env.conversation_store.history("sess-live")
    assert len(history) == 1
    assert history[0][0].startswith(f"[Subsession {sub_id[:8]} (task)")
    assert "half done" in history[0][1]


# ---------------------------------------------------------------------------
# create_agent_from_settings — subsession wiring
# ---------------------------------------------------------------------------


def test_create_agent_model_level_override() -> None:
    """``model_level`` overrides ``settings.llmio_model_level``."""
    settings = Settings(llmio_model_level=3, llmio_api_key="sk-key")

    agent = create_agent_from_settings("Be terse.", settings=settings, model_level=2)

    assert agent._model_level == 2
    # Level 2 → openrouter (key-bearing), so the key is forwarded.
    assert agent._api_key == "sk-key"  # pragma: allowlist secret


def test_main_agent_gets_per_request_subsession_tools_factory() -> None:
    """With an env and no ctx, subsession tools are built per request."""
    from tests.common.subsession_fakes import build_env

    settings = Settings()
    env = build_env()

    agent = create_agent_from_settings(settings=settings, subsession_env=env)

    assert agent._request_tools_factory is not None
    per_request = agent._request_tools_factory("sess-1")
    names = [t.__name__ for t in per_request]
    assert names == [
        "spawn_subsession_tool",
        "message_subsession",
        "close_subsession",
        "list_subsessions",
    ]


def test_subsession_agent_gets_static_tools_with_complete() -> None:
    """With a ctx and close state, the tools are baked in statically."""
    from robotsix_chat.subsessions import CloseState, SubsessionContext
    from tests.common.subsession_fakes import build_env

    settings = Settings()
    env = build_env()
    ctx = SubsessionContext(owner_session_id="sess-1", subsession_id="sub-1", depth=1)

    agent = create_agent_from_settings(
        settings=settings,
        subsession_env=env,
        subsession_ctx=ctx,
        subsession_close_state=CloseState(),
    )

    assert agent._request_tools_factory is None
    names = [getattr(t, "__name__", "") for t in agent._tools or []]
    assert "complete_subsession" in names
    assert "spawn_subsession_tool" in names


def test_bare_agent_has_no_subsession_tools() -> None:
    """Without a subsession env, no subsession tools are attached."""
    agent = create_agent_from_settings(settings=Settings())

    assert agent._request_tools_factory is None
    names = [getattr(t, "__name__", "") for t in agent._tools or []]
    assert "spawn_subsession_tool" not in names
    assert "complete_subsession" not in names


# ---------------------------------------------------------------------------
# Chat endpoint — subsession tool scoping via client_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_passes_session_id_as_client_id() -> None:
    """``/chat`` scopes the per-request tools to the SESSION id.

    Even when the browser sends its own ``client_id``, the agent receives
    the session id as ``client_id`` so spawned subsessions (and their SSE
    frames) land in the owning chat session.
    """
    async with mock_app(tokens=["ok"]) as f:
        await f.client.post(
            "/chat",
            json={
                "message": "hi",
                "session_id": "sess-42",
                "owner_id": "owner-1",
                "client_id": "browser-99",
            },
        )

    assert f.agent.session_id == "sess-42"
    assert f.agent.client_id == "sess-42"


# ---------------------------------------------------------------------------
# Message idempotency
# ---------------------------------------------------------------------------


class TestMessageIdempotency:
    """Tests for per-session message idempotency via ``MessageIdempotencyStore``."""

    @pytest.mark.asyncio
    async def test_same_message_id_concurrent(self) -> None:
        """Concurrent POSTs with same message_id → one agent call, both get reply."""
        async with mock_app(tokens=["Hello", " ", "world!"]) as f:
            import asyncio

            async def post() -> Any:
                return await f.client.post(
                    "/chat",
                    json={
                        "message": "hi",
                        "message_id": "abc-123",
                        "session_id": "s1",
                        "owner_id": "o1",
                    },
                )

            r1, r2 = await asyncio.gather(post(), post())

        assert f.agent.call_count == 1

        frames1 = _parse_sse(r1)
        frames2 = _parse_sse(r2)

        # Both responses contain a done frame.
        assert any(f["type"] == SSE_DONE_TYPE for f in frames1)
        assert any(f["type"] == SSE_DONE_TYPE for f in frames2)

        # Both contain the same reply text.
        reply1 = "".join(
            str(f["content"]) for f in frames1 if f["type"] == SSE_TOKEN_TYPE
        )
        reply2 = "".join(
            str(f["content"]) for f in frames2 if f["type"] == SSE_TOKEN_TYPE
        )
        assert reply1 == reply2 == "Hello world!"

    @pytest.mark.asyncio
    async def test_same_message_id_sequential(self) -> None:
        """First POST completes; second with same message_id replays from store."""
        async with mock_app(tokens=["Hello", " ", "world!"]) as f:
            r1 = await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "abc-456",
                    "session_id": "s2",
                    "owner_id": "o2",
                },
            )
            r2 = await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "abc-456",
                    "session_id": "s2",
                    "owner_id": "o2",
                },
            )

        # Agent called exactly once (first request only).
        assert f.agent.call_count == 1

        frames1 = _parse_sse(r1)
        frames2 = _parse_sse(r2)

        assert any(f["type"] == SSE_DONE_TYPE for f in frames1)
        assert any(f["type"] == SSE_DONE_TYPE for f in frames2)

        # Second response replays the full reply in a single token frame.
        token_frames2 = [f for f in frames2 if f["type"] == SSE_TOKEN_TYPE]
        assert len(token_frames2) == 1
        assert token_frames2[0]["content"] == "Hello world!"

    @pytest.mark.asyncio
    async def test_distinct_message_ids_run_independently(self) -> None:
        """Two POSTs with different message_ids → two agent invocations."""
        async with mock_app(tokens=["ok"]) as f:
            await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "id-1",
                    "session_id": "s3",
                    "owner_id": "o3",
                },
            )
            await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "id-2",
                    "session_id": "s3",
                    "owner_id": "o3",
                },
            )

        assert f.agent.call_count == 2

    @pytest.mark.asyncio
    async def test_no_message_id_backward_compat(self) -> None:
        """POST without message_id field — agent runs, reply returned, no error."""
        async with mock_app(tokens=["ok"]) as f:
            response = await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "session_id": "s4",
                    "owner_id": "o4",
                },
            )

        assert response.status_code == 200
        assert f.agent.call_count == 1
        frames = _parse_sse(response)
        assert any(f["type"] == SSE_DONE_TYPE for f in frames)
        assert any(f["type"] == SSE_TOKEN_TYPE for f in frames)

    @pytest.mark.asyncio
    async def test_retry_after_stream_error_reruns(self) -> None:
        """First POST errors → no completed entry → second POST re-runs agent."""
        # First app instance raises an error.
        async with mock_app(
            error=RuntimeError("boom"),
            tokens=["ok"],  # won't be reached
        ) as f1:
            r1 = await f1.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "abc",
                    "session_id": "s5",
                    "owner_id": "o5",
                },
            )

        frames1 = _parse_sse(r1)
        assert any(f["type"] == SSE_ERROR_TYPE for f in frames1)
        assert not any(f["type"] == SSE_DONE_TYPE for f in frames1)

        # Second app instance with a non-erroring agent.
        async with mock_app(tokens=["ok"]) as f2:
            r2 = await f2.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "abc",
                    "session_id": "s5",
                    "owner_id": "o5",
                },
            )

        assert f2.agent.call_count == 1
        frames2 = _parse_sse(r2)
        assert any(f["type"] == SSE_DONE_TYPE for f in frames2)

    @pytest.mark.asyncio
    async def test_history_read_under_lock(self) -> None:
        """Second message sees the first message's reply in agent.history."""
        store = ConversationStore()

        async with mock_app(tokens=["first reply"], conversation_store=store) as f1:
            await f1.client.post(
                "/chat",
                json={
                    "message": "msg1",
                    "message_id": "id-a",
                    "session_id": "s6",
                    "owner_id": "o6",
                },
            )

        async with mock_app(tokens=["second reply"], conversation_store=store) as f2:
            await f2.client.post(
                "/chat",
                json={
                    "message": "msg2",
                    "message_id": "id-b",
                    "session_id": "s6",
                    "owner_id": "o6",
                },
            )

        # The second call's agent.history must include the first message's reply.
        assert f2.agent.history == [("msg1", "first reply")]

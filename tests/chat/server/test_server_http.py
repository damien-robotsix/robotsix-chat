"""HTTP endpoint contract tests for the chat SSE server.

Covers health/OpenAPI probes, the /chat SSE stream, history, sessions, error
handling, the browser UI serving, CORS, security headers and the correlation
ID header. Split out of ``test_server.py`` (module-size).
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.server import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_TOKEN_TYPE,
)
from robotsix_chat.chat.server.routes.errors import STREAM_ERROR_SERVER
from robotsix_chat.subsessions import (
    SubsessionKind,
    SubsessionRegistry,
    SubsessionStatus,
)
from tests.chat.server.test_server import _parse_sse, _register_subsession
from tests.conftest import mock_app

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
# OpenAPI schema & versioned API prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openapi_json_endpoint() -> None:
    """Serve an OpenAPI document covering every route, root and /api/v1.

    The schema is derived from the same route table that registers the
    handlers, so every endpoint is contract-discoverable.
    """
    async with mock_app() as f:
        response = await f.client.get("/openapi.json")

    assert response.status_code == 200
    data = response.json()
    assert data["openapi"] == "3.0.2"
    paths = data["paths"]
    # Root paths and their versioned aliases are both documented.
    assert "/chat" in paths
    assert "/api/v1/chat" in paths
    assert "/sessions/{session_id}" in paths
    assert "/api/v1/sessions/{session_id}" in paths
    # Path converters are normalized to plain placeholders.
    assert "/config/versions/{version}" in paths
    assert "/api/v1/config/versions/{version}" in paths
    # Ops probes are intentionally root-only.
    assert "/api/v1/health" not in paths
    # Operation objects carry a summary.
    assert paths["/api/v1/chat"]["post"]["summary"]


@pytest.mark.asyncio
async def test_docs_swaggerui_endpoint() -> None:
    """Serve a SwaggerUI HTML page wired to /openapi.json."""
    async with mock_app() as f:
        response = await f.client.get("/docs")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert '"/openapi.json"' in response.text
    assert "swagger-ui" in response.text


@pytest.mark.asyncio
async def test_versioned_api_prefix_route() -> None:
    """The stable /api/v1 prefix serves the same handlers as the root paths."""
    async with mock_app() as f:
        response = await f.client.get("/api/v1/models")

    assert response.status_code == 200
    data = response.json()
    assert "models" in data


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
    done = frames[-1]
    assert done["type"] == SSE_DONE_TYPE
    # The done frame reports the session the turn landed in, so the client
    # can adopt a continuation session after idle-timeout compaction.
    assert isinstance(done["session_id"], str) and done["session_id"]


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
    assert any(f["type"] == SSE_DONE_TYPE for f in frames)


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

    tail = response.text.rstrip("\r\n").rsplit("data: ", 1)[-1]
    done = json.loads(tail)
    assert done["type"] == SSE_DONE_TYPE
    assert isinstance(done["session_id"], str) and done["session_id"]
    assert isinstance(done["timestamp"], int | float)


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
# Sessions endpoints — missing owner_id validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload", "expected_substring"),
    [
        pytest.param("get", "/sessions", None, "owner_id", id="list"),
        pytest.param("post", "/sessions", {"json": {}}, "owner_id", id="create"),
        pytest.param("delete", "/sessions/whatever", None, "owner_id", id="delete"),
        pytest.param("post", "/sessions/whatever/close", None, "owner_id", id="close"),
    ],
)
async def test_sessions_missing_owner_id_returns_400(
    method: str,
    path: str,
    payload: dict[str, object] | None,
    expected_substring: str,
) -> None:
    """Sessions endpoints return 400 when ``owner_id`` is missing."""
    async with mock_app() as f:
        client_method = getattr(f.client, method)
        if payload is not None:
            response = await client_method(path, **payload)
        else:
            response = await client_method(path)

    assert response.status_code == 400
    assert expected_substring in response.json()["error"]


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
    """Advancing time does NOT clear session history."""
    from tests.chat.test_conversation import _FakeWallClock, _store

    clock = _FakeWallClock()
    store = _store(wall_clock=clock)
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
    """``GET /`` returns HTML that references the external CSS, JS, and meta tags.

    The served HTML must:
    - link ``/static/chat.css`` via a ``<link>`` tag
    - load ``/static/chat.js`` via a ``<script src>`` tag
    - carry an ``idle-timeout-minutes`` ``<meta>`` tag
    - carry a ``project-title`` ``<meta>`` tag for JS bootstrapping
    """
    async with mock_app() as f:
        response = await f.client.get("/")

    assert response.status_code == 200
    assert 'href="/static/chat.css"' in response.text
    assert 'src="/static/chat.js"' in response.text
    assert "idle-timeout-minutes" in response.text
    assert "project-title" in response.text


# ---------------------------------------------------------------------------
# UI: static file serving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_static_files_served() -> None:
    """``GET /static/chat.css`` returns the CSS with ``text/css`` content-type."""
    async with mock_app() as f:
        response = await f.client.get("/static/chat.css")

    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    assert ".bubble {" in response.text
    assert "color-scheme: dark" in response.text


@pytest.mark.asyncio
async def test_ui_static_js_served() -> None:
    """``GET /static/chat.js`` returns the JS with ``text/javascript`` content-type."""
    async with mock_app() as f:
        response = await f.client.get("/static/chat.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"].lower()
    assert '"use strict"' in response.text
    assert "function submitMessage" in response.text


# ---------------------------------------------------------------------------
# Chat endpoint — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"json": {}}, id="missing"),
        pytest.param({"json": {"message": 123}}, id="wrong_type"),
        pytest.param(
            {"content": b"not json", "headers": {"Content-Type": "application/json"}},
            id="invalid_json",
        ),
        pytest.param({"json": {"message": ""}}, id="empty_string"),
    ],
)
async def test_chat_endpoint_rejects_invalid_payload(
    payload: dict[str, object],
) -> None:
    """The /chat endpoint returns 400 for invalid payloads."""
    async with mock_app() as f:
        response = await f.client.post("/chat", **payload)

    assert response.status_code == 400
    assert "error" in response.json()


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
    # The raw exception text must not reach the client — the frame carries a
    # curated message plus a stable code and a correlation id instead.
    assert "LLM went boom" not in error_frames[0]["message"]
    assert error_frames[0]["code"] == STREAM_ERROR_SERVER
    assert "correlation_id" in error_frames[0]

    # A failing agent must not emit a "done" frame.
    done_frames = [f for f in frames if f.get("type") == SSE_DONE_TYPE]
    assert len(done_frames) == 0


@pytest.mark.asyncio
async def test_chat_endpoint_agent_raises_empty_message() -> None:
    """SSE error frame stays curated and non-empty when str(exc) is empty."""
    async with mock_app(error=RuntimeError("")) as f:
        response = await f.client.post("/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert SSE_CONTENT_TYPE in response.headers["content-type"]

    frames = _parse_sse(response)

    error_frames = [f for f in frames if f.get("type") == SSE_ERROR_TYPE]
    assert len(error_frames) == 1
    # The curated message is independent of str(exc), so an empty exception
    # message still yields usable text — and never the exception class name.
    assert error_frames[0]["message"]
    assert "RuntimeError" not in error_frames[0]["message"]
    assert error_frames[0]["code"] == STREAM_ERROR_SERVER

    # A failing agent must not emit a "done" frame.
    done_frames = [f for f in frames if f.get("type") == SSE_DONE_TYPE]
    assert len(done_frames) == 0


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
        (30, 'content="30"'),
        (5, 'content="5"'),
        (0, 'content="0"'),
    ],
)
async def test_ui_injects_idle_timeout(timeout: int, expect_substring: str) -> None:
    """``GET /`` injects the configured ``idle_timeout_minutes`` into a meta tag."""
    async with mock_app(idle_timeout_minutes=timeout) as f:
        response = await f.client.get("/")

    assert response.status_code == 200
    assert expect_substring in response.text
    # No unsubstituted placeholder remains.
    assert "{{ IDLE_TIMEOUT_MINUTES }}" not in response.text


@pytest.mark.asyncio
async def test_ui_injects_message_queue() -> None:
    """``GET /`` references the external chat.js script."""
    async with mock_app() as f:
        response = await f.client.get("/")

    assert response.status_code == 200
    assert 'src="/static/chat.js"' in response.text


@pytest.mark.asyncio
async def test_ui_renders_event_stream_wiring() -> None:
    """``GET /`` references the external chat.css and chat.js files.

    The served HTML must reference the CSS and JS for the UI.
    (The subsession-panel JS markup is covered by
    ``scripts/check_sse_event_types.py``, which cross-checks the
    frame-type literals against ``events.py``.)
    """
    async with mock_app() as f:
        response = await f.client.get("/")

    assert response.status_code == 200
    assert 'href="/static/chat.css"' in response.text
    assert 'src="/static/chat.js"' in response.text


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
# HTTP security headers / SSE response headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/", "/docs", "/does-not-exist"])
async def test_security_headers_on_every_response(path: str) -> None:
    """SecureASGIMiddleware decorates every response, including errors."""
    async with mock_app() as f:
        response = await f.client.get(path)

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "strict-transport-security" in response.headers
    assert "permissions-policy" in response.headers
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src-attr 'none'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src ")[1].split(";")[0]


@pytest.mark.asyncio
async def test_csp_allows_swagger_inline_script_by_hash() -> None:
    """The /docs inline bootstrap script's SHA-256 is in the CSP script-src."""
    import hashlib

    async with mock_app() as f:
        response = await f.client.get("/docs")

    parts = response.text.split("<script>")
    assert len(parts) == 2
    inline = parts[1].split("</script>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_chat_sse_response_headers() -> None:
    """POST /chat streams with the anti-buffering SSE header set."""
    async with mock_app() as f:
        response = await f.client.post("/chat", json={"message": "hi"})

    assert response.headers["content-type"] == SSE_CONTENT_TYPE
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"


def test_sse_response_factory_headers() -> None:
    """sse_response() defines the SSE header set once; extra headers merge."""
    from robotsix_chat.chat.server.routes._shared import sse_response

    async def gen() -> AsyncIterator[bytes]:
        yield b": keepalive\n\n"

    response = sse_response(gen(), headers={"X-Extra": "1"})
    assert response.headers["content-type"] == SSE_CONTENT_TYPE
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-extra"] == "1"


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

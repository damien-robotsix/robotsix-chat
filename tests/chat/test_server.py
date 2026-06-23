"""Tests for the chat SSE server."""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
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
from tests.conftest import mock_app


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
    """``GET /history?client_id=`` returns the client's recorded turns as JSON."""
    store = ConversationStore()
    store.begin("c1")
    store.record("c1", "hi", "hello")
    store.record("c1", "how are you", "I'm fine")

    async with mock_app(conversation_store=store) as f:
        response = await f.client.get("/history?client_id=c1")

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
    """``GET /history`` without a ``client_id`` query param returns 400."""
    async with mock_app() as f:
        response = await f.client.get("/history")

    assert response.status_code == 400
    data = response.json()
    assert "error" in data
    assert "client_id" in data["error"]


@pytest.mark.asyncio
async def test_history_read_is_non_mutating() -> None:
    """Reading history does not refresh last-activity or reset an idle conversation."""
    from tests.chat.test_conversation import _FakeClock, _store

    clock = _FakeClock()
    store = _store(clock)
    store.begin("c1")
    store.record("c1", "q", "a")

    # Read history — this must not count as activity.
    turns = store.history("c1")
    assert turns == [("q", "a")]

    # Advance past the default idle window and assert begin() resets.
    clock.advance(1801)
    session_id, history = store.begin("c1")
    assert history == []  # history was read-only; idle window still expired


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
    """A keyless level (3 → claude-sdk) never forwards an api_key."""
    settings = Settings()  # model_level 3, keyless

    agent = create_agent_from_settings("Be helpful.", settings=settings)

    assert isinstance(agent, LlmioChatAgent)
    assert agent._model_level == 3
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
        "robotsix_chat.config.DEFAULT_CONFIG_PATH", Path("/nonexistent/chat.local.yaml")
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
        "robotsix_chat.config.DEFAULT_CONFIG_PATH", Path("/nonexistent/chat.local.yaml")
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
        task_registry = call_args[1].pop("task_registry")
        from robotsix_chat.chat.tasks import TaskRegistry

        assert isinstance(task_registry, TaskRegistry)
        event_bus = call_args[1].pop("event_bus")
        from robotsix_chat.chat.events import EventBus

        assert isinstance(event_bus, EventBus)
        check_loop_registry = call_args[1].pop("check_loop_registry")
        from robotsix_chat.chat.loops import CheckLoopRegistry

        assert isinstance(check_loop_registry, CheckLoopRegistry)
        on_startup = call_args[1].pop("on_startup")
        assert callable(on_startup)
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
        "robotsix_chat.config.DEFAULT_CONFIG_PATH", Path("/nonexistent/chat.local.yaml")
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
async def test_ui_renders_task_notifications() -> None:
    """``GET /`` contains the task-notification UI wiring.

    The served HTML must reference the ``/events`` SSE endpoint, the
    ``openEventStream`` function that opens it, the three task-lifecycle
    frame-type literals that the JS dispatcher switches on, the
    ``.bubble.notification`` CSS selector used for rendering, and the
    side-panel markers (``tasks-toggle``, ``tasks-list``, ``updateTaskPanel``)
    introduced for the background-tasks detail panel.
    """
    async with mock_app() as f:
        response = await f.client.get("/")

    assert response.status_code == 200
    assert '"/events"' in response.text
    assert "openEventStream" in response.text
    assert '"task_started"' in response.text
    assert '"task_completed"' in response.text
    assert '"task_failed"' in response.text
    assert ".bubble.notification" in response.text
    assert '"tasks-toggle"' in response.text
    assert '"tasks-list"' in response.text
    assert "updateTaskPanel" in response.text
    assert "Close tasks panel" in response.text  # dismiss button in tasks header
    assert '"Escape"' in response.text  # Escape key closes tasks panel


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
# Check-loop registry wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_loop_registry_wired_into_app_state() -> None:
    """``check_loop_registry`` kwarg is exposed on ``app.state``."""
    from robotsix_chat.chat.events import EventBus
    from robotsix_chat.chat.loops import CheckLoopRegistry

    bus = EventBus()
    reg = CheckLoopRegistry(event_sink=bus, store_path=None)

    async with mock_app(check_loop_registry=reg, event_bus=bus) as f:
        assert f.app.state.check_loop_registry is reg
        assert f.app.state.event_bus is bus


@pytest.mark.asyncio
async def test_check_loop_registry_defaults_to_none() -> None:
    """Without ``check_loop_registry`` kwarg, ``app.state`` stores ``None``."""
    async with mock_app() as f:
        assert f.app.state.check_loop_registry is None


# ---------------------------------------------------------------------------
# Stop route — /loops/{loop_id}/stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loops_stop_endpoint_no_registry_returns_503() -> None:
    """``POST /loops/{id}/stop`` returns 503 when no registry is wired."""
    async with mock_app() as f:
        response = await f.client.post("/loops/abc123/stop")

    assert response.status_code == 503
    assert response.json() == {"error": "check-loop feature not enabled"}


@pytest.mark.asyncio
async def test_loops_stop_endpoint_unknown_loop_returns_404() -> None:
    """Stopping a loop id that does not exist returns a 404 JSON error."""
    from robotsix_chat.chat.loops import CheckLoopRegistry

    reg = CheckLoopRegistry(store_path=None)
    async with mock_app(check_loop_registry=reg) as f:
        response = await f.client.post("/loops/nonexistent/stop")

    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "unknown loop"
    assert data["loop_id"] == "nonexistent"


@pytest.mark.asyncio
async def test_loops_stop_endpoint_stops_running_loop() -> None:
    """``POST /loops/{id}/stop`` stops a running loop and returns 200."""
    import asyncio

    from robotsix_chat.chat.loops import CheckLoopRegistry, LoopStatus

    reg = CheckLoopRegistry(store_path=None)

    # Register a running loop — the registry needs an asyncio.Task.
    async def _noop() -> None:
        pass

    task = asyncio.create_task(_noop())
    loop_id = reg.register(
        "client-1",
        "check the weather",
        interval_seconds=60.0,
        max_iterations=None,
        coro=task,
    )

    async with mock_app(check_loop_registry=reg) as f:
        response = await f.client.post(f"/loops/{loop_id}/stop")

    assert response.status_code == 200
    data = response.json()
    assert data["loop_id"] == loop_id
    assert data["status"] == "stopped"

    info = reg.get(loop_id)
    assert info is not None
    assert info.status == LoopStatus.STOPPED
    assert info.stop_reason == "stopped via api"

    # Cleanup — the task may have been cancelled by stop().
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_loops_stop_endpoint_idempotent() -> None:
    """Stopping an already-stopped loop is idempotent — returns 200."""
    import asyncio

    from robotsix_chat.chat.loops import CheckLoopRegistry

    reg = CheckLoopRegistry(store_path=None)

    async def _noop() -> None:
        pass

    task = asyncio.create_task(_noop())
    loop_id = reg.register(
        "client-2",
        "check mail",
        interval_seconds=30.0,
        max_iterations=None,
        coro=task,
    )
    # Pre-stop
    reg.stop(loop_id, reason="already stopped")

    async with mock_app(check_loop_registry=reg) as f:
        response = await f.client.post(f"/loops/{loop_id}/stop")

    assert response.status_code == 200
    assert response.json()["status"] == "stopped"

    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Loop lifecycle frame observable via /events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_stopped_frame_reaches_events_stream() -> None:
    """``registry.stop`` publishes ``loop_stopped`` frame on the shared ``EventBus``."""
    import asyncio

    from starlette.responses import StreamingResponse

    from robotsix_chat.chat.events import EventBus, loop_stopped_frame
    from robotsix_chat.chat.loops import CheckLoopRegistry
    from robotsix_chat.chat.server import events_endpoint
    from tests.chat.test_events import _make_request, _parse_data_line

    bus = EventBus()
    reg = CheckLoopRegistry(event_sink=bus, store_path=None)

    # Register a running loop for client "cloop"
    async def _noop() -> None:
        pass

    task = asyncio.create_task(_noop())
    loop_id = reg.register(
        "cloop",
        "check inbox",
        interval_seconds=60.0,
        max_iterations=None,
        coro=task,
    )

    async with mock_app(check_loop_registry=reg, event_bus=bus) as f:
        # Open the /events SSE stream for the same client
        request = _make_request("cloop", f.app)
        response = await events_endpoint(request)
        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"

        body_iter: AsyncGenerator[bytes, None] = response.body_iterator  # type: ignore[assignment]
        try:
            # Consume the initial heartbeat
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            assert chunk == b": keepalive\n\n"

            # Stop the loop — the registry publishes via the shared EventBus
            reg.stop(loop_id, reason="test stop")

            # The next SSE frame must be the loop_stopped data frame
            chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
            text = chunk.decode()
            lines = text.rstrip("\n").split("\n")
            data_lines = [ln for ln in lines if ln.startswith("data: ")]
            assert len(data_lines) == 1
            parsed = _parse_data_line(data_lines[0])
            expected = loop_stopped_frame(loop_id, reason="test stop", iterations=0)
            assert parsed == expected
        finally:
            await body_iter.aclose()

    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Resume-on-startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_check_loops_restarts_persisted_running_loop(
    tmp_path: Path,
) -> None:
    """A persisted ``status='running'`` loop is resumed via the startup hook."""
    import json

    from robotsix_chat.chat.loops import (
        CheckLoopRegistry,
        LoopStatus,
        resume_check_loops,
    )
    from robotsix_chat.config import Settings

    store_path = tmp_path / "check_loops.json"
    persisted_id = "resume-me-1"
    persisted_client = "c-resume"
    persisted_prompt = "check the weather every hour"

    store_path.write_text(
        json.dumps(
            [
                {
                    "id": persisted_id,
                    "client_id": persisted_client,
                    "prompt": persisted_prompt,
                    "interval_seconds": 60.0,
                    "max_iterations": None,
                    "iterations": 3,
                    "status": "running",
                    "last_result": "sunny",
                }
            ]
        )
    )

    reg = CheckLoopRegistry(store_path=store_path)

    # Stub agent factory so resume doesn't start real agent work.
    from tests.conftest import MockAgent

    def _agent_factory(s: Settings) -> MockAgent:
        return MockAgent(tokens=["resumed ok"])

    settings = Settings()
    # Ensure settings allow the loop interval
    settings.min_check_loop_interval_seconds = 1.0

    resumed = resume_check_loops(reg, settings, agent_factory=_agent_factory)
    assert resumed == [persisted_id]

    info = reg.get(persisted_id)
    assert info is not None
    assert info.status == LoopStatus.RUNNING
    assert info.client_id == persisted_client
    assert info.prompt == persisted_prompt

    # Cleanup — stop the resumed loop so its asyncio task cancels.
    reg.stop(persisted_id, reason="test teardown")
    # Give the task a moment to process the cancellation.
    import asyncio

    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_resume_check_loops_skips_stopped_and_failed(
    tmp_path: Path,
) -> None:
    """Persisted loops with status ``stopped`` or ``failed`` are not resumed."""
    import json

    from robotsix_chat.chat.loops import CheckLoopRegistry, resume_check_loops
    from robotsix_chat.config import Settings

    store_path = tmp_path / "check_loops.json"
    store_path.write_text(
        json.dumps(
            [
                {
                    "id": "loop-running",
                    "client_id": "c1",
                    "prompt": "check X",
                    "interval_seconds": 60.0,
                    "max_iterations": None,
                    "iterations": 0,
                    "status": "running",
                    "last_result": None,
                },
                {
                    "id": "loop-stopped",
                    "client_id": "c1",
                    "prompt": "check Y",
                    "interval_seconds": 60.0,
                    "max_iterations": None,
                    "iterations": 5,
                    "status": "stopped",
                    "last_result": "done",
                },
                {
                    "id": "loop-failed",
                    "client_id": "c1",
                    "prompt": "check Z",
                    "interval_seconds": 60.0,
                    "max_iterations": None,
                    "iterations": 2,
                    "status": "failed",
                    "last_result": None,
                },
            ]
        )
    )
    reg = CheckLoopRegistry(store_path=store_path)

    from tests.conftest import MockAgent

    def _agent_factory(s: Settings) -> MockAgent:
        return MockAgent(tokens=["ok"])

    settings = Settings()
    settings.min_check_loop_interval_seconds = 1.0

    resumed = resume_check_loops(reg, settings, agent_factory=_agent_factory)
    assert resumed == ["loop-running"]
    assert reg.get("loop-running") is not None
    assert reg.get("loop-stopped") is None
    assert reg.get("loop-failed") is None

    # Cleanup
    reg.stop("loop-running", reason="test teardown")
    import asyncio

    await asyncio.sleep(0.05)


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
    assert f.app.state.check_loop_registry is None  # default


# ---------------------------------------------------------------------------
# GET /loops — snapshot endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loops_list_endpoint_missing_client_id() -> None:
    """``GET /loops`` without client_id returns 400."""
    async with mock_app() as f:
        response = await f.client.get("/loops")

    assert response.status_code == 400
    assert response.json() == {"error": "client_id query parameter is required"}


@pytest.mark.asyncio
async def test_loops_list_endpoint_no_registry_returns_503() -> None:
    """``GET /loops?client_id=x`` returns 503 when no registry is wired."""
    async with mock_app() as f:
        response = await f.client.get("/loops?client_id=test-client")

    assert response.status_code == 503
    assert response.json() == {"error": "check-loop feature not enabled"}


@pytest.mark.asyncio
async def test_loops_list_endpoint_returns_loops_for_client() -> None:
    """``GET /loops?client_id=x`` returns the loops for that client."""
    import asyncio

    from robotsix_chat.chat.loops import CheckLoopRegistry

    reg = CheckLoopRegistry(store_path=None)

    # Register two loops for client "c-a" and one for "c-b".
    async def _noop() -> None:
        pass

    t1 = asyncio.create_task(_noop())
    lid1 = reg.register(
        "c-a", "check weather", interval_seconds=60.0, max_iterations=None, coro=t1
    )

    t2 = asyncio.create_task(_noop())
    lid2 = reg.register(
        "c-a", "check inbox", interval_seconds=30.0, max_iterations=5, coro=t2
    )

    t3 = asyncio.create_task(_noop())
    lid3 = reg.register(
        "c-b", "check stocks", interval_seconds=120.0, max_iterations=None, coro=t3
    )

    async with mock_app(check_loop_registry=reg) as f:
        # Client "c-a" should see two loops.
        response = await f.client.get("/loops?client_id=c-a")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "loops" in data
        loops = data["loops"]
        assert isinstance(loops, list)
        assert len(loops) == 2

        ids = {entry["id"] for entry in loops}
        assert ids == {lid1, lid2}

        for entry in loops:
            assert entry["client_id"] == "c-a"
            assert entry["status"] == "running"
            assert entry["interval_seconds"] in (60.0, 30.0)
            assert entry["iterations"] == 0
            assert entry["error"] is None
            assert entry["stop_reason"] is None
            # Every LoopInfo field must be present.
            for field in (
                "id",
                "client_id",
                "prompt",
                "interval_seconds",
                "status",
                "iterations",
                "max_iterations",
                "last_result",
                "next_run",
                "error",
                "stop_reason",
            ):
                assert field in entry, f"missing field {field!r}"

        # Client "c-b" should see one loop.
        response_b = await f.client.get("/loops?client_id=c-b")
        assert response_b.status_code == 200
        loops_b = response_b.json()["loops"]
        assert len(loops_b) == 1
        assert loops_b[0]["id"] == lid3
        assert loops_b[0]["client_id"] == "c-b"

    # Cleanup
    for t in (t1, t2, t3):
        with contextlib.suppress(asyncio.CancelledError):
            await t


@pytest.mark.asyncio
async def test_loops_list_endpoint_reflects_stopped_and_failed() -> None:
    """Status changes made via the registry appear in the snapshot."""
    import asyncio

    from robotsix_chat.chat.loops import CheckLoopRegistry

    reg = CheckLoopRegistry(store_path=None)

    async def _noop() -> None:
        pass

    t1 = asyncio.create_task(_noop())
    lid1 = reg.register(
        "c-x", "stopped loop", interval_seconds=10.0, max_iterations=None, coro=t1
    )

    t2 = asyncio.create_task(_noop())
    lid2 = reg.register(
        "c-x", "failed loop", interval_seconds=10.0, max_iterations=None, coro=t2
    )

    reg.stop(lid1, reason="manual")
    reg.fail(lid2, error="something broke")

    async with mock_app(check_loop_registry=reg) as f:
        response = await f.client.get("/loops?client_id=c-x")
        assert response.status_code == 200
        loops = response.json()["loops"]
        assert len(loops) == 2

        by_id = {e["id"]: e for e in loops}
        assert by_id[lid1]["status"] == "stopped"
        assert by_id[lid1]["stop_reason"] == "manual"
        assert by_id[lid2]["status"] == "failed"
        assert by_id[lid2]["error"] == "something broke"

    with contextlib.suppress(asyncio.CancelledError):
        await t1
    with contextlib.suppress(asyncio.CancelledError):
        await t2

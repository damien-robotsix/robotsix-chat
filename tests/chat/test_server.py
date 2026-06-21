"""Tests for the chat SSE server."""

from __future__ import annotations

import json
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
        assert passed_agent._instruction == "You are a helpful assistant."
        # A conversation store is built from settings and forwarded; assert the
        # rest of the server options explicitly.
        conversation_store = call_args[1].pop("conversation_store")
        assert isinstance(conversation_store, ConversationStore)
        assert call_args[1] == {
            "host": "127.0.0.1",
            "port": 8080,
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

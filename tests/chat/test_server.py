"""Tests for the chat SSE server."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from robotsix_chat.chat.server import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_TOKEN_TYPE,
    create_agent_from_settings,
    create_app,
    run_server_from_config,
)
from robotsix_chat.config import Settings
from robotsix_chat.llm import LlmioChatAgent
from tests.conftest import MockAgent, http_client, mock_app

# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    async with mock_app() as f:
        response = await f.client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Chat endpoint — SSE streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_streams_tokens() -> None:
    async with mock_app(tokens=["Hello", " ", "world!"]) as f:
        response = await f.client.post("/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert response.headers["content-type"] == SSE_CONTENT_TYPE

    text = response.text
    # SSE uses \n\n as event delimiter.  Split on that, then extract the
    # JSON payload from each "data: ..." block.
    events = [e for e in text.split("\n\n") if e]
    frames: list[dict[str, object]] = []
    for e in events:
        if e.startswith("data: "):
            payload = e[len("data: ") :]
            frames.append(json.loads(payload))

    assert len(frames) >= 4  # 3 tokens + done
    assert frames[0] == {"type": SSE_TOKEN_TYPE, "content": "Hello"}
    assert frames[1] == {"type": SSE_TOKEN_TYPE, "content": " "}
    assert frames[2] == {"type": SSE_TOKEN_TYPE, "content": "world!"}
    assert frames[-1] == {"type": SSE_DONE_TYPE}


@pytest.mark.asyncio
async def test_chat_endpoint_opens_with_heartbeat() -> None:
    """The stream emits a heartbeat comment up front (and keeps the connection
    alive while the agent works), so a long quiet reply isn't dropped. The
    comment is not a ``data:`` frame, so it never parses as a token."""
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
    async with mock_app(tokens=["ok"]) as f:
        await f.client.post("/chat", json={"message": "hello world"})
        agent_ref = f.agent

    assert agent_ref.called_with == "hello world"


@pytest.mark.asyncio
async def test_chat_endpoint_sends_done_at_end() -> None:
    async with mock_app(tokens=["one", "two"]) as f:
        response = await f.client.post("/chat", json={"message": "x"})

    assert response.text.rstrip("\r\n").endswith(f'data: {{"type": "{SSE_DONE_TYPE}"}}')


# ---------------------------------------------------------------------------
# Chat endpoint — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_missing_message_field() -> None:
    async with mock_app() as f:
        response = await f.client.post("/chat", json={})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_message_not_a_string() -> None:
    async with mock_app() as f:
        response = await f.client.post("/chat", json={"message": 123})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_invalid_json() -> None:
    async with mock_app() as f:
        response = await f.client.post(
            "/chat", content=b"not json", headers={"Content-Type": "application/json"}
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_empty_message_string() -> None:
    async with mock_app() as f:
        response = await f.client.post("/chat", json={"message": ""})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_agent_raises() -> None:
    async with mock_app(error=RuntimeError("LLM went boom")) as f:
        response = await f.client.post("/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert SSE_CONTENT_TYPE in response.headers["content-type"]

    text = response.text
    events = [e for e in text.split("\n\n") if e]
    frames: list[dict[str, object]] = []
    for e in events:
        if e.startswith("data: "):
            frames.append(json.loads(e[len("data: ") :]))

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
    """``run_server_from_config()`` with no *agent* creates an
    ``LlmioChatAgent`` from ``Settings`` and forwards server options."""
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
    """``run_server_from_config(agent)`` forwards *agent* to
    ``run_server`` without creating a new one."""
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
    async with mock_app() as f:
        response = await f.client.get("/nonexistent")

    assert response.status_code == 404
    data = response.json()
    assert data == {"error": "not found"}


@pytest.mark.asyncio
async def test_wrong_method_on_known_route_returns_405() -> None:
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
    app = create_app(MockAgent())

    async with http_client(app) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in response.text
    assert "robotsix-agent-comm" in response.text or "Chat" in response.text


@pytest.mark.asyncio
async def test_ui_disabled_returns_404() -> None:
    """With ``serve_ui=False`` the root path is not registered."""
    app = create_app(MockAgent(), serve_ui=False)

    async with http_client(app) as client:
        response = await client.get("/")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cors_headers_present_when_configured() -> None:
    """A configured allowed origin receives ``access-control-allow-origin``."""
    app = create_app(MockAgent(), cors_allow_origins=["https://ui.example.com"])

    async with http_client(app) as client:
        response = await client.post(
            "/chat",
            json={"message": "hi"},
            headers={"Origin": "https://ui.example.com"},
        )

    assert response.headers.get("access-control-allow-origin") == (
        "https://ui.example.com"
    )


@pytest.mark.asyncio
async def test_no_cors_headers_by_default() -> None:
    """Without configuration, no CORS headers are added."""
    app = create_app(MockAgent())

    async with http_client(app) as client:
        response = await client.post(
            "/chat",
            json={"message": "hi"},
            headers={"Origin": "https://ui.example.com"},
        )

    assert "access-control-allow-origin" not in response.headers


# ---------------------------------------------------------------------------
# Correlation ID header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_correlation_id_header_in_response() -> None:
    """A custom ``correlation_id_header`` name reaches the middleware and
    is echoed back in the response."""
    app = create_app(MockAgent(), correlation_id_header="X-Custom-ID")

    async with http_client(app) as client:
        response = await client.get(
            "/health",
            headers={"X-Custom-ID": "11111111-1111-1111-1111-111111111111"},
        )

    assert response.status_code == 200
    assert response.headers["X-Custom-ID"] == "11111111-1111-1111-1111-111111111111"

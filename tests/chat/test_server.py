"""Tests for the chat SSE server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

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


class MockAgent:
    """A :class:`ChatAgent` that yields a fixed list of tokens."""

    def __init__(
        self,
        tokens: list[str] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.tokens = tokens or ["Hello", " ", "world!"]
        self.error = error
        self.called_with: str | None = None

    async def stream(self, message: str) -> AsyncIterator[str]:
        self.called_with = message
        if self.error is not None:
            raise self.error
        for token in self.tokens:
            yield token


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Chat endpoint — SSE streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_streams_tokens() -> None:
    agent = MockAgent(tokens=["Hello", " ", "world!"])
    app = create_app(agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/chat", json={"message": "hello"})

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
async def test_chat_endpoint_passes_message_to_agent() -> None:
    agent = MockAgent(tokens=["ok"])
    app = create_app(agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/chat", json={"message": "hello world"})

    assert agent.called_with == "hello world"


@pytest.mark.asyncio
async def test_chat_endpoint_sends_done_at_end() -> None:
    agent = MockAgent(tokens=["one", "two"])
    app = create_app(agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/chat", json={"message": "x"})

    assert response.text.rstrip("\r\n").endswith(f'data: {{"type": "{SSE_DONE_TYPE}"}}')


# ---------------------------------------------------------------------------
# Chat endpoint — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_missing_message_field() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/chat", json={})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_message_not_a_string() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/chat", json={"message": 123})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_invalid_json() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chat", content=b"not json", headers={"Content-Type": "application/json"}
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_empty_message_string() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/chat", json={"message": ""})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_agent_raises() -> None:
    error_agent = MockAgent(error=RuntimeError("LLM went boom"))
    app = create_app(error_agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/chat", json={"message": "hello"})

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
    assert agent._api_key == "sk-from-settings"


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
    assert result._api_key == "sk-env-test"


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
    agent = MockAgent()
    app = create_app(agent)

    with patch(
        "robotsix_chat.chat.server._load_ui_html",
        side_effect=RuntimeError("UI resource missing"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get("/")

    assert response.status_code == 500
    data = response.json()
    assert data == {"error": "internal server error"}


# ---------------------------------------------------------------------------
# Unknown routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_route_returns_404_json() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/nonexistent")

    assert response.status_code == 404
    data = response.json()
    assert data == {"error": "not found"}


@pytest.mark.asyncio
async def test_wrong_method_on_known_route_returns_405() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # POST /health is not a valid endpoint — Starlette returns 405.
        response = await client.post("/health")

    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Browser UI serving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_served_at_root_by_default() -> None:
    """``GET /`` returns the bundled browser chat UI HTML."""
    app = create_app(MockAgent())

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in response.text
    assert "robotsix-agent-comm" in response.text or "Chat" in response.text


@pytest.mark.asyncio
async def test_ui_disabled_returns_404() -> None:
    """With ``serve_ui=False`` the root path is not registered."""
    app = create_app(MockAgent(), serve_ui=False)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cors_headers_present_when_configured() -> None:
    """A configured allowed origin receives ``access-control-allow-origin``."""
    app = create_app(MockAgent(), cors_allow_origins=["https://ui.example.com"])

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
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

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chat",
            json={"message": "hi"},
            headers={"Origin": "https://ui.example.com"},
        )

    assert "access-control-allow-origin" not in response.headers

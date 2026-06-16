"""Tests for the chat SSE server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import pytest

from robotsix_chat import PROJECT_TITLE
from robotsix_chat.chat.server import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_TOKEN_TYPE,
    LLMChatAgent,
    create_agent_from_settings,
    create_app,
    run_server_from_config,
)
from robotsix_chat.config import Settings
from tests.conftest import http_client


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

    async with http_client(app) as client:
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

    async with http_client(app) as client:
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

    async with http_client(app) as client:
        await client.post("/chat", json={"message": "hello world"})

    assert agent.called_with == "hello world"


@pytest.mark.asyncio
async def test_chat_endpoint_sends_done_at_end() -> None:
    agent = MockAgent(tokens=["one", "two"])
    app = create_app(agent)

    async with http_client(app) as client:
        response = await client.post("/chat", json={"message": "x"})

    assert response.text.rstrip("\r\n").endswith(f'data: {{"type": "{SSE_DONE_TYPE}"}}')


# ---------------------------------------------------------------------------
# Chat endpoint — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_missing_message_field() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with http_client(app) as client:
        response = await client.post("/chat", json={})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_message_not_a_string() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with http_client(app) as client:
        response = await client.post("/chat", json={"message": 123})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_invalid_json() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with http_client(app) as client:
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

    async with http_client(app) as client:
        response = await client.post("/chat", json={"message": ""})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_agent_raises() -> None:
    error_agent = MockAgent(error=RuntimeError("LLM went boom"))
    app = create_app(error_agent)

    async with http_client(app) as client:
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
# LLMChatAgent adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_chat_agent_streams_tokens() -> None:
    """``LLMChatAgent`` delegates ``stream()`` to the wrapped ``Agent.run()``."""
    mock_agent = MagicMock()

    async def fake_run(message: str) -> AsyncIterator[str]:
        for token in ["Hi", " ", "there"]:
            yield token

    mock_agent.run = fake_run

    adapter = LLMChatAgent(mock_agent)
    tokens = [token async for token in adapter.stream("hello")]

    assert tokens == ["Hi", " ", "there"]


# ---------------------------------------------------------------------------
# create_agent_from_settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_from_settings_explicit() -> None:
    """``create_agent_from_settings`` wires LLM fields from a ``Settings`` object."""
    settings = Settings(
        llm_api_key="sk-from-settings",
        llm_model="gpt-5",
        llm_base_url="https://custom.example.com",
    )

    with patch("robotsix_chat.chat.server.Agent") as MockAgent:
        mock_agent_instance = MockAgent.return_value
        result = create_agent_from_settings("Be concise.", settings=settings)

        MockAgent.assert_called_once_with(
            instruction="Be concise.",
            api_key="sk-from-settings",
            model="gpt-5",
            base_url="https://custom.example.com",
            graceful_errors=False,
        )
        assert isinstance(result, LLMChatAgent)
        assert result._agent is mock_agent_instance


@pytest.mark.asyncio
async def test_create_agent_from_settings_graceful_errors_plumbed() -> None:
    """``create_agent_from_settings`` forwards ``graceful_errors=True`` to
    ``Agent``."""
    settings = Settings(
        llm_api_key="sk-test",
        graceful_errors=True,
    )

    with patch("robotsix_chat.chat.server.Agent") as MockAgent:
        create_agent_from_settings("Be concise.", settings=settings)

        MockAgent.assert_called_once_with(
            instruction="Be concise.",
            api_key="sk-test",
            model="gpt-4o-mini",
            base_url=None,
            graceful_errors=True,
        )


@pytest.mark.asyncio
async def test_create_agent_from_settings_uses_from_env_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_agent_from_settings`` loads from the environment when
    *settings* is ``None``."""
    monkeypatch.setenv("LLM_API_KEY", "sk-env-test")
    monkeypatch.setenv("LLM_MODEL", "gpt-4.1")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:8080/v1")

    with patch("robotsix_chat.chat.server.Agent") as MockAgent:
        result = create_agent_from_settings("Helpful bot.")

        MockAgent.assert_called_once_with(
            instruction="Helpful bot.",
            api_key="sk-env-test",
            model="gpt-4.1",
            base_url="http://localhost:8080/v1",
            graceful_errors=False,
        )
        assert isinstance(result, LLMChatAgent)


# ---------------------------------------------------------------------------
# run_server_from_config — LLM wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_server_from_config_creates_agent_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_server_from_config()`` with no *agent* creates an
    ``LLMChatAgent`` from ``Settings``."""
    monkeypatch.setenv("LLM_API_KEY", "sk-run-server-test")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("SERVER_PORT", "8080")

    with (
        patch("robotsix_chat.chat.server.Agent") as MockAgent,
        patch("robotsix_chat.chat.server.run_server") as mock_run_server,
    ):
        run_server_from_config()

        MockAgent.assert_called_once_with(
            instruction="You are a helpful assistant.",
            api_key="sk-run-server-test",
            model="gpt-4o",
            base_url=None,  # empty string → None via Settings
            graceful_errors=False,
        )
        # The agent passed to run_server is an LLMChatAgent wrapping
        # the constructed Agent.
        call_args = mock_run_server.call_args
        passed_agent = call_args[0][0]
        assert isinstance(passed_agent, LLMChatAgent)
        assert passed_agent._agent is MockAgent.return_value
        assert call_args[1] == {
            "host": "127.0.0.1",
            "port": 8080,
            "cors_allow_origins": [],
        }


@pytest.mark.asyncio
async def test_run_server_from_config_passes_explicit_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_server_from_config(agent)`` forwards *agent* to
    ``run_server`` without creating a new one."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    mock_agent = MagicMock()

    with patch("robotsix_chat.chat.server.run_server") as mock_run_server:
        run_server_from_config(mock_agent)

        mock_run_server.assert_called_once()
        passed_agent = mock_run_server.call_args[0][0]
        assert passed_agent is mock_agent


# ---------------------------------------------------------------------------
# Unknown routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_route_returns_404_json() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with http_client(app) as client:
        response = await client.get("/nonexistent")

    assert response.status_code == 404
    data = response.json()
    assert data == {"error": "not found"}


@pytest.mark.asyncio
async def test_wrong_method_on_known_route_returns_405() -> None:
    agent = MockAgent()
    app = create_app(agent)

    async with http_client(app) as client:
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

    async with http_client(app) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in response.text
    assert PROJECT_TITLE in response.text


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

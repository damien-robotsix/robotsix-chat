"""Tests for the ``llm.Agent`` async streaming wrapper."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robotsix_chat.llm import Agent

# ---------------------------------------------------------------------------
# 1. Instantiation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instantiate_with_explicit_client() -> None:
    """``Agent(instruction, client=mock_client)`` stores the wrapped
    ``llmio.Agent`` and passes the client through."""
    mock_client = MagicMock()

    with patch("robotsix_chat.llm.agent.llmio.Agent") as MockLLMIOAgent:
        mock_llmio_instance = MockLLMIOAgent.return_value
        agent = Agent("You are helpful.", client=mock_client)

        MockLLMIOAgent.assert_called_once_with(
            instruction="You are helpful.",
            client=mock_client,
            model="gpt-4o-mini",
            graceful_errors=False,
        )
        assert agent._agent is mock_llmio_instance


@pytest.mark.asyncio
async def test_instantiate_with_api_key_creates_client() -> None:
    """``Agent(instruction, api_key="sk-test")`` constructs an
    ``OpenAIClient`` internally and forwards it to ``llmio.Agent``."""
    with (
        patch("robotsix_chat.llm.agent.llmio.Agent") as MockLLMIOAgent,
        patch("robotsix_chat.llm.agent.OpenAIClient") as MockOpenAIClient,
    ):
        mock_client_instance = MockOpenAIClient.return_value
        agent = Agent("You are helpful.", api_key="sk-test")

        MockOpenAIClient.assert_called_once_with(api_key="sk-test", base_url=None)
        MockLLMIOAgent.assert_called_once_with(
            instruction="You are helpful.",
            client=mock_client_instance,
            model="gpt-4o-mini",
            graceful_errors=False,
        )
        assert agent._agent is MockLLMIOAgent.return_value


@pytest.mark.asyncio
async def test_instantiate_with_api_key_and_base_url() -> None:
    """``Agent(instruction, api_key="sk-test", base_url="http://local")``
    forwards ``base_url`` to ``OpenAIClient``."""
    with (
        patch("robotsix_chat.llm.agent.llmio.Agent") as MockLLMIOAgent,
        patch("robotsix_chat.llm.agent.OpenAIClient") as MockOpenAIClient,
    ):
        agent = Agent(
            "You are helpful.",
            api_key="sk-test",
            base_url="http://localhost:11434",
        )

        MockOpenAIClient.assert_called_once_with(
            api_key="sk-test", base_url="http://localhost:11434"
        )
        assert agent._agent is MockLLMIOAgent.return_value


# ---------------------------------------------------------------------------
# 2. Tool registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_registration() -> None:
    """``@agent.tool`` adds the function to the underlying ``llmio.Agent``
    and the decorated function remains callable directly."""
    with patch("robotsix_chat.llm.agent.llmio.Agent") as MockLLMIOAgent:
        mock_llmio = MockLLMIOAgent.return_value

        # Simulate the real llmio.Agent.tool behaviour: when called with
        # a function, return the function unchanged; when called without,
        # return a decorator that records the function.
        def fake_tool(
            fn: Callable[..., Any] | None = None, *, strict: bool = False
        ) -> Callable[..., Any]:
            if fn is not None:
                return fn

            def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
                return func

            return decorator

        mock_llmio.tool = fake_tool

        agent = Agent("You are helpful.", api_key="sk-test")

        # -- bare decorator form -----------------------------------------
        @agent.tool
        def get_time() -> str:
            """Return the current server time."""
            return "2025-01-01T00:00:00"

        # The decorated function is callable and returns its value
        assert get_time() == "2025-01-01T00:00:00"

        # -- parameterised form ------------------------------------------
        @agent.tool(strict=True)
        def get_date() -> str:
            """Return the current date."""
            return "2025-01-01"

        assert get_date() == "2025-01-01"


# ---------------------------------------------------------------------------
# 3. run() — streaming helpers
# ---------------------------------------------------------------------------


def _make_on_stream(
    callbacks: list[Callable[..., Any]],
) -> Callable[..., Any]:
    """Return a function that behaves like ``llmio.Agent.on_stream``.

    The returned callable stores *fn* in *callbacks* and returns it
    unchanged, matching the real decorator contract.
    """

    def on_stream(fn: Callable[..., Any]) -> Callable[..., Any]:
        callbacks.append(fn)
        return fn

    return on_stream


# ---------------------------------------------------------------------------
# 4. run() — streaming tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_streams_tokens() -> None:
    """Mock ``llmio.Agent.speak`` and ``on_stream`` to simulate delta
    callbacks; ``run()`` yields the expected tokens."""
    with patch("robotsix_chat.llm.agent.llmio.Agent") as MockLLMIOAgent:
        mock_llmio = MockLLMIOAgent.return_value

        _stream_callbacks: list[Callable[..., Any]] = []
        mock_llmio._stream_callbacks = _stream_callbacks
        mock_llmio.on_stream = _make_on_stream(_stream_callbacks)

        async def fake_speak(
            message: str,
            history: list[Any] | None = None,
            stream: bool = False,
            _context: object = None,
        ) -> object:
            del message, history, _context
            for cb in _stream_callbacks:
                cb(delta="Hello")
                cb(delta=" ")
                cb(delta="world")
            from llmio.agent import AgentResponse

            return AgentResponse(messages=["Hello world"], history=[])

        mock_llmio.speak = fake_speak

        agent = Agent("You are helpful.", api_key="sk-test")
        tokens: list[str] = []
        async for token in agent.run("hello"):
            tokens.append(token)

        assert tokens == ["Hello", " ", "world"]


@pytest.mark.asyncio
async def test_run_yields_error_on_api_failure() -> None:
    """When ``speak()`` raises, ``run()`` yields an ``"Error: ..."`` token
    and stops."""
    with patch("robotsix_chat.llm.agent.llmio.Agent") as MockLLMIOAgent:
        mock_llmio = MockLLMIOAgent.return_value
        _stream_callbacks: list[Callable[..., Any]] = []
        mock_llmio._stream_callbacks = _stream_callbacks
        mock_llmio.on_stream = _make_on_stream(_stream_callbacks)

        async def fake_speak(
            message: str,
            history: list[Any] | None = None,
            stream: bool = False,
            _context: object = None,
        ) -> None:
            del message, history, stream, _context
            raise RuntimeError("API connection refused")

        mock_llmio.speak = fake_speak

        agent = Agent("You are helpful.", api_key="sk-test")
        tokens: list[str] = []
        async for token in agent.run("hello"):
            tokens.append(token)

        assert len(tokens) == 1
        assert tokens[0].startswith("Error:")
        assert "API connection refused" in tokens[0]


@pytest.mark.asyncio
async def test_run_yields_error_on_bad_tool_call() -> None:
    """When ``speak()`` raises ``llmio.BadToolCall``, ``run()`` yields an
    error token."""
    with patch("robotsix_chat.llm.agent.llmio.Agent") as MockLLMIOAgent:
        mock_llmio = MockLLMIOAgent.return_value
        _stream_callbacks: list[Callable[..., Any]] = []
        mock_llmio._stream_callbacks = _stream_callbacks
        mock_llmio.on_stream = _make_on_stream(_stream_callbacks)

        from llmio import BadToolCall

        async def fake_speak(
            message: str,
            history: list[Any] | None = None,
            stream: bool = False,
            _context: object = None,
        ) -> None:
            del message, history, stream, _context
            raise BadToolCall("bad args")

        mock_llmio.speak = fake_speak

        agent = Agent("You are helpful.", api_key="sk-test")
        tokens: list[str] = []
        async for token in agent.run("hello"):
            tokens.append(token)

        assert len(tokens) == 1
        assert tokens[0].startswith("Error:")
        assert "bad args" in tokens[0]


@pytest.mark.asyncio
async def test_run_with_history() -> None:
    """Passing a non-empty *history* list forwards it to
    ``llmio.Agent.speak``."""
    with patch("robotsix_chat.llm.agent.llmio.Agent") as MockLLMIOAgent:
        mock_llmio = MockLLMIOAgent.return_value
        _stream_callbacks: list[Callable[..., Any]] = []
        mock_llmio._stream_callbacks = _stream_callbacks
        mock_llmio.on_stream = _make_on_stream(_stream_callbacks)

        speak_called_with: list[tuple[str, list[Any] | None]] = []

        async def fake_speak(
            message: str,
            history: list[Any] | None = None,
            **kwargs: object,
        ) -> object:
            speak_called_with.append((message, history))
            from llmio.agent import AgentResponse

            return AgentResponse(messages=[], history=[])

        mock_llmio.speak = fake_speak

        agent = Agent("You are helpful.", api_key="sk-test")
        history: list[Any] = [{"role": "user", "content": "previous"}]
        tokens: list[str] = []
        async for token in agent.run("hello", history=history):
            tokens.append(token)

        assert len(speak_called_with) == 1
        _, passed_history = speak_called_with[0]
        assert passed_history == history
        assert tokens == []


@pytest.mark.asyncio
async def test_run_empty_response() -> None:
    """When ``speak()`` returns an empty message list, ``run()`` yields
    nothing and exits cleanly."""
    with patch("robotsix_chat.llm.agent.llmio.Agent") as MockLLMIOAgent:
        mock_llmio = MockLLMIOAgent.return_value
        _stream_callbacks: list[Callable[..., Any]] = []
        mock_llmio._stream_callbacks = _stream_callbacks
        mock_llmio.on_stream = _make_on_stream(_stream_callbacks)

        async def fake_speak(
            message: str,
            history: list[Any] | None = None,
            stream: bool = False,
            _context: object = None,
        ) -> object:
            del message, history, stream, _context
            from llmio.agent import AgentResponse

            return AgentResponse(messages=[], history=[])

        mock_llmio.speak = fake_speak

        agent = Agent("You are helpful.", api_key="sk-test")
        tokens: list[str] = []
        async for token in agent.run("hello"):
            tokens.append(token)

        assert tokens == []

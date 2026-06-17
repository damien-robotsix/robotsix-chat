"""Tests for :class:`LlmioChatAgent` — the robotsix-llmio-backed chat agent."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robotsix_chat.llm import LlmioChatAgent


def _patched_create_model(output: str = "hi there") -> tuple[MagicMock, MagicMock]:
    """Return (create_model mock, handle mock) wired so ``build_agent().run()``
    resolves to a result whose ``.output`` is *output*."""
    handle = MagicMock()

    async def fake_run(message: str) -> MagicMock:
        result = MagicMock()
        result.output = output
        return result

    handle.run = fake_run
    handle.close = MagicMock()

    provider = MagicMock()
    provider.build_agent.return_value = handle

    create_model = MagicMock(return_value=provider)
    return create_model, handle


@pytest.mark.asyncio
async def test_stream_yields_block_response() -> None:
    """``stream`` yields the agent's full reply as a single block."""
    create_model, handle = _patched_create_model("Hello world!")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["Hello world!"]
    handle.close.assert_called_once()  # handle is always closed


@pytest.mark.asyncio
async def test_keyless_level_forwards_no_api_key() -> None:
    """With no api_key (keyless level), ``create_model`` gets only the level."""
    create_model, _ = _patched_create_model()

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        _ = [c async for c in agent.stream("hi")]

    create_model.assert_called_once_with(level=3)


@pytest.mark.asyncio
async def test_key_bearing_level_forwards_api_key() -> None:
    """An api_key is forwarded to ``create_model``; ``build_agent`` gets the level."""
    create_model, _ = _patched_create_model()
    provider = create_model.return_value

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(
            model_level=1, instruction="Be helpful.", api_key="sk-or-test"
        )
        _ = [c async for c in agent.stream("hi")]

    create_model.assert_called_once_with(level=1, api_key="sk-or-test")
    provider.build_agent.assert_called_once_with(level=1, system_prompt="Be helpful.")


@pytest.mark.asyncio
async def test_empty_output_yields_nothing() -> None:
    """An empty reply yields no chunks (and still closes the handle)."""
    create_model, handle = _patched_create_model("")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=1, instruction="Be helpful.", api_key="k")
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == []
    handle.close.assert_called_once()


@pytest.mark.asyncio
async def test_handle_closed_on_error() -> None:
    """If the underlying run raises, the handle is still closed."""
    handle = MagicMock()

    async def boom(message: str) -> None:
        raise RuntimeError("backend exploded")

    handle.run = boom
    handle.close = MagicMock()
    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model = MagicMock(return_value=provider)

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        with pytest.raises(RuntimeError, match="backend exploded"):
            _ = [c async for c in agent.stream("hi")]

    handle.close.assert_called_once()

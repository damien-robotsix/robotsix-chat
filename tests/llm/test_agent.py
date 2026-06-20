"""Tests for :class:`LlmioChatAgent` — the robotsix-llmio-backed chat agent."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from robotsix_chat.llm import LlmioChatAgent


class _RecordingMemory:
    """A ChatMemory stub that records remember() calls and returns a fixed recall."""

    def __init__(self, recall: str = "") -> None:
        self._recall = recall
        self.remembered: list[tuple[str, str]] = []

    async def setup(self) -> None:
        return None

    async def recall(self, query: str) -> str:
        return self._recall

    async def remember(self, user_message: str, assistant_message: str) -> None:
        self.remembered.append((user_message, assistant_message))


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
            model_level=1,
            instruction="Be helpful.",
            api_key="sk-or-test",  # pragma: allowlist secret
        )
        _ = [c async for c in agent.stream("hi")]

    create_model.assert_called_once_with(
        level=1,
        api_key="sk-or-test",  # pragma: allowlist secret
    )
    provider.build_agent.assert_called_once_with(
        level=1, system_prompt="Be helpful.", tools=None
    )


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


# ---------------------------------------------------------------------------
# Memory integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recalled_memory_injected_into_system_prompt() -> None:
    """Recalled memory is appended to the system prompt for that call."""
    create_model, _ = _patched_create_model("ok")
    provider = create_model.return_value
    memory = _RecordingMemory(recall="Damien prefers Python.")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(
            model_level=3, instruction="Be helpful.", memory=memory
        )
        _ = [c async for c in agent.stream("hi")]

    system_prompt = provider.build_agent.call_args.kwargs["system_prompt"]
    assert system_prompt.startswith("Be helpful.")
    assert "Damien prefers Python." in system_prompt


@pytest.mark.asyncio
async def test_no_recall_leaves_prompt_unchanged() -> None:
    """With empty recall, the system prompt is exactly the instruction."""
    create_model, _ = _patched_create_model("ok")
    provider = create_model.return_value

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(
            model_level=3, instruction="Be helpful.", memory=_RecordingMemory("")
        )
        _ = [c async for c in agent.stream("hi")]

    assert provider.build_agent.call_args.kwargs["system_prompt"] == "Be helpful."


@pytest.mark.asyncio
async def test_exchange_persisted_in_background() -> None:
    """After a reply, the (message, reply) exchange is handed to memory."""
    create_model, _ = _patched_create_model("the reply")
    memory = _RecordingMemory()

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(
            model_level=3, instruction="Be helpful.", memory=memory
        )
        _ = [c async for c in agent.stream("the question")]
        # Let the fire-and-forget write task run.
        await asyncio.sleep(0)

    assert memory.remembered == [("the question", "the reply")]


@pytest.mark.asyncio
async def test_empty_reply_not_persisted() -> None:
    """An empty reply yields no chunks and nothing is written to memory."""
    create_model, _ = _patched_create_model("")
    memory = _RecordingMemory()

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(
            model_level=3, instruction="Be helpful.", memory=memory
        )
        chunks = [c async for c in agent.stream("hi")]
        await asyncio.sleep(0)

    assert chunks == []
    assert memory.remembered == []

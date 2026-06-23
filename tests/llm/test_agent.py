"""Tests for :class:`LlmioChatAgent` — the robotsix-llmio-backed chat agent."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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
    """Return patched create_model and handle wired for the given output."""
    handle = MagicMock()

    async def fake_run(message: str, *, message_history: object = None) -> MagicMock:
        handle.run_calls.append(
            {"message": message, "message_history": message_history}
        )
        result = MagicMock()
        result.output = output
        return result

    handle.run_calls = []
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
    kwargs = provider.build_agent.call_args.kwargs
    assert kwargs["level"] == 1
    assert kwargs["tools"] is None
    # The chat must never expose the SDK's built-in tools.
    assert kwargs["builtin_tools"] is False
    # The instruction is preserved (with the no-system-access guard appended).
    assert kwargs["system_prompt"].startswith("Be helpful.")


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

    async def boom(message: str, *, message_history: object = None) -> None:
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


async def _agent_with_memory(
    output: str = "hi there",
    recall: str = "",
    message: str = "hi",
) -> tuple[MagicMock, LlmioChatAgent, list[str], _RecordingMemory]:
    """Create an agent with patched create_model and RecordingMemory.

    Stream a message and return captured objects.
    """
    create_model, _ = _patched_create_model(output)
    provider = create_model.return_value
    memory = _RecordingMemory(recall=recall)

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.", memory=memory)
        chunks = [c async for c in agent.stream(message)]

    return provider, agent, chunks, memory


@pytest.mark.asyncio
async def test_recalled_memory_injected_into_system_prompt() -> None:
    """Recalled memory is appended to the system prompt for that call."""
    provider, _, _, _ = await _agent_with_memory(
        output="ok", recall="Damien prefers Python."
    )

    system_prompt = provider.build_agent.call_args.kwargs["system_prompt"]
    assert system_prompt.startswith("Be helpful.")
    assert "Damien prefers Python." in system_prompt


@pytest.mark.asyncio
async def test_no_recall_adds_no_memory_block() -> None:
    """Keep the system prompt clean when recall is empty.

    With no recalled memory, the system prompt has the instruction and
    guard but no memory block.
    """
    provider, _, _, _ = await _agent_with_memory(output="ok")

    system_prompt = provider.build_agent.call_args.kwargs["system_prompt"]
    assert system_prompt.startswith("Be helpful.")
    assert "no ability to run shell commands" in system_prompt  # the guard
    assert "# Relevant memory" not in system_prompt  # no recall block


@pytest.mark.asyncio
async def test_exchange_persisted_in_background() -> None:
    """After a reply, the (message, reply) exchange is handed to memory."""
    _, _, _, memory = await _agent_with_memory(
        output="the reply", message="the question"
    )
    # Let the fire-and-forget write task run.
    await asyncio.sleep(0)

    assert memory.remembered == [("the question", "the reply")]


@pytest.mark.asyncio
async def test_empty_reply_not_persisted() -> None:
    """An empty reply yields no chunks and nothing is written to memory."""
    _, _, chunks, memory = await _agent_with_memory(output="")
    await asyncio.sleep(0)

    assert chunks == []
    assert memory.remembered == []


# ---------------------------------------------------------------------------
# Conversation history & trace session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_passed_as_message_history() -> None:
    """Prior turns are rendered into a pydantic-ai message history for the run."""
    from pydantic_ai.messages import ModelRequest, ModelResponse

    create_model, handle = _patched_create_model("next reply")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        _ = [
            c
            async for c in agent.stream(
                "third", history=[("first", "1st reply"), ("second", "2nd reply")]
            )
        ]

    message_history = handle.run_calls[0]["message_history"]
    # Two turns → request/response per turn, in order.
    assert [type(m) for m in message_history] == [
        ModelRequest,
        ModelResponse,
        ModelRequest,
        ModelResponse,
    ]


@pytest.mark.asyncio
async def test_no_history_passes_none() -> None:
    """With no prior turns, message_history is None (a plain single query)."""
    create_model, handle = _patched_create_model("reply")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        _ = [c async for c in agent.stream("hi")]

    assert handle.run_calls[0]["message_history"] is None


@pytest.mark.asyncio
async def test_session_id_wraps_run_in_langfuse_session() -> None:
    """When a session id is given, the run executes inside langfuse_session."""
    create_model, _ = _patched_create_model("reply")
    seen: list[str] = []

    import contextlib

    @contextlib.contextmanager
    def fake_session(session_id: str):  # type: ignore[no-untyped-def]
        seen.append(session_id)
        yield

    with (
        patch("robotsix_chat.llm.agent.create_model", create_model),
        patch(
            "robotsix_llmio.core.tracing.langfuse_session", fake_session, create=True
        ),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        _ = [c async for c in agent.stream("hi", session_id="sess-123")]

    assert seen == ["sess-123"]


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_on_transient_error() -> None:
    """Transient error on first ``handle.run`` is retried; success yields reply."""
    call_count = 0

    async def fail_then_pass(
        _message: str, *, message_history: object = None
    ) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # A ValueError with a ValidationError-ish flavour is transient
            # when we patch the detector; the real detector would catch
            # OpenRouter's finish_reason='error' ValidationError.
            raise ValueError("simulated transient hiccup")
        result = MagicMock()
        result.output = "recovered reply"
        return result

    handle = MagicMock()
    handle.run = fail_then_pass
    handle.close = MagicMock()

    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    with (
        patch("robotsix_chat.llm.agent.create_model", create_model_patch),
        patch("robotsix_chat.llm.agent.is_openrouter_transient", return_value=True),
        patch("robotsix_chat.llm.agent.asyncio.sleep", new=AsyncMock()),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["recovered reply"]
    assert provider.build_agent.call_count == 2  # fresh handle per attempt
    assert handle.close.call_count == 2


@pytest.mark.asyncio
async def test_no_retry_on_non_transient_error() -> None:
    """Non-transient errors raise immediately with no retry."""
    handle = MagicMock()

    async def boom(_message: str, *, message_history: object = None) -> None:
        raise RuntimeError("backend exploded")

    handle.run = boom
    handle.close = MagicMock()

    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    with (
        patch("robotsix_chat.llm.agent.create_model", create_model_patch),
        patch("robotsix_chat.llm.agent.is_openrouter_transient", return_value=False),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        with pytest.raises(RuntimeError, match="backend exploded"):
            _ = [c async for c in agent.stream("hi")]

    assert provider.build_agent.call_count == 1
    assert handle.close.call_count == 1


@pytest.mark.asyncio
async def test_retries_exhausted_on_persistent_transient() -> None:
    """Persistent transient errors exhaust max attempts then re-raise."""
    from robotsix_chat.llm.agent import _MAX_RUN_ATTEMPTS

    handle = MagicMock()

    async def always_boom(_message: str, *, message_history: object = None) -> None:
        raise ValueError("persistent transient")

    handle.run = always_boom
    handle.close = MagicMock()

    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    with (
        patch("robotsix_chat.llm.agent.create_model", create_model_patch),
        patch("robotsix_chat.llm.agent.is_openrouter_transient", return_value=True),
        patch("robotsix_chat.llm.agent.asyncio.sleep", new=AsyncMock()),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        with pytest.raises(ValueError, match="persistent transient"):
            _ = [c async for c in agent.stream("hi")]

    assert provider.build_agent.call_count == _MAX_RUN_ATTEMPTS
    assert handle.close.call_count == _MAX_RUN_ATTEMPTS


@pytest.mark.asyncio
async def test_retry_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Each retry attempt logs at WARNING with the exception type."""
    call_count = 0

    async def fail_twice(_message: str, *, message_history: object = None) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise ValueError("transient blip")
        result = MagicMock()
        result.output = "ok"
        return result

    handle = MagicMock()
    handle.run = fail_twice
    handle.close = MagicMock()

    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    with (
        patch("robotsix_chat.llm.agent.create_model", create_model_patch),
        patch("robotsix_chat.llm.agent.is_openrouter_transient", return_value=True),
        patch("robotsix_chat.llm.agent.asyncio.sleep", new=AsyncMock()),
        caplog.at_level("WARNING", logger="robotsix_chat.llm.agent"),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        _ = [c async for c in agent.stream("hi")]

    assert len(caplog.records) == 2
    for record in caplog.records:
        assert record.levelname == "WARNING"
        assert "transient backend error on attempt" in record.message
        assert "ValueError" in record.message
        assert "retrying" in record.message


@pytest.mark.asyncio
async def test_retry_sleeps_backoff() -> None:
    """asyncio.sleep is awaited with the backoff schedule on each retry."""
    call_count = 0
    from robotsix_chat.llm.agent import _RETRY_BACKOFFS

    async def fail_twice(_message: str, *, message_history: object = None) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise ValueError("transient")
        result = MagicMock()
        result.output = "ok"
        return result

    handle = MagicMock()
    handle.run = fail_twice
    handle.close = MagicMock()

    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    sleep_mock = AsyncMock()

    with (
        patch("robotsix_chat.llm.agent.create_model", create_model_patch),
        patch("robotsix_chat.llm.agent.is_openrouter_transient", return_value=True),
        patch("robotsix_chat.llm.agent.asyncio.sleep", new=sleep_mock),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        _ = [c async for c in agent.stream("hi")]

    assert sleep_mock.await_count == 2
    # First retry → _RETRY_BACKOFFS[0], second → _RETRY_BACKOFFS[1]
    assert sleep_mock.await_args_list[0].args == (_RETRY_BACKOFFS[0],)
    assert sleep_mock.await_args_list[1].args == (_RETRY_BACKOFFS[1],)


# ---------------------------------------------------------------------------
# Image attachments — multimodal prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_with_images_builds_multimodal_prompt() -> None:
    """With images, handle.run receives a list with text + BinaryContent parts."""
    from pydantic_ai.messages import BinaryContent

    create_model, handle = _patched_create_model("I see an image!")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=1, instruction="Be helpful.", api_key="k")
        chunks = [
            c
            async for c in agent.stream(
                "describe this", images=[("image/png", b"fake-png-data")]
            )
        ]

    assert chunks == ["I see an image!"]
    run_arg = handle.run_calls[0]["message"]
    assert isinstance(run_arg, list)
    assert len(run_arg) == 2
    assert run_arg[0] == "describe this"
    assert isinstance(run_arg[1], BinaryContent)
    assert run_arg[1].data == b"fake-png-data"
    assert run_arg[1].media_type == "image/png"


@pytest.mark.asyncio
async def test_stream_with_images_only_no_text() -> None:
    """Images-only (empty message) builds a list of just BinaryContent parts."""
    from pydantic_ai.messages import BinaryContent

    create_model, handle = _patched_create_model("nice pic")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=1, instruction="Be helpful.", api_key="k")
        chunks = [
            c async for c in agent.stream("", images=[("image/jpeg", b"jpeg-data")])
        ]

    assert chunks == ["nice pic"]
    run_arg = handle.run_calls[0]["message"]
    assert isinstance(run_arg, list)
    assert len(run_arg) == 1
    assert isinstance(run_arg[0], BinaryContent)
    assert run_arg[0].data == b"jpeg-data"
    assert run_arg[0].media_type == "image/jpeg"


@pytest.mark.asyncio
async def test_stream_without_images_still_passes_plain_string() -> None:
    """With no images, handle.run receives a plain str (behaviour unchanged)."""
    create_model, handle = _patched_create_model("text-only reply")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("hello")]

    assert chunks == ["text-only reply"]
    run_arg = handle.run_calls[0]["message"]
    assert isinstance(run_arg, str)
    assert run_arg == "hello"

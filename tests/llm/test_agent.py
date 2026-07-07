"""Tests for :class:`LlmioChatAgent` — the robotsix-llmio-backed chat agent."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from robotsix_chat.llm import LlmioChatAgent


class _RecordingMemory:
    """A ChatMemory stub that records remember() calls and returns a fixed recall."""

    def __init__(self, recall: str = "") -> None:
        self._recall = recall
        self.remembered: list[tuple[str, str, str | None]] = []

    async def setup(self) -> None:
        return None

    async def recall(self, query: str, *, session_id: str | None = None) -> str:
        return self._recall

    async def remember(
        self,
        user_message: str,
        assistant_message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        self.remembered.append((user_message, assistant_message, session_id))


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
async def test_recalled_memory_prepended_to_user_turn() -> None:
    """Recalled memory goes into the current user turn, not the system prompt."""
    provider, _, _, _ = await _agent_with_memory(
        output="ok", recall="Damien prefers Python.", message="hi"
    )

    handle = provider.build_agent.return_value
    sent = handle.run_calls[0]["message"]
    assert sent.startswith("# Relevant memory")
    assert "Damien prefers Python." in sent
    assert sent.endswith("hi")  # the user's text closes the turn

    # The system prompt stays byte-stable (the head of the provider's
    # cacheable prefix must never carry per-message recall text).
    system_prompt = provider.build_agent.call_args.kwargs["system_prompt"]
    assert system_prompt == "Be helpful."


@pytest.mark.asyncio
async def test_no_recall_adds_no_memory_block() -> None:
    """With no recalled memory the message and system prompt are untouched."""
    provider, _, _, _ = await _agent_with_memory(output="ok", message="hi")

    handle = provider.build_agent.return_value
    assert handle.run_calls[0]["message"] == "hi"
    system_prompt = provider.build_agent.call_args.kwargs["system_prompt"]
    assert system_prompt.startswith("Be helpful.")
    assert "# Relevant memory" not in system_prompt  # no recall block


@pytest.mark.asyncio
async def test_system_prompt_identical_with_and_without_recall() -> None:
    """Recall never alters the system prompt (prompt-cache stability)."""
    provider_plain, _, _, _ = await _agent_with_memory(output="ok")
    provider_recall, _, _, _ = await _agent_with_memory(
        output="ok", recall="Damien prefers Python."
    )

    plain = provider_plain.build_agent.call_args.kwargs["system_prompt"]
    with_recall = provider_recall.build_agent.call_args.kwargs["system_prompt"]
    assert plain == with_recall


@pytest.mark.asyncio
async def test_exchange_persisted_in_background() -> None:
    """After a reply, the (message, reply) exchange is handed to memory."""
    _, _, _, memory = await _agent_with_memory(
        output="the reply", message="the question"
    )
    # Let the fire-and-forget write task run.
    await asyncio.sleep(0)

    assert memory.remembered == [("the question", "the reply", None)]


@pytest.mark.asyncio
async def test_empty_reply_not_persisted() -> None:
    """An empty reply yields no chunks and nothing is written to memory."""
    _, _, chunks, memory = await _agent_with_memory(output="")
    await asyncio.sleep(0)

    assert chunks == []
    assert memory.remembered == []


@pytest.mark.asyncio
async def test_session_id_forwarded_to_memory() -> None:
    """session_id from agent.stream is threaded to both recall and remember."""
    create_model, _ = _patched_create_model("ok")
    memory = _RecordingMemory(recall="some recall")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.", memory=memory)
        _ = [c async for c in agent.stream("hi", session_id="sess-abc")]

    await asyncio.sleep(0)
    assert memory.remembered == [("hi", "ok", "sess-abc")]


# ---------------------------------------------------------------------------
# event_sink — live claudeSDK activity forwarding
# ---------------------------------------------------------------------------


class _RecordingEventSink:
    """An EventSink stub that records every published (session_id, frame)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, session_id: str, frame: dict[str, object]) -> None:
        self.published.append((session_id, frame))


def _patched_create_model_with_activity(
    output: str = "hi there",
) -> tuple[MagicMock, MagicMock]:
    """Like _patched_create_model, but ``run`` fires one activity event.

    It fires via the ambient ``activity_events()`` contextvar while it
    "runs" — simulating what robotsix-llmio's ``_stream_query`` does
    internally.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKActivityEvent
    from robotsix_llmio.claude_sdk._stream import _current_on_event

    handle = MagicMock()

    async def fake_run(message: str, *, message_history: object = None) -> MagicMock:
        on_event = _current_on_event.get()
        if on_event is not None:
            on_event(
                ClaudeSDKActivityEvent(
                    kind="tool_call", turn=1, tool_name="search", detail="{}"
                )
            )
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
async def test_event_sink_receives_activity_frame() -> None:
    """A configured event_sink gets an ``activity`` frame published.

    Scoped to the turn's session_id, for an event the claudeSDK run fires.
    """
    create_model, _ = _patched_create_model_with_activity()
    sink = _RecordingEventSink()

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(
            model_level=3, instruction="Be helpful.", event_sink=sink
        )
        _ = [c async for c in agent.stream("hi", session_id="sess-abc")]

    from robotsix_chat.chat.events import SSE_ACTIVITY_TYPE

    assert sink.published == [
        (
            "sess-abc",
            {
                "type": SSE_ACTIVITY_TYPE,
                "kind": "tool_call",
                "turn": 1,
                "tool_name": "search",
                "detail": "{}",
                "is_error": False,
            },
        )
    ]


@pytest.mark.asyncio
async def test_no_event_sink_configured_is_silent() -> None:
    """Without an event_sink, a stream() call behaves exactly as before.

    No callback is installed, and nothing raises.
    """
    create_model, _ = _patched_create_model_with_activity()

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("hi", session_id="sess-abc")]

    assert chunks == ["hi there"]


@pytest.mark.asyncio
async def test_event_sink_configured_but_no_session_id_is_silent() -> None:
    """event_sink is configured, but stream() is called without a session_id.

    A stateless single query has nowhere to scope the frame, so no callback
    is installed and nothing is published.
    """
    create_model, _ = _patched_create_model_with_activity()
    sink = _RecordingEventSink()

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(
            model_level=3, instruction="Be helpful.", event_sink=sink
        )
        _ = [c async for c in agent.stream("hi")]  # no session_id

    assert sink.published == []


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


# ---------------------------------------------------------------------------
# Usage-exhausted tier fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_exhausted_falls_back_to_another_tier() -> None:
    """ClaudeSDKUsageExhaustedError at level 4 falls back to level 3 (opus).

    Falls back for the SAME turn instead of surfacing the raw error text.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    level4_handle = MagicMock()

    async def exhausted(_message: str, *, message_history: object = None) -> None:
        raise ClaudeSDKUsageExhaustedError("You're out of usage credits")

    level4_handle.run = exhausted
    level4_handle.close = MagicMock()
    level4_provider = MagicMock()
    level4_provider.build_agent.return_value = level4_handle

    level3_handle = MagicMock()

    async def recovered(_message: str, *, message_history: object = None) -> MagicMock:
        result = MagicMock()
        result.output = "opus reply"
        return result

    level3_handle.run = recovered
    level3_handle.close = MagicMock()
    level3_provider = MagicMock()
    level3_provider.build_agent.return_value = level3_handle

    # acall_with_tier_fallback retries its starting level (4) once — it has
    # no way to know this level was already just attempted outside it — and
    # that retry fails identically before falling back to level 3.
    create_model_patch = MagicMock(
        side_effect=[level4_provider, level4_provider, level3_provider]
    )

    with patch("robotsix_chat.llm.agent.create_model", create_model_patch):
        agent = LlmioChatAgent(model_level=4, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["opus reply"]
    assert create_model_patch.call_args_list == [
        call(level=4),
        call(level=4),
        call(level=3),
    ]
    assert level4_handle.close.call_count == 2
    level3_handle.close.assert_called_once()
    # The fallback attempt reuses the exact same prompt/instruction — the
    # user should get a real answer, not have to re-ask.
    assert level3_provider.build_agent.call_args.kwargs["system_prompt"].startswith(
        "Be helpful."
    )


@pytest.mark.asyncio
async def test_usage_exhausted_fallback_also_failing_raises() -> None:
    """If the fallback tier ALSO fails, the failure propagates.

    Scoped to one promotion — does not keep cascading through every
    remaining tier.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    level4_handle = MagicMock()

    async def exhausted(_message: str, *, message_history: object = None) -> None:
        raise ClaudeSDKUsageExhaustedError("You're out of usage credits")

    level4_handle.run = exhausted
    level4_handle.close = MagicMock()
    level4_provider = MagicMock()
    level4_provider.build_agent.return_value = level4_handle

    level3_handle = MagicMock()

    async def also_boom(_message: str, *, message_history: object = None) -> None:
        raise RuntimeError("opus is also down")

    level3_handle.run = also_boom
    level3_handle.close = MagicMock()
    level3_provider = MagicMock()
    level3_provider.build_agent.return_value = level3_handle

    create_model_patch = MagicMock(
        side_effect=[level4_provider, level4_provider, level3_provider]
    )

    with patch("robotsix_chat.llm.agent.create_model", create_model_patch):
        agent = LlmioChatAgent(model_level=4, instruction="Be helpful.")
        with pytest.raises(RuntimeError, match="opus is also down"):
            _ = [c async for c in agent.stream("hi")]

    assert create_model_patch.call_count == 3


@pytest.mark.asyncio
async def test_non_usage_exhausted_error_not_affected_by_fallback() -> None:
    """A plain non-transient error at the primary level still raises.

    Raises immediately — the fallback path is never entered for it.
    """
    handle = MagicMock()

    async def boom(_message: str, *, message_history: object = None) -> None:
        raise RuntimeError("unrelated failure")

    handle.run = boom
    handle.close = MagicMock()
    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    with (
        patch("robotsix_chat.llm.agent.create_model", create_model_patch),
        patch("robotsix_chat.llm.agent.is_openrouter_transient", return_value=False),
    ):
        agent = LlmioChatAgent(model_level=4, instruction="Be helpful.")
        with pytest.raises(RuntimeError, match="unrelated failure"):
            _ = [c async for c in agent.stream("hi")]

    assert create_model_patch.call_count == 1


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


# ---------------------------------------------------------------------------
# Model level passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_level_passed_to_build_agent() -> None:
    """The constructor's ``model_level`` is forwarded to ``build_agent``."""
    create_model, _ = _patched_create_model("ok")
    provider = create_model.return_value

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        _ = [c async for c in agent.stream("hi")]

    assert create_model.call_args.kwargs["level"] == 3
    assert provider.build_agent.call_args.kwargs["level"] == 3

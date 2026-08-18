"""Tests for :class:`LlmioChatAgent` — the robotsix-llmio-backed chat agent."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.llm.agent import _sdk_session_uuid

# The exact shape the Claude CLI accepts for ``--session-id``.
_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


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
async def test_task_budget_forwarded_to_keyless_level() -> None:
    """``task_budget_tokens`` is forwarded as ``max_tokens`` to a keyless tier."""
    create_model, _ = _patched_create_model()

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(
            model_level=3,
            instruction="Be helpful.",
            task_budget_tokens=30_000,
        )
        _ = [c async for c in agent.stream("hi")]

    create_model.assert_called_once_with(level=3, max_tokens=30_000)


@pytest.mark.asyncio
async def test_task_budget_not_forwarded_to_keyed_level() -> None:
    """``task_budget_tokens`` must not clobber a keyed tier's own max_tokens."""
    create_model, _ = _patched_create_model()

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(
            model_level=1,
            instruction="Be helpful.",
            api_key="k",
            task_budget_tokens=30_000,
        )
        _ = [c async for c in agent.stream("hi")]

    create_model.assert_called_once_with(level=1, api_key="k")


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
    # The recall block must be explicitly fenced off from the live message:
    # similarity-recalled text reads like the current topic, and without an
    # end marker the model can take the whole turn as background.
    assert "# End of recalled memory" in sent
    assert sent.index("Damien prefers Python.") < sent.index("# End of recalled memory")

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

    # The synthetic recall_memory tool_call/tool_result frames (published
    # around memory.recall(), see test_recall_activity_frames_* below) bracket
    # the claudeSDK run's own event — recall() is a no-op with the default
    # NullMemory, so its result frame reports no context found.
    assert sink.published == [
        (
            "sess-abc",
            {
                "type": SSE_ACTIVITY_TYPE,
                "kind": "tool_call",
                "turn": 0,
                "tool_name": "recall_memory",
                "detail": "",
                "is_error": False,
            },
        ),
        (
            "sess-abc",
            {
                "type": SSE_ACTIVITY_TYPE,
                "kind": "tool_result",
                "turn": 0,
                "tool_name": None,
                "detail": "no relevant memory found",
                "is_error": False,
            },
        ),
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
        ),
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
# Synthetic activity frames around memory.recall()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_activity_frames_no_context_found() -> None:
    """recall() returning "" publishes a tool_call/tool_result pair around it."""
    create_model, _ = _patched_create_model("hi there")
    sink = _RecordingEventSink()
    memory = _RecordingMemory(recall="")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(
            model_level=3, instruction="Be helpful.", event_sink=sink, memory=memory
        )
        _ = [c async for c in agent.stream("hi", session_id="sess-abc")]

    from robotsix_chat.chat.events import SSE_ACTIVITY_TYPE

    assert sink.published == [
        (
            "sess-abc",
            {
                "type": SSE_ACTIVITY_TYPE,
                "kind": "tool_call",
                "turn": 0,
                "tool_name": "recall_memory",
                "detail": "",
                "is_error": False,
            },
        ),
        (
            "sess-abc",
            {
                "type": SSE_ACTIVITY_TYPE,
                "kind": "tool_result",
                "turn": 0,
                "tool_name": None,
                "detail": "no relevant memory found",
                "is_error": False,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_recall_activity_frames_context_found() -> None:
    """A non-empty recall() reports how much context was found."""
    create_model, _ = _patched_create_model("hi there")
    sink = _RecordingEventSink()
    memory = _RecordingMemory(recall="prior fact")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(
            model_level=3, instruction="Be helpful.", event_sink=sink, memory=memory
        )
        _ = [c async for c in agent.stream("hi", session_id="sess-abc")]

    result_frame = sink.published[1]
    assert result_frame[1]["kind"] == "tool_result"
    assert result_frame[1]["detail"] == "found 10 chars of prior context"


@pytest.mark.asyncio
async def test_recall_activity_frames_no_event_sink_is_silent() -> None:
    """Without an event_sink, recall() runs normally and nothing is published."""
    create_model, _ = _patched_create_model("hi there")
    memory = _RecordingMemory(recall="prior fact")

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.", memory=memory)
        chunks = [c async for c in agent.stream("hi", session_id="sess-abc")]

    assert chunks == ["hi there"]


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
        patch("robotsix_http.retry.asyncio.sleep", new=AsyncMock()),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["recovered reply"]
    assert provider.build_agent.call_count == 2  # fresh handle per attempt
    assert handle.close.call_count == 2


# Stand-ins for claude_agent_sdk's transport failures.  robotsix_llmio's
# is_claude_sdk_transient matches SDK exception classes by *name* (so it works
# without the SDK installed), so local classes with the same names exercise the
# real predicate end-to-end.
class CLIConnectionError(Exception):
    """Lost the control-protocol connection to the CLI."""


class ClaudeSDKQueryTimeout(Exception):  # noqa: N818 — name must match the upstream SDK class (matched by name)
    """Per-call wall-clock cap tripped on a stalled run."""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boom",
    [
        pytest.param(
            lambda: RuntimeError("Claude Code returned an error result: success"),
            id="degenerate-success-frame",
        ),
        pytest.param(
            lambda: CLIConnectionError("control-protocol connection lost"),
            id="claude-sdk-connection-error",
        ),
        pytest.param(
            lambda: ClaudeSDKQueryTimeout("query timed out"),
            id="claude-sdk-query-timeout",
        ),
    ],
)
async def test_retry_on_claude_sdk_transient_signature(
    boom: Callable[[], BaseException],
) -> None:
    """Each Claude Agent SDK transient signature is retried and recovers.

    Uses the REAL transient predicates (no patching): the degenerate-success
    frame is matched by message and the transport failures by class name
    inside ``is_claude_sdk_transient`` — so this test fails if the OR wiring
    into the chat retry loop is ever dropped.
    """
    call_count = 0

    async def fail_then_pass(
        _message: str, *, message_history: object = None
    ) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise boom()
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
        patch("robotsix_http.retry.asyncio.sleep", new=AsyncMock()),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["recovered reply"]
    assert provider.build_agent.call_count == 2  # fresh handle per retry
    assert handle.close.call_count == 2


def test_chat_turn_transient_excludes_usage_and_auth() -> None:
    """Usage-exhaustion and auth failures are NOT transient at the chat-turn loop.

    Retrying either at this tier just repeats the identical failure; they must
    propagate to the tier-fallback path instead.  Asserted against the REAL
    combined predicate so the exclusion checks inside
    ``is_claude_sdk_transient`` stay wired in.
    """
    from robotsix_llmio.claude_sdk import (
        ClaudeSDKAuthError,
        ClaudeSDKUsageExhaustedError,
    )

    from robotsix_chat.llm.agent import _is_chat_turn_transient

    assert not _is_chat_turn_transient(
        ClaudeSDKUsageExhaustedError("You're out of usage credits")
    )
    assert not _is_chat_turn_transient(
        ClaudeSDKAuthError(
            "Failed to authenticate. API Error: 401 OAuth access token has expired."
        )
    )
    # Sanity anchor: the SDK signatures themselves stay transient.
    assert _is_chat_turn_transient(
        RuntimeError("Claude Code returned an error result: success")
    )
    assert _is_chat_turn_transient(
        CLIConnectionError("control-protocol connection lost")
    )


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
        patch("robotsix_http.retry.asyncio.sleep", new=AsyncMock()),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        with pytest.raises(ValueError, match="persistent transient"):
            _ = [c async for c in agent.stream("hi")]

    # max_retries=2 → 3 total attempts (1 initial + 2 retries)
    assert provider.build_agent.call_count == 3
    assert handle.close.call_count == 3


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
    """Retries are handled by robotsix-http; no per-retry agent log lines."""
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
        patch("robotsix_http.retry.asyncio.sleep", new=AsyncMock()),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        _ = [c async for c in agent.stream("hi")]

    # Three attempts: two transient failures, one success.
    assert provider.build_agent.call_count == 3
    assert handle.close.call_count == 3


@pytest.mark.asyncio
async def test_retry_sleeps_backoff() -> None:
    """Retry backoff is handled by robotsix-http RetryConfig; verify retry count."""
    call_count = 0

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
        patch("robotsix_http.retry.asyncio.sleep", new=sleep_mock),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        _ = [c async for c in agent.stream("hi")]

    # Two transient failures → two sleeps, three total attempts.
    assert sleep_mock.await_count == 2
    assert provider.build_agent.call_count == 3


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


# ---------------------------------------------------------------------------
# Auth-failure tier fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_falls_back_past_every_claude_sdk_tier() -> None:
    """An expired Claude credential is shared by every claudeSDK tier.

    The live outage: levels 4 and 3 both drive the same `claude` CLI against
    the same `.credentials.json`, so a one-step fallback lands on level 3 and
    fails identically. The walk must reach a keyed provider (level 2) for the
    turn to be rescued — and the key must be forwarded there even though the
    primary level is keyless.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKAuthError

    def _dead_credential_provider() -> MagicMock:
        handle = MagicMock()

        async def expired(_message: str, *, message_history: object = None) -> None:
            raise ClaudeSDKAuthError(
                "Failed to authenticate. API Error: 401 OAuth access token has expired."
            )

        handle.run = expired
        handle.close = MagicMock()
        provider = MagicMock()
        provider.build_agent.return_value = handle
        return provider

    level2_handle = MagicMock()

    async def recovered(_message: str, *, message_history: object = None) -> MagicMock:
        result = MagicMock()
        result.output = "openrouter reply"
        return result

    level2_handle.run = recovered
    level2_handle.close = MagicMock()
    level2_provider = MagicMock()
    level2_provider.build_agent.return_value = level2_handle

    # level 4 (primary), level 4 again (the loop's own starting-level retry),
    # level 3 (same dead credential), then level 2 (keyed, works).
    create_model_patch = MagicMock(
        side_effect=[
            _dead_credential_provider(),
            _dead_credential_provider(),
            _dead_credential_provider(),
            level2_provider,
        ]
    )

    with patch("robotsix_chat.llm.agent.create_model", create_model_patch):
        agent = LlmioChatAgent(
            model_level=4,
            instruction="Be helpful.",
            api_key="or-key",  # pragma: allowlist secret
        )
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["openrouter reply"]
    assert create_model_patch.call_args_list == [
        # Keyless claudeSDK tiers must never receive an api_key.
        call(level=4),
        call(level=4),
        call(level=3),
        # The keyed tier must, or the fallback cannot actually serve.
        call(level=2, api_key="or-key"),  # pragma: allowlist secret
    ]


@pytest.mark.asyncio
async def test_usage_exhausted_fallback_stays_one_step() -> None:
    """Widening the reach for auth failures must not widen it for exhaustion.

    Usage exhaustion is scoped to the tier that reported it, so the next tier
    is already a working one — cascading further would burn tiers needlessly.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    def _failing_provider(exc: Exception) -> MagicMock:
        handle = MagicMock()

        async def boom(_message: str, *, message_history: object = None) -> None:
            raise exc

        handle.run = boom
        handle.close = MagicMock()
        provider = MagicMock()
        provider.build_agent.return_value = handle
        return provider

    create_model_patch = MagicMock(
        side_effect=[
            _failing_provider(ClaudeSDKUsageExhaustedError("out of usage credits")),
            _failing_provider(ClaudeSDKUsageExhaustedError("out of usage credits")),
            _failing_provider(RuntimeError("opus is also down")),
        ]
    )

    with patch("robotsix_chat.llm.agent.create_model", create_model_patch):
        agent = LlmioChatAgent(
            model_level=4,
            instruction="Be helpful.",
            api_key="or-key",  # pragma: allowlist secret
        )
        with pytest.raises(RuntimeError, match="opus is also down"):
            _ = [c async for c in agent.stream("hi")]

    # Stops after the single promotion — never reaches level 2 or 1.
    assert create_model_patch.call_count == 3


@pytest.mark.asyncio
async def test_keyless_primary_level_never_receives_the_api_key() -> None:
    """Holding the key must not change what the primary level is given.

    The agent now always carries the configured OpenRouter key so a fallback
    can reach a keyed tier — but a keyless claudeSDK provider rejects an
    api_key, so the primary call must still be made without one.
    """
    handle = MagicMock()

    async def reply(_message: str, *, message_history: object = None) -> MagicMock:
        result = MagicMock()
        result.output = "sdk reply"
        return result

    handle.run = reply
    handle.close = MagicMock()
    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    with patch("robotsix_chat.llm.agent.create_model", create_model_patch):
        agent = LlmioChatAgent(
            model_level=4,
            instruction="Be helpful.",
            api_key="or-key",  # pragma: allowlist secret
        )
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["sdk reply"]
    create_model_patch.assert_called_once_with(level=4)


@pytest.mark.asyncio
async def test_keyed_primary_level_still_receives_the_api_key() -> None:
    """The pre-existing behaviour for a keyed primary level is unchanged."""
    handle = MagicMock()

    async def reply(_message: str, *, message_history: object = None) -> MagicMock:
        result = MagicMock()
        result.output = "openrouter reply"
        return result

    handle.run = reply
    handle.close = MagicMock()
    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    with patch("robotsix_chat.llm.agent.create_model", create_model_patch):
        agent = LlmioChatAgent(
            model_level=2,
            instruction="Be helpful.",
            api_key="or-key",  # pragma: allowlist secret
        )
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["openrouter reply"]
    create_model_patch.assert_called_once_with(
        level=2,
        api_key="or-key",  # pragma: allowlist secret
    )


class TestSdkSessionUuid:
    """The Claude CLI rejects a ``--session-id`` that is not a canonical UUID.

    Chat session ids are ``uuid4().hex`` (no dashes) and autonomous sessions
    use bare names, so the raw id can never be forwarded as-is.
    """

    def test_hex_session_id_becomes_canonical_uuid(self) -> None:
        """A dashless ``uuid4().hex`` id is mapped to a form the CLI accepts."""
        raw = uuid.uuid4().hex  # what ConversationStore generates
        assert _CANONICAL_UUID_RE.match(raw) is None  # precondition: CLI rejects
        assert _CANONICAL_UUID_RE.match(_sdk_session_uuid(raw)) is not None

    def test_non_uuid_session_name_becomes_canonical_uuid(self) -> None:
        """Autonomous sessions are named, not hex, and must map too."""
        assert _CANONICAL_UUID_RE.match(_sdk_session_uuid("default")) is not None

    def test_is_deterministic(self) -> None:
        """Stability is what makes the CLI reuse its session cache."""
        assert _sdk_session_uuid("session-one") == _sdk_session_uuid("session-one")
        assert _sdk_session_uuid("session-one") != _sdk_session_uuid("session-two")


class TestBindSdkSession:
    """``session_id`` creates a session; ``resume`` continues one.

    Setting ``session_id`` on every turn created the transcript on turn 1 and
    then asked the CLI to create the same id again on every later turn, which
    it refuses with "Session ID <uuid> is already in use" — losing the turn,
    and losing all three retries with it because the id is deterministic.
    Observed 2026-08-13 on two chat sessions.
    """

    class _Handle:
        """Minimal stand-in exposing the ``_build_options`` seam."""

        def __init__(self) -> None:
            self._build_options = lambda sp: SimpleNamespace(
                session_id=None, resume=None
            )

    def _opts(self, *, resuming: bool) -> Any:
        from robotsix_chat.llm.agent import _bind_sdk_session

        handle = self._Handle()
        _bind_sdk_session(handle, "chat-session-1", resuming=resuming)
        return handle._build_options("system prompt")

    def test_rebinding_does_not_leave_both_options_set(self) -> None:
        """The flip must replace the binding, not add to it.

        ``_attempt`` re-binds the same handle when the first binding is
        refused.  Wrapping the previous wrapper left its assignment in place,
        so the CLI got ``--session-id`` *and* ``--resume`` and refused
        outright ("--session-id can only be used with --continue or --resume
        if --fork-session is also specified") — the self-heal could never
        succeed.  Observed 2026-08-14 on three autonomous sessions.
        """
        from robotsix_chat.llm.agent import _bind_sdk_session

        handle = self._Handle()
        _bind_sdk_session(handle, "chat-session-1", resuming=True)
        _bind_sdk_session(handle, "chat-session-1", resuming=False)

        opts = handle._build_options("system prompt")
        assert opts.session_id == _sdk_session_uuid("chat-session-1")
        assert opts.resume is None

    def test_rebinding_the_other_way_round_is_also_clean(self) -> None:
        """The flip runs in both directions; neither may leak the other field."""
        from robotsix_chat.llm.agent import _bind_sdk_session

        handle = self._Handle()
        _bind_sdk_session(handle, "chat-session-1", resuming=False)
        _bind_sdk_session(handle, "chat-session-1", resuming=True)

        opts = handle._build_options("system prompt")
        assert opts.resume == _sdk_session_uuid("chat-session-1")
        assert opts.session_id is None

    def test_repeated_rebinding_does_not_stack_wrappers(self) -> None:
        """Guard against unbounded wrapper depth if the flip ever loops."""
        from robotsix_chat.llm.agent import _bind_sdk_session

        handle = self._Handle()
        base = handle._build_options
        for i in range(5):
            _bind_sdk_session(handle, "chat-session-1", resuming=bool(i % 2))
        assert handle._build_options._robotsix_orig_build is base

    def test_first_turn_creates_the_session(self) -> None:
        """Turn 1 has no history, so the session must be created."""
        opts = self._opts(resuming=False)
        assert opts.session_id == _sdk_session_uuid("chat-session-1")
        assert opts.resume is None

    def test_later_turns_resume_it(self) -> None:
        """Any turn with history continues the existing session."""
        opts = self._opts(resuming=True)
        assert opts.resume == _sdk_session_uuid("chat-session-1")
        assert opts.session_id is None

    def test_never_sets_both(self) -> None:
        """Set exactly one of the two options, never both.

        The SDK rejects them together unless ``fork_session`` is set, and a
        fork would defeat the cache reuse this binding exists for.
        """
        for resuming in (True, False):
            opts = self._opts(resuming=resuming)
            assert (opts.session_id is None) != (opts.resume is None)

    def test_missing_seam_is_a_no_op(self) -> None:
        """A handle without ``_build_options`` must not raise."""
        from robotsix_chat.llm.agent import _bind_sdk_session

        _bind_sdk_session(SimpleNamespace(), "chat-session-1", resuming=True)


class TestSessionBindingErrorDetection:
    """Both directions of chat-history / CLI-transcript disagreement."""

    def test_detects_already_in_use(self) -> None:
        """Creating an id whose transcript already exists."""
        from robotsix_chat.llm.agent import _is_session_binding_error

        assert _is_session_binding_error(
            RuntimeError(
                "Error: Session ID bcc0a8f8-b677-5248-b707-3c8f03043c65 "
                "is already in use."
            )
        )

    def test_detects_missing_session_on_resume(self) -> None:
        """Resuming an id the CLI no longer has."""
        from robotsix_chat.llm.agent import _is_session_binding_error

        assert _is_session_binding_error(
            RuntimeError("No conversation found with session ID abc")
        )

    def test_ignores_unrelated_errors(self) -> None:
        """Only binding errors may flip the mode — everything else propagates."""
        from robotsix_chat.llm.agent import _is_session_binding_error

        assert not _is_session_binding_error(RuntimeError("rate limit exceeded"))
        assert not _is_session_binding_error(RuntimeError("connection reset"))


class TestSdkSessionStateAcrossAttempts:
    """A second attempt in the same turn must resume, not re-create.

    The SDK session id is derived deterministically from the chat session, so
    every attempt in a turn targets the same CLI session.  Binding from
    ``bool(message_history)`` per attempt cannot see that the previous attempt
    already created it: on a NEW chat the history stays empty all turn, so the
    tier fallback asked the CLI to create an id the primary attempt had just
    created and got "Session ID <uuid> is already in use".  The fallback path
    had no flip-once self-heal, so the turn died — every new chat broke while
    the configured tier was out of credits (observed 2026-08-17).
    """

    class _Handle:
        """Records the options each run was bound with."""

        def __init__(self, fail_with: Exception | None = None) -> None:
            self._build_options = lambda sp: SimpleNamespace(
                session_id=None, resume=None
            )
            self.bindings: list[tuple[Any, Any]] = []
            self._fail_with = fail_with

        async def run(self) -> str:
            opts = self._build_options("system prompt")
            self.bindings.append((opts.session_id, opts.resume))
            if self._fail_with is not None:
                exc, self._fail_with = self._fail_with, None
                raise exc
            return "ok"

    @pytest.mark.asyncio
    async def test_new_chat_second_attempt_resumes(self) -> None:
        """The fallback attempt resumes the session the primary created."""
        from robotsix_chat.llm.agent import (
            _run_bound_to_sdk_session,
            _SdkSessionState,
        )

        # A brand-new chat: no history for the whole turn.
        state = _SdkSessionState(exists=False)

        primary = self._Handle()
        await _run_bound_to_sdk_session(primary, "chat-1", state, primary.run)
        # First attempt creates: --session-id set, --resume unset.
        assert primary.bindings[0][0] is not None
        assert primary.bindings[0][1] is None

        fallback = self._Handle()
        await _run_bound_to_sdk_session(fallback, "chat-1", state, fallback.run)
        # Second attempt in the SAME turn must resume, despite empty history.
        assert fallback.bindings[0][0] is None, "fallback must not re-create the id"
        assert fallback.bindings[0][1] is not None, "fallback must resume"

    @pytest.mark.asyncio
    async def test_primary_failure_still_marks_the_session_created(self) -> None:
        """An exhausted tier still leaves the transcript behind.

        This is the real-world shape: the primary attempt creates the session
        and *then* dies on exhausted credits, so the fallback must resume.
        """
        from robotsix_chat.llm.agent import (
            _run_bound_to_sdk_session,
            _SdkSessionState,
        )

        state = _SdkSessionState(exists=False)
        primary = self._Handle(fail_with=RuntimeError("out of usage credits"))
        with pytest.raises(RuntimeError):
            await _run_bound_to_sdk_session(primary, "chat-1", state, primary.run)
        assert state.exists is True

        fallback = self._Handle()
        await _run_bound_to_sdk_session(fallback, "chat-1", state, fallback.run)
        assert fallback.bindings[0][1] is not None, "fallback must resume"

    @pytest.mark.asyncio
    async def test_fallback_path_self_heals_on_binding_error(self) -> None:
        """A binding error flips the binding and re-runs — on any path.

        The fallback path previously had no self-heal at all, so a stale
        state guess was unrecoverable.
        """
        from robotsix_chat.llm.agent import (
            _run_bound_to_sdk_session,
            _SdkSessionState,
        )

        # State says "not created", but the CLI disagrees.
        state = _SdkSessionState(exists=False)
        handle = self._Handle(
            fail_with=RuntimeError("Error: Session ID abc is already in use.")
        )
        result = await _run_bound_to_sdk_session(handle, "chat-1", state, handle.run)

        assert result == "ok"
        assert len(handle.bindings) == 2, "should have re-run once"
        # First tried to create, then flipped to resume.
        assert handle.bindings[0][0] is not None
        assert handle.bindings[1][1] is not None
        assert state.exists is True

    @pytest.mark.asyncio
    async def test_existing_chat_resumes_from_the_start(self) -> None:
        """A chat with history resumes on the very first attempt."""
        from robotsix_chat.llm.agent import (
            _run_bound_to_sdk_session,
            _SdkSessionState,
        )

        state = _SdkSessionState(exists=True)
        handle = self._Handle()
        await _run_bound_to_sdk_session(handle, "chat-1", state, handle.run)
        assert handle.bindings[0][0] is None
        assert handle.bindings[0][1] is not None

    @pytest.mark.asyncio
    async def test_no_session_id_runs_unbound(self) -> None:
        """Without a chat session there is nothing to bind."""
        from robotsix_chat.llm.agent import (
            _run_bound_to_sdk_session,
            _SdkSessionState,
        )

        state = _SdkSessionState(exists=False)
        handle = self._Handle()
        assert await _run_bound_to_sdk_session(handle, None, state, handle.run) == "ok"
        assert handle.bindings == [(None, None)]

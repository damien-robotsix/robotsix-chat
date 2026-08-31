"""Tests for :class:`LlmioChatAgent` — the robotsix-llmio-backed chat agent."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from robotsix_chat.llm import LlmioChatAgent


@pytest.fixture(autouse=True)
def _reset_tier_cooldown() -> object:
    """Isolate llmio's process-global tier-cooldown state between tests.

    ``robotsix_llmio.core.tier_fallback`` records terminal failures (usage
    exhaustion, auth) in a module-global ``ModelHealthTracker`` and, once a
    model crosses the consecutive-failure threshold, skips it *without a call*.
    Several tests here deliberately drive fallbacks to exhaustion, so without a
    reset the accumulated failures leak across tests and later tests see a tier
    skipped — shifting their ``create_model`` side-effect sequence and failing
    spuriously. Reset the tracker around each test so every test starts with all
    tiers healthy.
    """
    from robotsix_llmio.core.cooldown import reset_health_tracker

    reset_health_tracker()
    yield
    reset_health_tracker()


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
            model_level=4,
            instruction="Be helpful.",
            task_budget_tokens=30_000,
        )
        _ = [c async for c in agent.stream("hi")]

    create_model.assert_called_once_with(level=4, max_tokens=30_000)


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
    # no way to know this level was already just attempted outside it. That
    # retry fails identically, arming llmio's claudeSDK FAMILY latch (usage
    # exhaustion cools every Claude tier at once), so the walk skips level 5
    # without a call and lands directly on the keyed level 3.
    create_model_patch = MagicMock(
        side_effect=[level4_provider, level4_provider, level3_provider]
    )

    from robotsix_llmio.core import reset_health_tracker

    reset_health_tracker()
    try:
        with patch("robotsix_chat.llm.agent.create_model", create_model_patch):
            agent = LlmioChatAgent(model_level=4, instruction="Be helpful.")
            chunks = [c async for c in agent.stream("hi")]
    finally:
        reset_health_tracker()

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
async def test_usage_exhausted_fallback_keeps_conversation_context() -> None:
    """A usage-exhausted Claude turn falls back WITH the prior turns intact.

    Regression for the context-wipe bug: the keyed fallback tier (level 3)
    does not share the Claude SDK resume session, so it must receive the
    conversation as explicit ``message_history`` AND a system note telling it
    it is mid-conversation, so it does not reply "I don't have full context on
    what 'it' refers to" to a terse follow-up.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    # The keyless Claude tiers (4, 5) all draw on the one exhausted
    # subscription, so the walk passes through them and lands on keyed level 3.
    claude_handle = MagicMock()

    async def exhausted(_message: str, *, message_history: object = None) -> None:
        raise ClaudeSDKUsageExhaustedError("You're out of usage credits")

    claude_handle.run = exhausted
    claude_handle.close = MagicMock()
    claude_provider = MagicMock()
    claude_provider.build_agent.return_value = claude_handle

    seen: dict[str, object] = {}
    level3_handle = MagicMock()

    async def recovered(_message: str, *, message_history: object = None) -> MagicMock:
        seen["message_history"] = message_history
        result = MagicMock()
        result.output = "opus reply about the browser ticket"
        return result

    level3_handle.run = recovered
    level3_handle.close = MagicMock()
    level3_provider = MagicMock()
    level3_provider.build_agent.return_value = level3_handle

    create_model_patch = MagicMock(
        side_effect=lambda *, level, **_kw: (
            level3_provider if level == 3 else claude_provider
        )
    )

    history = [
        ("tell me about robotsix-browser", "It is a headless browser component."),
        ("ok, file a ticket and watch", "Filed the ticket; watching it now."),
    ]

    with patch("robotsix_chat.llm.agent.create_model", create_model_patch):
        agent = LlmioChatAgent(model_level=4, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("and?", history=history)]

    assert chunks == ["opus reply about the browser ticket"]
    # The keyed fallback tier received the prior turns explicitly (2 turns ->
    # a ModelRequest/ModelResponse pair each).
    message_history = seen["message_history"]
    assert message_history is not None
    assert len(message_history) == 4  # type: ignore[arg-type]
    # And its system prompt carries the continuation note so it does not ask
    # the user to restate the topic.
    fallback_prompt = level3_provider.build_agent.call_args.kwargs["system_prompt"]
    assert fallback_prompt.startswith("Be helpful.")
    assert "continuing an ongoing conversation" in fallback_prompt.lower()


@pytest.mark.asyncio
async def test_usage_exhausted_fallback_also_failing_raises() -> None:
    """If every fallback tier ALSO fails, the failure propagates.

    The walk may visit every other tier (the subscription cap takes all
    Claude tiers down together, so it must be able to reach a keyed one);
    when they all fail, the last failure surfaces.
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
        side_effect=lambda *, level, **_kw: (
            level4_provider if level == 4 else level3_provider
        )
    )

    with patch("robotsix_chat.llm.agent.create_model", create_model_patch):
        agent = LlmioChatAgent(model_level=4, instruction="Be helpful.")
        with pytest.raises(RuntimeError, match="opus is also down"):
            _ = [c async for c in agent.stream("hi")]

    # 4 (retry), then every other tier — all failing — before the error surfaces.
    assert create_model_patch.call_count >= 3


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
    # level 5 (same dead credential), then level 3 (keyed, works).
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
        call(level=5),
        # The keyed tier must, or the fallback cannot actually serve.
        call(level=3, api_key="or-key"),  # pragma: allowlist secret
    ]


@pytest.mark.asyncio
async def test_usage_exhausted_fallback_walks_every_remaining_tier() -> None:
    """Every remaining tier is walked before the last failure surfaces.

    Exhaustion is per-subscription under the five-tier map, so the walk may
    reach every other tier (it must be able to land on keyed level 3).
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
        side_effect=lambda *, level, **_kw: (
            _failing_provider(ClaudeSDKUsageExhaustedError("out of usage credits"))
            if level == 4
            else _failing_provider(RuntimeError("opus is also down"))
        )
    )

    with patch("robotsix_chat.llm.agent.create_model", create_model_patch):
        agent = LlmioChatAgent(
            model_level=4,
            instruction="Be helpful.",
            api_key="or-key",  # pragma: allowlist secret
        )
        with pytest.raises(RuntimeError, match="opus is also down"):
            _ = [c async for c in agent.stream("hi")]

    # Walks past the sibling Claude tier down to the keyed ones.
    assert create_model_patch.call_count >= 3
    assert 3 in [c.kwargs["level"] for c in create_model_patch.call_args_list]


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
            model_level=3,
            instruction="Be helpful.",
            api_key="or-key",  # pragma: allowlist secret
        )
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["openrouter reply"]
    create_model_patch.assert_called_once_with(
        level=3,
        api_key="or-key",  # pragma: allowlist secret
    )


# ---------------------------------------------------------------------------
# Stateless Claude turns: no CLI session resume, one fresh memory block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_does_not_bind_a_cli_session_and_carries_history_in_prompt() -> None:
    """Each turn is stateless: no CLI session binding, history in the prompt.

    The handle's option builder is left untouched (no ``resume``/``session_id``
    binding) and the conversation travels as ``message_history``. Resuming a
    CLI session made the transcript keep every earlier prompt — N history
    copies and N stale memory blocks by turn N.
    """
    create_model, handle = _patched_create_model("ok")
    sentinel_builder = MagicMock(name="orig_build_options")
    handle._build_options = sentinel_builder
    seen: dict[str, object] = {}

    async def fake_run(prompt, *, message_history=None):
        seen["prompt"] = prompt
        seen["history"] = message_history
        result = MagicMock()
        result.output = "ok"
        return result

    handle.run = fake_run

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=4, instruction="Be helpful.")
        _ = [
            c
            async for c in agent.stream(
                "third question",
                history=[("q1", "a1"), ("q2", "a2")],
                session_id="chat-session-1",
            )
        ]

    assert handle._build_options is sentinel_builder  # never wrapped
    hist = seen["history"]
    assert isinstance(hist, list) and len(hist) == 4  # 2 turns × (request, response)
    assert seen["prompt"] == "third question"


@pytest.mark.asyncio
async def test_recalled_memory_is_prepended_once_to_the_current_turn_only() -> None:
    """Recall output is attached once, to the newest user message only.

    Not the system prompt, not the history — so each turn carries exactly one,
    fresh block.
    """
    from robotsix_chat.llm.agent import _MEMORY_PROMPT_FOOTER, _MEMORY_PROMPT_HEADER

    create_model, handle = _patched_create_model("ok")
    seen: dict[str, object] = {}

    async def fake_run(prompt, *, message_history=None):
        seen["prompt"] = prompt
        seen["history"] = message_history
        result = MagicMock()
        result.output = "ok"
        return result

    handle.run = fake_run
    memory = MagicMock()

    async def fake_recall(message, *, session_id=None):
        return "recalled fact about " + message

    memory.recall = fake_recall

    async def fake_remember(*args, **kwargs):
        return None

    memory.remember = fake_remember

    with patch("robotsix_chat.llm.agent.create_model", create_model):
        agent = LlmioChatAgent(model_level=4, instruction="Be helpful.", memory=memory)
        _ = [
            c
            async for c in agent.stream(
                "second question",
                history=[("first question", "first answer")],
                session_id="chat-session-1",
            )
        ]

    prompt = seen["prompt"]
    assert isinstance(prompt, str)
    assert prompt.count(_MEMORY_PROMPT_HEADER) == 1
    assert prompt.endswith(f"{_MEMORY_PROMPT_FOOTER}\nsecond question")
    assert "recalled fact about second question" in prompt
    # History is passed structurally and stays memory-free.
    hist = seen["history"]
    assert isinstance(hist, list)
    assert all("recalled" not in str(getattr(m, "parts", "")) for m in hist)


@pytest.mark.asyncio
async def test_usage_exhausted_walks_past_sibling_claude_tier_to_keyed_level() -> None:
    """An exhausted level 5 walks past level 4 down to keyed level 3 (mimo).

    Under the five-tier map levels 5 (fable) and 4 (opus) share the subscription
    cap, so the walk must not stop at 4 with "fallback depth exhausted" (the
    2026-08-29 'internal error').
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    seen: list[int] = []

    def _provider(level: int) -> MagicMock:
        handle = MagicMock()
        if level in (5, 4):

            async def exhausted(_m: str, *, message_history: object = None) -> None:
                raise ClaudeSDKUsageExhaustedError(
                    "You've hit your session limit · resets 1am (UTC)"
                )

            handle.run = exhausted
        else:

            async def recovered(
                _m: str, *, message_history: object = None
            ) -> MagicMock:
                result = MagicMock()
                result.output = "mimo reply"
                result.all_messages.return_value = []
                result.usage.return_value = None
                return result

            handle.run = recovered
        handle.close = MagicMock()
        provider = MagicMock()
        provider.build_agent.return_value = handle
        return provider

    def _create_model(*, level: int, **_kw: object) -> MagicMock:
        seen.append(level)
        return _provider(level)

    with patch("robotsix_chat.llm.agent.create_model", side_effect=_create_model):
        agent = LlmioChatAgent(model_level=5, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["mimo reply"]
    assert seen[-1] == 3, seen


# ---------------------------------------------------------------------------
# Per-turn actions log collection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_events_feed_the_actions_collector() -> None:
    """tool_call / tool_result events land as paired entries in the collector.

    Works without an event sink: the collector is the caller's, the sink is
    the UI's, and either alone is reason enough to wire the callback.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKActivityEvent
    from robotsix_llmio.claude_sdk._stream import _current_on_event

    from robotsix_chat.chat.actions import collect_actions

    handle = MagicMock()

    async def fake_run(message: str, *, message_history: object = None) -> MagicMock:
        on_event = _current_on_event.get()
        assert on_event is not None
        on_event(
            ClaudeSDKActivityEvent(
                kind="tool_call",
                turn=1,
                tool_name="create_ticket",
                detail='{"title": "Volume file-write"}',
            )
        )
        on_event(
            ClaudeSDKActivityEvent(
                kind="tool_result", turn=1, detail='{"ticket_id": "T-04d8"}'
            )
        )
        on_event(
            ClaudeSDKActivityEvent(
                kind="tool_call", turn=2, tool_name="merge_pr", detail='{"pr": 812}'
            )
        )
        on_event(
            ClaudeSDKActivityEvent(
                kind="tool_result", turn=2, detail="405 not mergeable", is_error=True
            )
        )
        result = MagicMock()
        result.output = "done"
        result.all_messages = MagicMock(return_value=[])
        return result

    handle.run = fake_run
    handle.close = MagicMock()
    provider = MagicMock()
    provider.build_agent.return_value = handle

    with patch(
        "robotsix_chat.llm.agent.create_model", MagicMock(return_value=provider)
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        with collect_actions() as actions:
            _ = [c async for c in agent.stream("file it", session_id="sess-1")]

    assert len(actions) == 2
    assert actions[0].startswith("create_ticket(") and "T-04d8" in actions[0]
    assert (
        actions[1].startswith("merge_pr(") and "ERROR 405 not mergeable" in actions[1]
    )
    # all_messages() was consulted but the event-fed log was kept as is.
    assert handle.run is fake_run


@pytest.mark.asyncio
async def test_result_messages_fill_collector_when_no_events_fired() -> None:
    """Keyed tiers report tool calls only via ``result.all_messages()``."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
    )

    from robotsix_chat.chat.actions import collect_actions

    handle = MagicMock()

    async def fake_run(message: str, *, message_history: object = None) -> MagicMock:
        result = MagicMock()
        result.output = "done"
        result.all_messages = MagicMock(
            return_value=[
                ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="spawn_subsession",
                            args={"kind": "task"},
                            tool_call_id="c1",
                        )
                    ]
                ),
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name="spawn_subsession",
                            content="sub-42",
                            tool_call_id="c1",
                        )
                    ]
                ),
            ]
        )
        return result

    handle.run = fake_run
    handle.close = MagicMock()
    provider = MagicMock()
    provider.build_agent.return_value = handle

    with patch(
        "robotsix_chat.llm.agent.create_model", MagicMock(return_value=provider)
    ):
        agent = LlmioChatAgent(model_level=1, api_key="k", instruction="Be helpful.")
        with collect_actions() as actions:
            _ = [c async for c in agent.stream("go", session_id="sess-1")]
        # Without a collector nothing is recorded and nothing breaks.
        _ = [c async for c in agent.stream("go again", session_id="sess-1")]

    assert actions == ['spawn_subsession({"kind": "task"}) -> sub-42']


@pytest.mark.asyncio
async def test_keyed_fallback_caps_oversized_history() -> None:
    """A keyed fallback tier caps the history to fit its token window.

    When the conversation history is too large for the fallback tier's context
    window (e.g. level-3 mimo at 65 536 tokens), the oldest turns are dropped
    and the first surviving user message is prefixed with an omission note.
    The most recent turns are always kept.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    # Build an oversized history: 60 turns, each ~4 000 chars (~1 000 tokens).
    # Total ~60 000 tokens — exceeds level-3's 70 % budget of ~45 875 tokens.
    big_turns: list[tuple[str, str]] = []
    for i in range(60):
        user = f"Turn {i}: " + "x" * 3960
        asst = f"Reply {i}: " + "y" * 3960
        big_turns.append((user, asst))

    # The Claude tier (level 4) is exhausted; the walk lands on keyed level 3.
    claude_handle = MagicMock()

    async def exhausted(_message: str, *, message_history: object = None) -> None:
        raise ClaudeSDKUsageExhaustedError("You're out of usage credits")

    claude_handle.run = exhausted
    claude_handle.close = MagicMock()
    claude_provider = MagicMock()
    claude_provider.build_agent.return_value = claude_handle

    seen: dict[str, object] = {}
    level3_handle = MagicMock()

    async def recovered(_message: str, *, message_history: object = None) -> MagicMock:
        seen["message_history"] = message_history
        result = MagicMock()
        result.output = "capped reply"
        return result

    level3_handle.run = recovered
    level3_handle.close = MagicMock()
    level3_provider = MagicMock()
    level3_provider.build_agent.return_value = level3_handle

    create_model_patch = MagicMock(
        side_effect=lambda *, level, **_kw: (
            level3_provider if level == 3 else claude_provider
        )
    )

    with patch("robotsix_chat.llm.agent.create_model", create_model_patch):
        agent = LlmioChatAgent(model_level=4, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("latest", history=big_turns)]

    assert chunks == ["capped reply"]
    # The fallback received a capped history — fewer messages than the
    # original 60 turns (120 pydantic-ai messages).
    message_history = seen["message_history"]
    assert message_history is not None
    assert len(message_history) < 120  # type: ignore[arg-type]
    # The newest turns are preserved: the last turn's user message ("Turn 59")
    # must survive.
    last_user_part = message_history[-2]  # type: ignore[index]
    assert "Turn 59" in str(last_user_part)
    # The first surviving user message carries the omission note.
    first_user_part = message_history[0]  # type: ignore[index]
    first_text = str(first_user_part)
    assert "Older conversation turns were omitted" in first_text

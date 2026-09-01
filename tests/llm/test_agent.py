"""Tests for :class:`LlmioChatAgent` — the robotsix-llmio-backed chat agent."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robotsix_chat.llm import LlmioChatAgent


@pytest.fixture(autouse=True)
def _reset_failover_tracker() -> object:
    """Isolate each test from llmio's process-wide provider-failover tracker.

    Several tests here deliberately drive default-slot failures to arm the
    failover window; without a reset the armed window leaks across tests and
    later tests resolve the fallback slot — shifting their provider-factory
    side-effect sequence and failing spuriously.
    """
    from robotsix_llmio.core.failover import reset_failover_tracker

    reset_failover_tracker()
    yield
    reset_failover_tracker()


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

    async def fake_run(
        message: str, *, message_history: object = None, **run_kwargs: object
    ) -> MagicMock:
        handle.run_calls.append(
            {
                "message": message,
                "message_history": message_history,
                "run_kwargs": dict(run_kwargs),
            }
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

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["Hello world!"]
    handle.close.assert_called_once()  # handle is always closed


@pytest.mark.asyncio
async def test_default_slot_gets_no_api_key() -> None:
    """The keyless default (claudeSDK) slot never receives an api_key."""
    get_provider, _ = _patched_create_model()

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", get_provider):
        agent = LlmioChatAgent(
            model_level=3, instruction="Be helpful.", api_key="sk-or-test"
        )
        _ = [c async for c in agent.stream("hi")]

    get_provider.assert_called_once_with("claudeSDK-claude-fable-5")


@pytest.mark.asyncio
async def test_keyed_slot_forwards_api_key() -> None:
    """With failover armed, the keyed (OpenRouter) slot gets the api_key.

    Plus the slot's own routing kwargs and per-response max_tokens.
    """
    from robotsix_llmio.config.tier import FALLBACK_LEVEL1
    from robotsix_llmio.core.failover import get_failover_tracker
    from robotsix_llmio.exceptions import ProviderExhaustedError

    get_failover_tracker().record_failure(
        "default", ProviderExhaustedError("weekly cap")
    )
    get_provider, _ = _patched_create_model()
    provider = get_provider.return_value

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", get_provider):
        agent = LlmioChatAgent(
            model_level=1,
            instruction="Be helpful.",
            api_key="sk-or-test",  # pragma: allowlist secret
        )
        _ = [c async for c in agent.stream("hi")]

    get_provider.assert_called_once_with(
        FALLBACK_LEVEL1.model,
        **FALLBACK_LEVEL1.provider_kwargs,
        max_tokens=FALLBACK_LEVEL1.max_tokens,
        api_key="sk-or-test",  # pragma: allowlist secret
    )
    kwargs = provider.build_agent.call_args.kwargs
    assert kwargs["level"] == 1
    assert kwargs["model"] == FALLBACK_LEVEL1.model_name
    assert kwargs["tools"] is None
    # The chat must never expose the SDK's built-in tools.
    assert kwargs["builtin_tools"] is False
    # The instruction is preserved (with the no-system-access guard appended).
    assert kwargs["system_prompt"].startswith("Be helpful.")


@pytest.mark.asyncio
async def test_task_budget_forwarded_to_keyless_slot() -> None:
    """``task_budget_tokens`` becomes ``max_tokens`` on the keyless slot.

    llmio maps it onto the SDK's advisory task_budget.
    """
    get_provider, _ = _patched_create_model()

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", get_provider):
        agent = LlmioChatAgent(
            model_level=2,
            instruction="Be helpful.",
            task_budget_tokens=30_000,
        )
        _ = [c async for c in agent.stream("hi")]

    get_provider.assert_called_once_with("claudeSDK-opus", max_tokens=30_000)


@pytest.mark.asyncio
async def test_task_budget_not_forwarded_to_keyed_slot() -> None:
    """``task_budget_tokens`` must not clobber a keyed slot's own max_tokens."""
    from robotsix_llmio.config.tier import FALLBACK_LEVEL1
    from robotsix_llmio.core.failover import get_failover_tracker
    from robotsix_llmio.exceptions import ProviderExhaustedError

    get_failover_tracker().record_failure(
        "default", ProviderExhaustedError("weekly cap")
    )
    get_provider, _ = _patched_create_model()

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", get_provider):
        agent = LlmioChatAgent(
            model_level=1,
            instruction="Be helpful.",
            api_key="k",
            task_budget_tokens=30_000,
        )
        _ = [c async for c in agent.stream("hi")]

    get_provider.assert_called_once_with(
        FALLBACK_LEVEL1.model,
        **FALLBACK_LEVEL1.provider_kwargs,
        max_tokens=FALLBACK_LEVEL1.max_tokens,
        api_key="k",
    )


@pytest.mark.asyncio
async def test_empty_output_yields_nothing() -> None:
    """An empty reply yields no chunks (and still closes the handle)."""
    create_model, handle = _patched_create_model("")

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
        agent = LlmioChatAgent(model_level=1, instruction="Be helpful.", api_key="k")
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == []
    handle.close.assert_called_once()


@pytest.mark.asyncio
async def test_handle_closed_on_error() -> None:
    """If the underlying run raises, the handle is still closed."""
    handle = MagicMock()

    async def boom(
        message: str, *, message_history: object = None, **run_kwargs: object
    ) -> None:
        raise RuntimeError("backend exploded")

    handle.run = boom
    handle.close = MagicMock()
    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model = MagicMock(return_value=provider)

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
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

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
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

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
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

    async def fake_run(
        message: str, *, message_history: object = None, **run_kwargs: object
    ) -> MagicMock:
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

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
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

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
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

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
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

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
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

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
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

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
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

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
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
async def test_keyed_slot_runs_with_request_limit() -> None:
    """Keyed (OpenRouter) slots raise pydantic-ai's default request_limit of 50.

    Tool-heavy turns burn one request per tool round-trip and legitimately
    exceed 50; without the override they die mid-stream with
    UsageLimitExceeded (the 2026-09-01 'internal error' under the weekly
    Claude cap).
    """
    from pydantic_ai.usage import UsageLimits
    from robotsix_llmio.core.failover import get_failover_tracker
    from robotsix_llmio.exceptions import ProviderExhaustedError

    get_failover_tracker().record_failure(
        "default", ProviderExhaustedError("weekly cap")
    )
    get_provider, handle = _patched_create_model("reply")

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", get_provider):
        agent = LlmioChatAgent(model_level=2, instruction="Be helpful.", api_key="k")
        _ = [c async for c in agent.stream("hi")]

    limits = handle.run_calls[0]["run_kwargs"].get("usage_limits")
    assert isinstance(limits, UsageLimits)
    assert limits.request_limit == 200


@pytest.mark.asyncio
async def test_keyless_slot_runs_without_usage_limits() -> None:
    """The Claude SDK default slot gets no usage_limits kwarg.

    The SDK tool path warns-and-drops run kwargs it cannot honor, and the
    CLI runs the agent loop internally so the cap never applies.
    """
    get_provider, handle = _patched_create_model("reply")

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", get_provider):
        agent = LlmioChatAgent(model_level=2, instruction="Be helpful.")
        _ = [c async for c in agent.stream("hi")]

    assert "usage_limits" not in handle.run_calls[0]["run_kwargs"]


@pytest.mark.asyncio
async def test_no_history_passes_none() -> None:
    """With no prior turns, message_history is None (a plain single query)."""
    create_model, handle = _patched_create_model("reply")

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
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
        patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model),
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
        _message: str, *, message_history: object = None, **run_kwargs: object
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
        patch(
            "robotsix_chat.llm.agent.get_provider_for_identifier", create_model_patch
        ),
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
        _message: str, *, message_history: object = None, **run_kwargs: object
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
        patch(
            "robotsix_chat.llm.agent.get_provider_for_identifier", create_model_patch
        ),
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

    async def boom(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> None:
        raise RuntimeError("backend exploded")

    handle.run = boom
    handle.close = MagicMock()

    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    with (
        patch(
            "robotsix_chat.llm.agent.get_provider_for_identifier", create_model_patch
        ),
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

    async def always_boom(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> None:
        raise ValueError("persistent transient")

    handle.run = always_boom
    handle.close = MagicMock()

    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    with (
        patch(
            "robotsix_chat.llm.agent.get_provider_for_identifier", create_model_patch
        ),
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


def _slot_handles(
    default_run: object, fallback_run: object
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build a provider factory routed by slot (claudeSDK-* vs openrouter-*).

    Returns ``(get_provider_patch, default_provider, fallback_provider)``;
    each provider mock's ``build_agent`` returns a handle whose ``run`` is
    the given coroutine function, and the handle is also exposed as
    ``provider.handle`` for close/call assertions.
    """
    default_handle = MagicMock()
    default_handle.run = default_run
    default_handle.close = MagicMock()
    default_provider = MagicMock()
    default_provider.build_agent.return_value = default_handle
    default_provider.handle = default_handle

    fallback_handle = MagicMock()
    fallback_handle.run = fallback_run
    fallback_handle.close = MagicMock()
    fallback_provider = MagicMock()
    fallback_provider.build_agent.return_value = fallback_handle
    fallback_provider.handle = fallback_handle

    def _route(identifier: str, **_kw: object) -> MagicMock:
        if str(identifier).startswith("claudeSDK"):
            return default_provider
        return fallback_provider

    return MagicMock(side_effect=_route), default_provider, fallback_provider


@pytest.mark.asyncio
async def test_usage_exhausted_fails_over_to_openrouter_same_level() -> None:
    """Claude exhaustion fails the turn over to the OpenRouter slot.

    The SAME capability level is retried on the fallback slot for the same
    turn — levels never change — instead of surfacing the raw error text.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    async def exhausted(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> None:
        raise ClaudeSDKUsageExhaustedError("You're out of usage credits")

    async def recovered(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> MagicMock:
        result = MagicMock()
        result.output = "deepseek reply"
        return result

    get_provider, default_provider, fallback_provider = _slot_handles(
        exhausted, recovered
    )

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", get_provider):
        agent = LlmioChatAgent(model_level=2, instruction="Be helpful.", api_key="k")
        chunks = [c async for c in agent.stream("hi")]

        assert chunks == ["deepseek reply"]
        default_provider.handle.close.assert_called_once()
        fallback_provider.handle.close.assert_called_once()
        # Both attempts ran at the SAME level.
        identifiers = [c.args[0] for c in get_provider.call_args_list]
        assert identifiers[0].startswith("claudeSDK")
        assert identifiers[1].startswith("openrouter")

        # The next turn goes straight to the fallback slot: exhaustion armed
        # the failover window, so the doomed default attempt is skipped.
        get_provider.reset_mock()
        chunks = [c async for c in agent.stream("hi again")]
        assert chunks == ["deepseek reply"]
        assert len(get_provider.call_args_list) == 1
        assert get_provider.call_args_list[0].args[0].startswith("openrouter")


@pytest.mark.asyncio
async def test_failover_keeps_conversation_context() -> None:
    """A failed-over turn reaches the keyed slot WITH the prior turns intact.

    Regression for the context-wipe bug: the keyed (OpenRouter) slot does not
    share the Claude SDK resume session, so it must receive the conversation
    as explicit ``message_history`` AND a system note telling it it is
    mid-conversation, so it does not reply "I don't have full context on what
    'it' refers to" to a terse follow-up.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    async def exhausted(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> None:
        raise ClaudeSDKUsageExhaustedError("You're out of usage credits")

    seen: dict[str, object] = {}

    async def recovered(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> MagicMock:
        seen["message_history"] = message_history
        result = MagicMock()
        result.output = "reply about the browser ticket"
        return result

    get_provider, _, fallback_provider = _slot_handles(exhausted, recovered)

    history = [
        ("tell me about robotsix-browser", "It is a headless browser component."),
        ("ok, file a ticket and watch", "Filed the ticket; watching it now."),
    ]

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", get_provider):
        agent = LlmioChatAgent(model_level=2, instruction="Be helpful.", api_key="k")
        chunks = [c async for c in agent.stream("and?", history=history)]

    assert chunks == ["reply about the browser ticket"]
    # The keyed slot received the prior turns explicitly (2 turns -> a
    # ModelRequest/ModelResponse pair each).
    message_history = seen["message_history"]
    assert message_history is not None
    assert len(message_history) == 4  # type: ignore[arg-type]
    # And its system prompt carries the continuation note so it does not ask
    # the user to restate the topic. (The handle's build_agent mock parent
    # holds the call.)
    fallback_prompt = fallback_provider.build_agent.call_args.kwargs["system_prompt"]
    assert fallback_prompt.startswith("Be helpful.")
    assert "continuing an ongoing conversation" in fallback_prompt.lower()


@pytest.mark.asyncio
async def test_both_slots_failing_raises() -> None:
    """If the fallback slot ALSO fails, the failure propagates."""
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    async def exhausted(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> None:
        raise ClaudeSDKUsageExhaustedError("You're out of usage credits")

    async def also_boom(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> None:
        raise RuntimeError("deepseek is also down")

    get_provider, _, _ = _slot_handles(exhausted, also_boom)

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", get_provider):
        agent = LlmioChatAgent(model_level=2, instruction="Be helpful.", api_key="k")
        with pytest.raises(RuntimeError, match="deepseek is also down"):
            _ = [c async for c in agent.stream("hi")]

    # Exactly both slots were attempted before the error surfaced.
    identifiers = [c.args[0] for c in get_provider.call_args_list]
    assert identifiers[0].startswith("claudeSDK")
    assert identifiers[-1].startswith("openrouter")


@pytest.mark.asyncio
async def test_non_usage_exhausted_error_not_affected_by_fallback() -> None:
    """A plain non-transient error at the primary level still raises.

    Raises immediately — the fallback path is never entered for it.
    """
    handle = MagicMock()

    async def boom(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> None:
        raise RuntimeError("unrelated failure")

    handle.run = boom
    handle.close = MagicMock()
    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    with (
        patch(
            "robotsix_chat.llm.agent.get_provider_for_identifier", create_model_patch
        ),
        patch("robotsix_chat.llm.agent.is_openrouter_transient", return_value=False),
    ):
        agent = LlmioChatAgent(model_level=2, instruction="Be helpful.")
        with pytest.raises(RuntimeError, match="unrelated failure"):
            _ = [c async for c in agent.stream("hi")]

    assert create_model_patch.call_count == 1


@pytest.mark.asyncio
async def test_retry_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Retries are handled by robotsix-http; no per-retry agent log lines."""
    call_count = 0

    async def fail_twice(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> MagicMock:
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
        patch(
            "robotsix_chat.llm.agent.get_provider_for_identifier", create_model_patch
        ),
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

    async def fail_twice(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> MagicMock:
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
        patch(
            "robotsix_chat.llm.agent.get_provider_for_identifier", create_model_patch
        ),
        patch("robotsix_chat.llm.agent.is_openrouter_transient", return_value=True),
        patch("robotsix_http.retry.asyncio.sleep", new=sleep_mock),
    ):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        _ = [c async for c in agent.stream("hi")]

    # Two transient failures → two sleeps, three total attempts.
    assert sleep_mock.await_count == 2
    assert provider.build_agent.call_count == 3


# ---------------------------------------------------------------------------
# Image attachments — llmio images= seam (never BinaryContent in the prompt)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_with_images_passes_images_to_build_agent() -> None:
    """Attachments go to llmio's ``build_agent(images=...)`` seam.

    The prompt stays a plain string: the claude transport reads images
    natively; text-only OpenRouter models get the injected ask_image tool.
    """
    create_model, handle = _patched_create_model("I see an image!")

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
        agent = LlmioChatAgent(model_level=1, instruction="Be helpful.", api_key="k")
        chunks = [
            c
            async for c in agent.stream(
                "describe this", images=[("image/png", b"fake-png-data")]
            )
        ]

    assert chunks == ["I see an image!"]
    run_arg = handle.run_calls[0]["message"]
    assert isinstance(run_arg, str)
    assert run_arg == "describe this"
    provider = create_model.return_value
    kwargs = provider.build_agent.call_args.kwargs
    assert kwargs["images"] == [("image/png", b"fake-png-data")]
    assert kwargs["vision_api_key"] == "k"


@pytest.mark.asyncio
async def test_stream_with_images_only_no_text() -> None:
    """Images-only (empty message) still runs with a plain-text prompt.

    The attachment travels via ``images=`` only.
    """
    create_model, handle = _patched_create_model("nice pic")

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
        agent = LlmioChatAgent(model_level=1, instruction="Be helpful.", api_key="k")
        chunks = [
            c async for c in agent.stream("", images=[("image/jpeg", b"jpeg-data")])
        ]

    assert chunks == ["nice pic"]
    run_arg = handle.run_calls[0]["message"]
    assert isinstance(run_arg, str)
    provider = create_model.return_value
    kwargs = provider.build_agent.call_args.kwargs
    assert kwargs["images"] == [("image/jpeg", b"jpeg-data")]


@pytest.mark.asyncio
async def test_stream_without_images_still_passes_plain_string() -> None:
    """With no images, ``build_agent`` gets ``images=None``.

    handle.run receives a plain str; no tool injection, no native attachments.
    """
    create_model, handle = _patched_create_model("text-only reply")

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        chunks = [c async for c in agent.stream("hello")]

    assert chunks == ["text-only reply"]
    run_arg = handle.run_calls[0]["message"]
    assert isinstance(run_arg, str)
    assert run_arg == "hello"
    provider = create_model.return_value
    assert provider.build_agent.call_args.kwargs["images"] is None


@pytest.mark.asyncio
async def test_replayed_history_is_text_only_even_with_images() -> None:
    """History replay must NEVER carry binary parts.

    A single BinaryContent in history 404s every text turn of the session on
    text-only OpenRouter models ('No endpoints found that support image
    input', live incident 2026-09-01). Attachments are per-turn via
    ``images=``; prior turns replay as pure text.
    """
    from pydantic_ai.messages import ModelRequest, ModelResponse

    create_model, handle = _patched_create_model("ok")

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
        agent = LlmioChatAgent(model_level=1, instruction="Be helpful.", api_key="k")
        _ = [
            c
            async for c in agent.stream(
                "and now?",
                history=[("look at this", "I described the image")],
                images=[("image/png", b"new-turn-image")],
            )
        ]

    message_history = handle.run_calls[0]["message_history"]
    assert message_history is not None
    for msg in message_history:
        assert isinstance(msg, (ModelRequest, ModelResponse))
        for part in msg.parts:
            assert isinstance(part.content, str)


def test_build_message_history_yields_only_text_parts() -> None:
    """The history builder's text-only invariant, asserted directly."""
    from robotsix_chat.llm.agent import _build_message_history

    messages = _build_message_history([("u1", "a1"), ("u2", "a2")])
    assert messages is not None
    for msg in messages:
        for part in msg.parts:
            assert isinstance(part.content, str)


# ---------------------------------------------------------------------------
# Model level passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_level_passed_to_build_agent() -> None:
    """The constructor's ``model_level`` is forwarded to ``build_agent``."""
    create_model, _ = _patched_create_model("ok")
    provider = create_model.return_value

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
        agent = LlmioChatAgent(model_level=3, instruction="Be helpful.")
        _ = [c async for c in agent.stream("hi")]

    assert create_model.return_value.build_agent.call_args.kwargs["level"] == 3
    assert provider.build_agent.call_args.kwargs["level"] == 3


# ---------------------------------------------------------------------------
# Auth-failure tier fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_fails_over_to_keyed_slot() -> None:
    """An expired Claude credential takes the whole default slot down.

    The agent wraps the chained ``ClaudeSDKAuthError`` as a provider-wide
    exhaustion so llmio's failover retries the SAME level on the keyed
    OpenRouter slot — and the key is forwarded there even though the
    default slot is keyless.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKAuthError

    async def expired(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> None:
        raise ClaudeSDKAuthError(
            "Failed to authenticate. API Error: 401 OAuth access token has expired."
        )

    async def recovered(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> MagicMock:
        result = MagicMock()
        result.output = "openrouter reply"
        return result

    get_provider, _, _ = _slot_handles(expired, recovered)

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", get_provider):
        agent = LlmioChatAgent(
            model_level=2,
            instruction="Be helpful.",
            api_key="sk-or-live",  # pragma: allowlist secret
        )
        chunks = [c async for c in agent.stream("hi")]

        assert chunks == ["openrouter reply"]
        keyed_call = get_provider.call_args_list[-1]
        assert keyed_call.args[0].startswith("openrouter")
        assert keyed_call.kwargs["api_key"] == "sk-or-live"  # pragma: allowlist secret

        # The dead credential armed the failover window immediately — the
        # next turn skips the doomed default attempt.
        get_provider.reset_mock()
        _ = [c async for c in agent.stream("again")]
        assert get_provider.call_args_list[0].args[0].startswith("openrouter")


@pytest.mark.asyncio
async def test_keyless_primary_level_never_receives_the_api_key() -> None:
    """Holding the key must not change what the primary level is given.

    The agent now always carries the configured OpenRouter key so a fallback
    can reach a keyed tier — but a keyless claudeSDK provider rejects an
    api_key, so the primary call must still be made without one.
    """
    handle = MagicMock()

    async def reply(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> MagicMock:
        result = MagicMock()
        result.output = "sdk reply"
        return result

    handle.run = reply
    handle.close = MagicMock()
    provider = MagicMock()
    provider.build_agent.return_value = handle
    create_model_patch = MagicMock(return_value=provider)

    with patch(
        "robotsix_chat.llm.agent.get_provider_for_identifier", create_model_patch
    ):
        agent = LlmioChatAgent(
            model_level=2,
            instruction="Be helpful.",
            api_key="or-key",  # pragma: allowlist secret
        )
        chunks = [c async for c in agent.stream("hi")]

    assert chunks == ["sdk reply"]
    create_model_patch.assert_called_once_with("claudeSDK-opus")


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

    async def fake_run(prompt, *, message_history=None, **run_kwargs):
        seen["prompt"] = prompt
        seen["history"] = message_history
        result = MagicMock()
        result.output = "ok"
        return result

    handle.run = fake_run

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
        agent = LlmioChatAgent(model_level=2, instruction="Be helpful.")
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

    async def fake_run(prompt, *, message_history=None, **run_kwargs):
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

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", create_model):
        agent = LlmioChatAgent(model_level=2, instruction="Be helpful.", memory=memory)
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
async def test_activity_events_feed_the_actions_collector() -> None:
    """tool_call / tool_result events land as paired entries in the collector.

    Works without an event sink: the collector is the caller's, the sink is
    the UI's, and either alone is reason enough to wire the callback.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKActivityEvent
    from robotsix_llmio.claude_sdk._stream import _current_on_event

    from robotsix_chat.chat.actions import collect_actions

    handle = MagicMock()

    async def fake_run(
        message: str, *, message_history: object = None, **run_kwargs: object
    ) -> MagicMock:
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
        "robotsix_chat.llm.agent.get_provider_for_identifier",
        MagicMock(return_value=provider),
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

    async def fake_run(
        message: str, *, message_history: object = None, **run_kwargs: object
    ) -> MagicMock:
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
        "robotsix_chat.llm.agent.get_provider_for_identifier",
        MagicMock(return_value=provider),
    ):
        agent = LlmioChatAgent(model_level=1, api_key="k", instruction="Be helpful.")
        with collect_actions() as actions:
            _ = [c async for c in agent.stream("go", session_id="sess-1")]
        # Without a collector nothing is recorded and nothing breaks.
        _ = [c async for c in agent.stream("go again", session_id="sess-1")]

    assert actions == ['spawn_subsession({"kind": "task"}) -> sub-42']


@pytest.mark.asyncio
async def test_keyed_fallback_caps_oversized_history() -> None:
    """A keyed fallback slot caps the history to fit its token window.

    When the conversation history is too large for the fallback slot's output
    window (level-2 flash: 65 536 max_tokens), the oldest turns are dropped
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

    # The Claude default slot is exhausted; failover lands on the keyed slot.
    async def exhausted(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> None:
        raise ClaudeSDKUsageExhaustedError("You're out of usage credits")

    seen: dict[str, object] = {}

    async def recovered(
        _message: str, *, message_history: object = None, **run_kwargs: object
    ) -> MagicMock:
        seen["message_history"] = message_history
        result = MagicMock()
        result.output = "capped reply"
        return result

    get_provider, _, _ = _slot_handles(exhausted, recovered)

    with patch("robotsix_chat.llm.agent.get_provider_for_identifier", get_provider):
        agent = LlmioChatAgent(model_level=2, instruction="Be helpful.", api_key="k")
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


def test_chat_overrides_fallback_workhorse_to_pro() -> None:
    """Chat-only operator override: the fleet's baked fallback L2 is flash,
    but chat's long conversational contexts degenerated on flash under xhigh
    reasoning — chat binds its fallback level 2 to the pro snapshot (mill
    keeps the baked flash).
    """
    from robotsix_llmio.config import load_tier_config

    agent = LlmioChatAgent(model_level=2, instruction="Be helpful.")
    assert (
        agent._tier_config.fallback.level2.model_name == "deepseek/deepseek-v4-pro-0813"
    )
    # The override is chat-local: llmio's baked default stays flash.
    assert (
        load_tier_config({}).fallback.level2.model_name
        == "deepseek/deepseek-v4-flash-20260731"
    )
    # Other bindings untouched.
    assert (
        agent._tier_config.fallback.level1.model_name
        == "deepseek/deepseek-v4-flash-20260731"
    )
    assert agent._tier_config.default.level2.model == "claudeSDK-opus"

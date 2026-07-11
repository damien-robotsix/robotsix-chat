"""LLM chat agent backed by robotsix-llmio's per-level model factory.

:class:`LlmioChatAgent` satisfies the chat server's ``ChatAgent`` protocol
(``async def stream(message) -> AsyncIterator[str]``). It selects the backend
purely from a capability **level** via
:func:`robotsix_llmio.config.create_model`: the level encodes the combined
``provider-model`` identifier (resolved from llmio's baked default
``TierLevelConfig``), so this package never names a concrete provider class or
the Claude Agent SDK.

By default: level 1-2 → ``openrouter-deepseek/...`` (needs an API
key), level 3 → ``claudeSDK-opus`` (keyless, via the logged-in ``claude`` CLI).

Responses are returned as a single block (not token-streamed): llmio's Claude
SDK model does not support incremental streaming through pydantic-ai, so each
``stream`` call yields the full reply once. The chat server still frames it as a
normal SSE ``token`` + ``done`` sequence.

The provider dependencies are obtained through robotsix-llmio's own extras —
``robotsix-llmio[claude-sdk]`` and ``robotsix-llmio[openrouter]`` —
wired via this package's ``claude-sdk`` / ``openrouter`` extras.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from robotsix_llmio.claude_sdk import (
    ClaudeSDKActivityEvent,
    ClaudeSDKUsageExhaustedError,
    activity_events,
)
from robotsix_llmio.config import create_model
from robotsix_llmio.config.tier import TierConfig, TierLevel, TierLevelConfig
from robotsix_llmio.core.tier_fallback import acall_with_tier_fallback
from robotsix_llmio.openrouter import is_openrouter_transient

from robotsix_chat.chat.events import EventSink, activity_frame
from robotsix_chat.memory import ChatMemory, NullMemory

logger = logging.getLogger(__name__)

# Transient upstream errors (OpenRouter provider failures, 5xx, network
# blips) are retried up to this many times.  A fresh agent handle is built
# per attempt so each try starts from a clean state.
_MAX_RUN_ATTEMPTS = 3
_RETRY_BACKOFFS = (0.5, 1.0)

# A prior conversation turn replayed to the agent: ``(user, assistant)``.
Turn = tuple[str, str]


def _build_message_history(history: list[Turn] | None) -> list[Any] | None:
    """Convert ``(user, assistant)`` turns into a pydantic-ai message history.

    Returns ``None`` for empty history (so callers pass nothing through). The
    pydantic-ai message types are imported lazily — llmio is built on
    pydantic-ai and ``handle.run`` already returns its result objects, but
    importing them here keeps the dependency off the module import path.
    """
    if not history:
        return None
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )

    messages: list[Any] = []
    for user_message, assistant_reply in history:
        messages.append(ModelRequest(parts=[UserPromptPart(content=user_message)]))
        messages.append(ModelResponse(parts=[TextPart(content=assistant_reply)]))
    return messages


@contextlib.contextmanager
def _trace_session(
    session_id: str | None,
    trace_metadata: dict[str, str] | None = None,
) -> Iterator[None]:
    """Group the enclosed agent run under *session_id* in Langfuse.

    A no-op when *session_id* is falsy or llmio's tracing extra is absent, so
    callers can wrap unconditionally.

    When *trace_metadata* is supplied, each key-value pair is stamped as a
    span attribute on the current recording span (if any) inside the session
    context — used for parent/owner lineage so the trace tree mirrors the
    subsession tree in observability.
    """
    if not session_id:
        yield
        return
    try:
        from robotsix_llmio.core.tracing import langfuse_session
    except ImportError:
        yield
        return
    with langfuse_session(session_id):
        if trace_metadata:
            _stamp_trace_metadata(trace_metadata)
        yield


def _stamp_trace_metadata(metadata: dict[str, str]) -> None:
    """Stamp *metadata* as attributes on the current OTel recording span.

    A no-op when OpenTelemetry is absent or no span is currently recording —
    the attributes are best-effort observability, not critical to the run.
    """
    try:
        from robotsix_llmio.core.tracing import get_recording_span
    except ImportError:
        return
    span = get_recording_span()
    if span is not None:
        for key, value in metadata.items():
            span.set_attribute(key, value)


def _activity_context(
    on_event: Callable[[ClaudeSDKActivityEvent], None] | None,
) -> contextlib.AbstractContextManager[None]:
    """``activity_events(on_event)``, or a no-op when *on_event* is ``None``.

    ``None`` means no sink was configured, or no session to scope frames to.
    """
    if on_event is None:
        return contextlib.nullcontext()
    return activity_events(on_event)


# Fencing for the recalled-memory block prepended to the current user turn.
# Recall is similarity-based, so the recalled text is often about the same
# topic as the live message — without an explicit end marker the model can
# read the whole turn as background and conclude there is no active request
# (observed 2026-07-11 on a subsession first turn, whose instructions carry
# no conversational framing that would separate them from the recall block).
_MEMORY_PROMPT_HEADER = (
    "# Relevant memory from earlier conversations\n"
    "Use this background only if it helps; ignore it otherwise.\n"
)
_MEMORY_PROMPT_FOOTER = (
    "\n# End of recalled memory\n"
    "Everything below is the current message — act on it now:\n"
)


class LlmioChatAgent:
    """Stream LLM responses via robotsix-llmio, selected by capability level.

    Each ``stream`` call builds a fresh llmio agent handle (deterministically
    closed). When a :class:`~robotsix_chat.memory.ChatMemory` is supplied, the
    agent gains continuity across calls: it recalls relevant memory before
    replying and persists the exchange afterwards (the write runs in the
    background so it never adds latency). With the default :class:`NullMemory`
    it stays fully stateless.
    """

    def __init__(
        self,
        *,
        model_level: int,
        instruction: str,
        api_key: str = "",
        memory: ChatMemory | None = None,
        tools: list[Any] | None = None,
        request_tools_factory: Callable[[str], list[Any]] | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        """Store the agent configuration for later ``stream`` calls.

        *request_tools_factory* is called once per ``stream`` invocation with
        the request's *client_id* to produce per-request tools (e.g. the
        subsession tools whose closures capture that session id).  It keeps
        the module dependency acyclic: those tools are built fresh per
        request inside ``stream``, not baked into the shared agent.

        *event_sink*, when given, receives an ``activity`` frame (see
        :func:`robotsix_chat.chat.events.activity_frame`) for every tool
        call, tool result, thinking block, or intermediate assistant text
        the claudeSDK backend streams during a turn — live feedback on what
        the agent is doing while the final reply is still pending. A
        non-claudeSDK level (e.g. an OpenRouter tier) simply never triggers
        it: ``robotsix_llmio.claude_sdk.activity_events()`` is a no-op unless
        the resolved transport is the Claude Agent SDK.
        """
        self._model_level = model_level
        self._instruction = instruction
        self._api_key = api_key
        self._memory: ChatMemory = memory if memory is not None else NullMemory()
        # Tools the underlying agent may call (e.g. the mill consult tool). When
        # non-empty, llmio runs a real tool loop; the final reply is still
        # returned as one block.
        self._tools = list(tools) if tools is not None else None
        self._request_tools_factory = request_tools_factory
        self._event_sink = event_sink
        # Hold references to in-flight background writes so they aren't GC'd.
        self._write_tasks: set[asyncio.Task[None]] = set()

    def _activity_callback(
        self, session_id: str | None
    ) -> Callable[[ClaudeSDKActivityEvent], None] | None:
        """Build the ``on_event`` callback for :func:`activity_events`.

        Bound to *session_id*. Returns ``None`` when there is nowhere to
        publish to (no sink configured, or no session to scope the frame to)
        so the caller can skip wrapping the run in a no-op context.
        """
        if self._event_sink is None or not session_id:
            return None
        sink = self._event_sink

        def _on_activity(event: ClaudeSDKActivityEvent) -> None:
            sink.publish(
                session_id,
                activity_frame(
                    event.kind,
                    event.turn,
                    tool_name=event.tool_name,
                    detail=event.detail,
                    is_error=event.is_error,
                ),
            )

        return _on_activity

    def _publish_synthetic_activity(
        self,
        session_id: str | None,
        kind: str,
        *,
        tool_name: str | None = None,
        detail: str = "",
        is_error: bool = False,
    ) -> None:
        """Publish an activity frame for a preliminary step outside the SDK run.

        E.g. memory recall — so the UI shows something during phases the
        Claude SDK's own activity events don't cover — otherwise the typing
        indicator sits blank for as long as that step takes, which can be
        the majority of the wall-clock time for a turn (memory recall alone
        has been observed taking 90+ seconds).

        A no-op when there is nowhere to publish to (no sink, no session).
        """
        if self._event_sink is None or not session_id:
            return
        self._event_sink.publish(
            session_id,
            activity_frame(
                kind, 0, tool_name=tool_name, detail=detail, is_error=is_error
            ),
        )

    async def stream(
        self,
        message: str,
        *,
        history: list[Turn] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
        trace_metadata: dict[str, str] | None = None,
    ) -> AsyncIterator[str]:
        """Yield the assistant's reply to *message* as a single block.

        *history* is the prior ``(user, assistant)`` turns of the current
        conversation, replayed to the agent so it has multi-turn context.
        *session_id* groups this run's trace spans under one conversation in
        Langfuse (a fresh id starts a new trace). *client_id* identifies the
        owning browser — it is forwarded to the per-request tools factory so
        delegation tools can tag spawned tasks correctly.  *images* is an
        optional list of ``(media_type, raw_bytes)`` pairs (e.g.
        ``[("image/png", b"...")]``) — when non-empty the prompt is built as a
        multimodal sequence so a vision-capable LLM can see the attachments.
        All keyword arguments are optional — with none, the agent behaves as a
        single stateless query.

        Transient upstream errors (OpenRouter provider failures, 5xx, network
        blips) are retried up to :data:`_MAX_RUN_ATTEMPTS` before surfacing.
        A claudeSDK tier reporting exhausted usage credits is not retried at
        the same tier — see :meth:`_run_with_usage_fallback`. Non-transient
        errors and exhausted retries are raised — the chat server turns that
        into an SSE ``error`` frame.
        """
        # Recall relevant memory and prepend it to the current user turn.
        # recall() never raises (it degrades to "" on any backend failure).
        # The block must NOT go into the system prompt: recall text changes
        # with every message, and the system prompt is the head of the
        # provider's cacheable prefix — mutating it there invalidates the
        # prompt cache for the whole request on every turn. Prepending to the
        # newest user turn keeps the instruction, tools, and replayed
        # transcript byte-stable and cache-servable.
        self._publish_synthetic_activity(
            session_id, "tool_call", tool_name="recall_memory"
        )
        recalled = await self._memory.recall(message, session_id=session_id)
        self._publish_synthetic_activity(
            session_id,
            "tool_result",
            detail=(
                f"found {len(recalled)} chars of prior context"
                if recalled
                else "no relevant memory found"
            ),
        )
        system_prompt = self._instruction
        llm_message = message
        if recalled:
            llm_message = (
                f"{_MEMORY_PROMPT_HEADER}{recalled}{_MEMORY_PROMPT_FOOTER}\n{message}"
            )

        # Forward the key only when one is configured; keyless levels
        # (claudeSDK) must not receive an api_key (the provider rejects it).
        if self._api_key:
            provider = create_model(level=self._model_level, api_key=self._api_key)
        else:
            provider = create_model(level=self._model_level)
        message_history = _build_message_history(history)

        # Compute effective tools once: static tools + per-request tools from
        # the factory (which captures client_id lexically so delegation works
        # even across the claude_sdk/MCP execution-context boundary).
        effective_tools: list[Any] = list(self._tools) if self._tools else []
        if self._request_tools_factory and client_id:
            effective_tools.extend(self._request_tools_factory(client_id))
        tools_arg = effective_tools or None

        # Build the user-prompt once: plain str (no images) or a multimodal
        # list (text + BinaryContent parts). NOTE: the default model_level 3
        # routes to robotsix_llmio's claude_sdk model, whose internal
        # _content_to_text() flattens non-text content to str(...) — images
        # are silently dropped on that path. To have the assistant actually
        # *see* images, configure a vision-capable OpenRouter model at level
        # 1 or 2. Full level-3 image support requires an external change to
        # robotsix_llmio's claude_sdk model to map image parts into the
        # Claude SDK request format.
        if images:
            from pydantic_ai.messages import BinaryContent

            user_prompt: list[str | BinaryContent] = []
            if llm_message:
                user_prompt.append(llm_message)
            for mt, data in images:
                user_prompt.append(BinaryContent(data=data, media_type=mt))
            prompt: object = user_prompt
        else:
            prompt = llm_message

        on_activity = self._activity_callback(session_id)
        result: Any = None
        for attempt in range(1, _MAX_RUN_ATTEMPTS + 1):
            # Build a fresh handle per attempt so each try starts from a
            # clean state (the handle is always closed in the finally block
            # below regardless of success or failure).
            handle = provider.build_agent(
                level=self._model_level,
                system_prompt=system_prompt,
                tools=tools_arg,
                builtin_tools=False,
            )
            try:
                try:
                    with (
                        _trace_session(session_id, trace_metadata),
                        _activity_context(on_activity),
                    ):
                        result = await handle.run(
                            prompt, message_history=message_history
                        )
                finally:
                    handle.close()
            except ClaudeSDKUsageExhaustedError as exc:
                logger.warning(
                    "model_level %d usage credits exhausted (%s) — "
                    "falling back to another tier for this turn",
                    self._model_level,
                    exc,
                )
                result = await self._run_with_usage_fallback(
                    prompt, message_history, tools_arg, session_id, trace_metadata
                )
                break
            except Exception as exc:
                if attempt == _MAX_RUN_ATTEMPTS or not is_openrouter_transient(exc):
                    raise
                logger.warning(
                    "transient backend error on attempt %d/%d (%s), retrying",
                    attempt,
                    _MAX_RUN_ATTEMPTS,
                    type(exc).__name__,
                )
                await asyncio.sleep(_RETRY_BACKOFFS[attempt - 1])
                continue
            break

        # The loop above always either raises or breaks with `result` set.
        text = result.output
        # Persist the exchange in the background so memory consolidation never
        # blocks the reply. The task is tracked to avoid premature GC.
        if text:
            self._schedule_remember(message, text, session_id)
            yield text

    async def _run_with_usage_fallback(
        self,
        prompt: object,
        message_history: list[Any] | None,
        tools_arg: list[Any] | None,
        session_id: str | None,
        trace_metadata: dict[str, str] | None = None,
    ) -> Any:
        """Retry the same turn at a different tier after a usage-exhaustion.

        Triggered by
        :class:`~robotsix_llmio.claude_sdk.ClaudeSDKUsageExhaustedError` at
        ``self._model_level``. Reuses robotsix-llmio's tier-escalation
        machinery
        (:func:`~robotsix_llmio.core.tier_fallback.acall_with_tier_fallback` —
        higher-then-lower, revisit-avoiding, depth-bounded) rather than
        hand-rolling a fallback chain, so it is entered ONLY once this
        specific cause has already been identified — any other failure
        during the primary attempt still raises immediately as before.

        Scoped to one promotion (``max_fallback_depth=1``): the only known
        need today is claudeSDK level 4 (fable) -> level 3 (opus), both
        keyless, so this never needs to forward an OpenRouter key for a
        lower tier this agent was not otherwise configured with one for.

        Note: ``acall_with_tier_fallback`` always retries its *starting*
        level once before escalating (it has no way to know this level was
        already just attempted). That first retry is expected to fail
        identically (the credits are still exhausted) and fail fast — a
        harmless, cheap redundant call, not a bug — before the loop falls
        back to the next tier.
        """
        tier_config = TierConfig()
        level_by_model = {
            getattr(tier_config, level.value).model: int(
                level.value.removeprefix("level")
            )
            for level in TierLevel
        }

        on_activity = self._activity_callback(session_id)

        def _fn_factory(tlc: TierLevelConfig) -> Callable[[], Any]:
            level = level_by_model[tlc.model]

            async def _call() -> Any:
                fallback_provider = create_model(level=level)
                fallback_handle = fallback_provider.build_agent(
                    level=level,
                    system_prompt=self._instruction,
                    tools=tools_arg,
                    builtin_tools=False,
                )
                try:
                    with (
                        _trace_session(session_id, trace_metadata),
                        _activity_context(on_activity),
                    ):
                        return await fallback_handle.run(
                            prompt, message_history=message_history
                        )
                finally:
                    fallback_handle.close()

            return _call

        return await acall_with_tier_fallback(
            _fn_factory,
            tier_config=tier_config,
            level=TierLevel(f"level{self._model_level}"),
            fallback_enabled=True,
            max_fallback_depth=1,
            what="chat turn (usage-exhausted fallback)",
        )

    def _schedule_remember(
        self, message: str, reply: str, session_id: str | None
    ) -> None:
        """Fire-and-forget the memory write for a completed exchange."""
        try:
            task = asyncio.create_task(
                self._memory.remember(message, reply, session_id=session_id)
            )
        except RuntimeError:
            # No running loop (shouldn't happen in the ASGI path) — skip silently.
            return
        self._write_tasks.add(task)
        task.add_done_callback(self._write_tasks.discard)

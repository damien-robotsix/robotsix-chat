"""LLM chat agent backed by robotsix-llmio's per-level model factory.

:class:`LlmioChatAgent` satisfies the chat server's ``ChatAgent`` protocol
(``async def stream(message) -> AsyncIterator[str]``). It selects the backend
purely from a capability **level** via
:func:`robotsix_llmio.config.create_model`: the level encodes the combined
``provider-model`` identifier (resolved from llmio's baked default
``TierLevelConfig``), so this package never names a concrete provider class or
the Claude Agent SDK.

By default: level 1-2 → ``openrouter[deepseek]-deepseek/...`` (needs an API
key), level 3 → ``claudeSDK-opus`` (keyless, via the logged-in ``claude`` CLI).

Responses are returned as a single block (not token-streamed): llmio's Claude
SDK model does not support incremental streaming through pydantic-ai, so each
``stream`` call yields the full reply once. The chat server still frames it as a
normal SSE ``token`` + ``done`` sequence.

The provider dependencies are obtained through robotsix-llmio's own extras —
``robotsix-llmio[claude-sdk]`` and ``robotsix-llmio[openrouter-deepseek]`` —
wired via this package's ``claude-sdk`` / ``openrouter`` extras.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from robotsix_llmio.config import create_model
from robotsix_llmio.openrouter import is_openrouter_transient

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
def _trace_session(session_id: str | None) -> Iterator[None]:
    """Group the enclosed agent run under *session_id* in Langfuse.

    A no-op when *session_id* is falsy or llmio's tracing extra is absent, so
    callers can wrap unconditionally.
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
        yield


# Header for the recalled-memory block injected into the system prompt.
_MEMORY_PROMPT_HEADER = (
    "# Relevant memory from earlier conversations\n"
    "Use this background only if it helps; ignore it otherwise.\n"
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
        model_name: str | None = None,
        max_output_tokens: int | None = None,
        output_stop_sequences: list[str] | None = None,
    ) -> None:
        """Store the agent configuration for later ``stream`` calls.

        *request_tools_factory* is called once per ``stream`` invocation with
        the request's *client_id* to produce per-request tools (e.g. the
        ``delegate_task`` tool whose closure captures that client_id).  It
        keeps the module dependency acyclic: delegation tools are built fresh
        per request inside ``stream``, not baked into the shared agent.

        *model_name* is an optional bare model name override (e.g. ``"sonnet"``
        or ``"haiku"``) passed directly to ``provider.build_agent(model=...)``.
        When ``None`` (the default), the model name is resolved from the level's
        tier default — behaviour is unchanged.

        *max_output_tokens* is an optional hard cap on LLM completion tokens
        per agent turn, passed as ``model_settings.max_tokens``.  ``None``
        (the default) disables the cap.

        *output_stop_sequences* is an optional list of stop strings passed as
        ``model_settings.stop_sequences``.  ``None`` (the default) leaves it
        unset.
        """
        self._model_level = model_level
        self._instruction = instruction
        self._api_key = api_key
        self._memory: ChatMemory = memory if memory is not None else NullMemory()
        # Tools the underlying agent may call (e.g. the mill consult tool). When
        # non-empty, llmio runs a real tool loop; the final reply is still
        # returned as one block.
        self._tools = tools or None
        self._request_tools_factory = request_tools_factory
        # Bare model-name override for the provider (e.g. "sonnet" for
        # claudeSDK-subscription sub-agents). None → resolved from tier default.
        self._model_name = model_name
        # Hard cap on LLM completion tokens per agent turn (model_settings.max_tokens).
        self._max_output_tokens = max_output_tokens
        # Optional stop strings (model_settings.stop_sequences).
        self._output_stop_sequences = output_stop_sequences
        # Hold references to in-flight background writes so they aren't GC'd.
        self._write_tasks: set[asyncio.Task[None]] = set()

    async def stream(
        self,
        message: str,
        *,
        history: list[Turn] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
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
        Non-transient errors and exhausted retries are raised — the chat server
        turns that into an SSE ``error`` frame.
        """
        # Recall relevant memory and fold it into the system prompt. recall()
        # never raises (it degrades to "" on any backend failure).
        recalled = await self._memory.recall(message)
        system_prompt = self._instruction
        if recalled:
            system_prompt = f"{system_prompt}\n\n{_MEMORY_PROMPT_HEADER}{recalled}"

        # Forward the key only when one is configured; keyless levels
        # (claudeSDK) must not receive an api_key (the provider rejects it).
        provider_kwargs: dict[str, str] = {}
        if self._api_key:
            provider_kwargs["api_key"] = self._api_key

        provider = create_model(level=self._model_level, **provider_kwargs)
        message_history = _build_message_history(history)

        # Compute effective tools once: static tools + per-request tools from
        # the factory (which captures client_id lexically so delegation works
        # even across the claude_sdk/MCP execution-context boundary).
        effective_tools: list[Any] = list(self._tools) if self._tools else []
        if self._request_tools_factory and client_id:
            effective_tools.extend(self._request_tools_factory(client_id))
        tools_arg = effective_tools or None

        for attempt in range(1, _MAX_RUN_ATTEMPTS + 1):
            # Build a fresh handle per attempt so each try starts from a
            # clean state (the handle is always closed in the finally block
            # below regardless of success or failure).
            handle = provider.build_agent(
                level=self._model_level,
                model=self._model_name,
                system_prompt=system_prompt,
                tools=tools_arg,
                builtin_tools=False,
            )
            try:
                try:
                    with _trace_session(session_id):
                        # Build the user-prompt: plain str (no images) or a
                        # multimodal list (text + BinaryContent parts).
                        # NOTE: the default model_level 3 routes to
                        # robotsix_llmio's claude_sdk model, whose internal
                        # _content_to_text() flattens non-text content to
                        # str(...) — images are silently dropped on that
                        # path.  To have the assistant actually *see* images,
                        # configure a vision-capable OpenRouter model at
                        # level 1 or 2.  Full level-3 image support requires
                        # an external change to robotsix_llmio's claude_sdk
                        # model to map image parts into the Claude SDK
                        # request format.
                        if images:
                            from pydantic_ai.messages import BinaryContent

                            user_prompt: list[str | BinaryContent] = []
                            if message:
                                user_prompt.append(message)
                            for mt, data in images:
                                user_prompt.append(
                                    BinaryContent(data=data, media_type=mt)
                                )
                            prompt: object = user_prompt
                        else:
                            prompt = message
                        # Build model_settings when output capping is configured.
                        model_settings = None
                        if (
                            self._max_output_tokens is not None
                            or self._output_stop_sequences
                        ):
                            from pydantic_ai.settings import ModelSettings

                            ms: ModelSettings = {}
                            if self._max_output_tokens is not None:
                                ms["max_tokens"] = self._max_output_tokens
                            if self._output_stop_sequences:
                                ms["stop_sequences"] = self._output_stop_sequences
                            model_settings = ms

                        result = await handle.run(
                            prompt,
                            message_history=message_history,
                            model_settings=model_settings,
                        )
                finally:
                    handle.close()
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

            text = result.output
            # Persist the exchange in the background so memory consolidation never
            # blocks the reply. The task is tracked to avoid premature GC.
            if text:
                self._schedule_remember(message, text)
                yield text
            return

    def _schedule_remember(self, message: str, reply: str) -> None:
        """Fire-and-forget the memory write for a completed exchange."""
        try:
            task = asyncio.create_task(self._memory.remember(message, reply))
        except RuntimeError:
            # No running loop (shouldn't happen in the ASGI path) — skip silently.
            return
        self._write_tasks.add(task)
        task.add_done_callback(self._write_tasks.discard)

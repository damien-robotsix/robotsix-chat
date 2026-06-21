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
from collections.abc import AsyncIterator, Iterator
from typing import Any

from robotsix_llmio.config import create_model

from robotsix_chat.memory import ChatMemory, NullMemory

logger = logging.getLogger(__name__)

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

# Always appended to the system prompt. The agent runs with NO built-in system
# tools (no shell/file/web/host access — enforced in llmio via
# ``builtin_tools=False``); stating this makes the model decline such requests
# gracefully instead of repeatedly attempting a denied tool (which the SDK
# surfaces as a hard error). Tools explicitly provided (e.g. the mill consult
# tool) remain available and are exempted by "tools provided to you".
_AGENT_GUARD = (
    "\n\nYou are a conversational assistant with no ability to run shell "
    "commands, read or edit files, browse the web, or otherwise access the host "
    "system or its network. You can only converse and use the tools explicitly "
    "provided to you in this session. If a request needs access you don't have, "
    "briefly say so and suggest an alternative; never narrate or pretend to "
    "perform such actions."
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
    ) -> None:
        """Store the agent configuration for later ``stream`` calls."""
        self._model_level = model_level
        self._instruction = instruction
        self._api_key = api_key
        self._memory: ChatMemory = memory if memory is not None else NullMemory()
        # Tools the underlying agent may call (e.g. the mill consult tool). When
        # non-empty, llmio runs a real tool loop; the final reply is still
        # returned as one block.
        self._tools = tools or None
        # Hold references to in-flight background writes so they aren't GC'd.
        self._write_tasks: set[asyncio.Task[None]] = set()

    async def stream(
        self,
        message: str,
        *,
        history: list[Turn] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield the assistant's reply to *message* as a single block.

        *history* is the prior ``(user, assistant)`` turns of the current
        conversation, replayed to the agent so it has multi-turn context.
        *session_id* groups this run's trace spans under one conversation in
        Langfuse (a fresh id starts a new trace). Both are optional — with
        neither, the agent behaves as a single stateless query.

        Raises on backend errors — the chat server turns that into an SSE
        ``error`` frame.
        """
        # Recall relevant memory and fold it into the system prompt. recall()
        # never raises (it degrades to "" on any backend failure).
        recalled = await self._memory.recall(message)
        system_prompt = f"{self._instruction}{_AGENT_GUARD}"
        if recalled:
            system_prompt = f"{system_prompt}\n\n{_MEMORY_PROMPT_HEADER}{recalled}"

        # Forward the key only when one is configured; keyless levels
        # (claudeSDK) must not receive an api_key (the provider rejects it).
        provider_kwargs: dict[str, str] = {}
        if self._api_key:
            provider_kwargs["api_key"] = self._api_key

        provider = create_model(level=self._model_level, **provider_kwargs)
        handle = provider.build_agent(
            level=self._model_level,
            system_prompt=system_prompt,
            tools=self._tools,
            # The chat is an untrusted, internet-facing surface: never expose the
            # SDK's built-in tools (Bash/Read/Edit/...). Only the explicitly
            # provided tools (e.g. the mill consult tool) are callable.
            builtin_tools=False,
        )
        message_history = _build_message_history(history)
        try:
            with _trace_session(session_id):
                result = await handle.run(message, message_history=message_history)
        finally:
            handle.close()

        text = result.output
        # Persist the exchange in the background so memory consolidation never
        # blocks the reply. The task is tracked to avoid premature GC.
        if text:
            self._schedule_remember(message, text)
            yield text

    def _schedule_remember(self, message: str, reply: str) -> None:
        """Fire-and-forget the memory write for a completed exchange."""
        try:
            task = asyncio.create_task(self._memory.remember(message, reply))
        except RuntimeError:
            # No running loop (shouldn't happen in the ASGI path) — skip silently.
            return
        self._write_tasks.add(task)
        task.add_done_callback(self._write_tasks.discard)

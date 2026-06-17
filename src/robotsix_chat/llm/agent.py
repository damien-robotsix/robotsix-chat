"""LLM chat agent backed by robotsix-llmio's per-level model factory.

:class:`LlmioChatAgent` satisfies the chat server's ``ChatAgent`` protocol
(``async def stream(message) -> AsyncIterator[str]``). It selects the backend
purely from a capability **level** via
:func:`robotsix_llmio.config.create_model`: the level encodes both the
transport and the model (resolved from llmio's baked default ``TierConfig``),
so this package never names a concrete provider class or the Claude Agent SDK.

By default: level 1-2 → ``openrouter[deepseek]`` (needs an API key), level 3 →
``claude-sdk``/``opus`` (keyless, via the logged-in ``claude`` CLI).

Responses are returned as a single block (not token-streamed): llmio's Claude
SDK model does not support incremental streaming through pydantic-ai, so each
``stream`` call yields the full reply once. The chat server still frames it as a
normal SSE ``token`` + ``done`` sequence.

The transport dependencies are obtained through robotsix-llmio's own extras —
``robotsix-llmio[claude_sdk]`` and ``robotsix-llmio[openrouter-deepseek]`` —
wired via this package's ``claude-sdk`` / ``openrouter`` extras.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from robotsix_llmio.config import create_model


class LlmioChatAgent:
    """Stream LLM responses via robotsix-llmio, selected by capability level.

    Each ``stream`` call is an independent, stateless query (no conversation
    memory) — matching the chat server, which sends one user message at a time.
    A fresh llmio agent handle is built per call and deterministically closed.
    """

    def __init__(
        self,
        *,
        model_level: int,
        instruction: str,
        api_key: str = "",
    ) -> None:
        self._model_level = model_level
        self._instruction = instruction
        self._api_key = api_key

    async def stream(self, message: str) -> AsyncIterator[str]:
        """Yield the assistant's reply to *message* as a single block.

        Raises on backend errors — the chat server turns that into an SSE
        ``error`` frame.
        """
        # Forward the key only when one is configured; keyless levels
        # (claude-sdk) must not receive an api_key (the provider rejects it).
        provider_kwargs: dict[str, str] = {}
        if self._api_key:
            provider_kwargs["api_key"] = self._api_key

        provider = create_model(level=self._model_level, **provider_kwargs)
        handle = provider.build_agent(
            level=self._model_level, system_prompt=self._instruction
        )
        try:
            result = await handle.run(message)
        finally:
            handle.close()

        text = result.output
        if text:
            yield text

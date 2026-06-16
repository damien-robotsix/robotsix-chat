"""Async :class:`Agent` wrapping ``llmio.Agent`` with a streaming API.

The wrapper handles ``llmio.Agent`` lifecycle, tool registration, and
streaming token delivery.  All LLM and tool errors are caught and
converted to ``"Error: ..."`` tokens — the generator never raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

import llmio
from llmio.clients import BaseClient, OpenAIClient


class Agent:
    """Streaming LLM agent backed by ``llmio.Agent``.

    Exposes an ``async for token in agent.run("hello")`` API that yields
    one token per LLM delta, including whitespace.  Tool registration is
    delegated to the underlying ``llmio.Agent`` so tools can be either
    sync or async, and their type annotations are used to build the
    OpenAI JSON schema.
    """

    def __init__(
        self,
        instruction: str,
        *,
        client: BaseClient | None = None,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str | None = None,
        graceful_errors: bool = False,
    ) -> None:
        if client is None:
            client = OpenAIClient(
                api_key=api_key if api_key is not None else os.environ["LLM_API_KEY"],
                base_url=base_url,
            )
        self._agent = llmio.Agent(
            instruction=instruction,
            client=client,
            model=model,
            graceful_errors=graceful_errors,
        )

    def tool(
        self, fn: Callable[..., Any] | None = None, *, strict: bool = False
    ) -> Callable[..., Any]:
        """Register a tool function with the underlying ``llmio.Agent``.

        Supports both bare ``@agent.tool`` and parameterised
        ``@agent.tool(strict=True)`` forms, matching llmio's API exactly.
        The decorated function remains callable directly.
        """
        return self._agent.tool(fn, strict=strict)

    async def run(
        self,
        user_message: str,
        *,
        history: list[llmio.Message] | None = None,
    ) -> AsyncIterator[str]:
        """Stream LLM tokens for *user_message*.

        Parameters
        ----------
        user_message:
            The user message to send to the LLM.
        history:
            Optional conversation history (list of ``llmio.Message``
            typed dicts).  When provided it is forwarded verbatim to the
            underlying ``llmio.Agent.speak`` call.

        Yields
        ------
        str
            One token per LLM delta.  If the LLM or a tool raises, an
            ``"Error: <message>"`` token is yielded before the stream
            ends.  The generator never raises.
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        # -- on_stream callback that feeds the queue -------------------
        def on_delta(delta: str) -> None:
            queue.put_nowait(delta)

        self._agent.on_stream(on_delta)

        # -- background speak task -------------------------------------
        async def _speak() -> None:
            try:
                await self._agent.speak(user_message, history=history, stream=True)
            except Exception as exc:
                queue.put_nowait(f"Error: {exc}")
            finally:
                queue.put_nowait(None)

        task = asyncio.ensure_future(_speak())

        # -- yield tokens from the queue -------------------------------
        try:
            while True:
                token = await queue.get()
                if token is None:
                    break
                yield token
            # Let any exception from speak() propagate after the
            # sentinel — but we already caught exceptions inside
            # _speak, so this is just a safety check.
            await task
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._agent._stream_callbacks.remove(on_delta)

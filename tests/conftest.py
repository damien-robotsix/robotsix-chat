"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient


def _make_on_stream(
    callbacks: list[Callable[..., Any]],
) -> Callable[..., Any]:
    """Return a function that behaves like ``llmio.Agent.on_stream``.

    The returned callable stores *fn* in *callbacks* and returns it
    unchanged, matching the real decorator contract.
    """

    def on_stream(fn: Callable[..., Any]) -> Callable[..., Any]:
        callbacks.append(fn)
        return fn

    return on_stream


@pytest.fixture
def mock_llmio_agent() -> Generator[
    tuple[Any, Any, list[Callable[..., Any]]], None, None
]:
    """Patch ``llmio.Agent`` and set up stream-callback machinery.

    Yields a 3-tuple of:

    * ``MockLLMIOAgent`` — the mock **class**
    * ``mock_llmio`` — the mock **instance** (``MockLLMIOAgent.return_value``)
    * ``_stream_callbacks`` — the list that ``on_stream`` writes into
    """
    with patch("robotsix_chat.llm.agent.llmio.Agent") as MockLLMIOAgent:
        mock_llmio = MockLLMIOAgent.return_value
        _stream_callbacks: list[Callable[..., Any]] = []
        mock_llmio._stream_callbacks = _stream_callbacks
        mock_llmio.on_stream = _make_on_stream(_stream_callbacks)
        yield MockLLMIOAgent, mock_llmio, _stream_callbacks


@asynccontextmanager
async def http_client(
    app: Any,
) -> AsyncIterator[AsyncClient]:
    """Yield an ``httpx.AsyncClient`` wired to *app* via ``ASGITransport``."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

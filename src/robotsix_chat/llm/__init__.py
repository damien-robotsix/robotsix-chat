"""LLM-powered agent — streaming wrapper around ``llmio.Agent``.

Provides the :class:`Agent` class, an async, streaming-first wrapper that
delegates tool-calling to ``llmio`` while exposing a simple
``AsyncIterator[str]`` API.  Errors from the underlying LLM or tool layer
are caught and yielded as error tokens, so callers never see a raw
traceback mid-stream.
"""

from __future__ import annotations

from .agent import Agent

__all__ = ["Agent"]

"""LLM chat agent — robotsix-llmio-backed implementation.

Provides :class:`LlmioChatAgent`, which selects the LLM backend from a
``transport`` alias plus a ``model_level`` (via ``robotsix_llmio.config``) and
satisfies the chat server's ``ChatAgent`` protocol with a simple
``AsyncIterator[str]`` API. Replies are returned as a single block.
"""

from __future__ import annotations

from .agent import LlmioChatAgent

__all__ = ["LlmioChatAgent"]

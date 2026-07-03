"""Tests for the memory layer.

Covers :func:`build_memory`, ``NullMemory``, and the cognee backend's
graceful-degradation contract (cognee mocked, never imported for real).
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_chat.config import (
    MemoryEmbeddingSettings,
    MemoryLlmSettings,
    MemorySettings,
)
from robotsix_chat.memory import NullMemory, build_memory
from robotsix_chat.memory.cognee import CogneeMemory, _format_results


def _enabled_settings(data_dir: str = "/data/cognee") -> MemorySettings:
    """Return a valid enabled MemorySettings with key and endpoint present."""
    return MemorySettings(
        enabled=True,
        data_dir=data_dir,
        llm=MemoryLlmSettings(api_key="sk-or-x"),  # pragma: allowlist secret
        embedding=MemoryEmbeddingSettings(endpoint="http://box:11434/v1"),
    )


# ---------------------------------------------------------------------------
# build_memory selection
# ---------------------------------------------------------------------------


def test_build_memory_disabled_returns_null() -> None:
    """Disabled memory yields a NullMemory regardless of other fields."""
    assert isinstance(build_memory(MemorySettings(enabled=False)), NullMemory)


def test_build_memory_enabled_without_cognee_returns_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the cognee extra is absent, enabled memory degrades to NullMemory."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert isinstance(build_memory(_enabled_settings()), NullMemory)


def test_build_memory_enabled_with_cognee_returns_cognee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cognee is importable, enabled memory yields a CogneeMemory."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "cognee" else None,
    )
    assert isinstance(build_memory(_enabled_settings()), CogneeMemory)


# ---------------------------------------------------------------------------
# NullMemory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_memory_is_inert() -> None:
    """Verify that NullMemory stores and recalls nothing."""
    mem = NullMemory()
    await mem.setup()
    await mem.remember("u", "a")
    assert await mem.recall("anything") == ""


# ---------------------------------------------------------------------------
# _format_results
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ([], ""),
        (["France"], "France"),
        (["a", "", "b"], "a\nb"),
        ("plain", "plain"),
    ],
)
def test_format_results(value: Any, expected: str) -> None:
    """Verify _format_results produces the expected output for given input."""
    assert _format_results(value) == expected


def test_format_results_truncates() -> None:
    """Verify _format_results truncates output exceeding the max length."""
    out = _format_results(["x" * 9000])
    assert len(out) <= 4001
    assert out.endswith("…")


# ---------------------------------------------------------------------------
# CogneeMemory graceful degradation (cognee mocked)
# ---------------------------------------------------------------------------


def _install_fake_cognee(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a stub ``cognee`` module (awaitable mocks) and return it."""
    fake: Any = types.ModuleType("cognee")
    fake.config = MagicMock()

    class _SearchType:
        GRAPH_COMPLETION = "GRAPH_COMPLETION"

    fake.SearchType = _SearchType
    fake.search = AsyncMock(return_value=["recalled fact"])
    fake.add = AsyncMock(return_value=None)
    fake.cognify = AsyncMock(return_value=None)
    monkeypatch.setitem(sys.modules, "cognee", fake)
    return fake


@pytest.fixture
def cognee_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> tuple[CogneeMemory, Any]:
    """Fixture providing a CogneeMemory with a mocked cognee module."""
    fake = _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(_enabled_settings(str(tmp_path / "cognee")))
    return mem, fake


@pytest.mark.asyncio
async def test_cognee_recall_returns_formatted(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """Verify that cognee recall returns the mocked fact as a string."""
    mem, _ = cognee_memory
    assert await mem.recall("who?") == "recalled fact"


@pytest.mark.asyncio
async def test_cognee_recall_blank_query_skips(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """Verify that a blank query skips cognee entirely."""
    mem, fake = cognee_memory
    assert await mem.recall("   ") == ""
    # A skipped recall never configures or queries cognee.
    fake.config.set_llm_provider.assert_not_called()
    fake.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_cognee_remember_calls_add_and_cognify(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """Verify that remember calls add and cognify on the cognee module."""
    mem, fake = cognee_memory
    await mem.remember("hello", "hi there")
    fake.add.assert_awaited_once()
    fake.cognify.assert_awaited_once()


@pytest.mark.asyncio
async def test_cognee_recall_never_raises(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """A backend error during recall degrades to an empty string."""
    mem, fake = cognee_memory
    fake.search = AsyncMock(side_effect=RuntimeError("backend down"))
    assert await mem.recall("who?") == ""


@pytest.mark.asyncio
async def test_cognee_remember_never_raises(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """A backend error during remember is swallowed (logged, not raised)."""
    mem, fake = cognee_memory
    fake.add = AsyncMock(side_effect=RuntimeError("write failed"))
    await mem.remember("hello", "hi")  # must not raise


@pytest.mark.asyncio
async def test_configure_restores_langfuse_env(
    cognee_memory: tuple[CogneeMemory, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LANGFUSE_* creds are hidden for cognee's import, then restored.

    cognee force-enables a langfuse import when these are set; the chat's own
    llmio tracing still needs them after setup.
    """
    import os

    mem, _ = cognee_memory
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-keep")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-keep")
    await mem.setup()
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-keep"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-keep"  # pragma: allowlist secret

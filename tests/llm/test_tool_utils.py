"""Tests for ``robotsix_chat.llm.tool_utils``."""

from __future__ import annotations

import inspect

import pytest

from robotsix_chat.llm.tool_utils import require_args

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _identity(a: str) -> str:
    return a


async def _required_and_optional(x: str, y: str = "default") -> str:
    return f"{x}:{y}"


async def _all_optional(a: str = "a", b: str = "b") -> str:
    return f"{a}:{b}"


async def _multiple_required(a: str, b: str, c: int = 0) -> str:
    return f"{a}:{b}:{c}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passes_valid_arg() -> None:
    """A valid non-empty argument passes through to the wrapped function."""
    wrapped = require_args(_identity)
    result = await wrapped("hello")
    assert result == "hello"


@pytest.mark.asyncio
async def test_returns_error_for_empty_string() -> None:
    """An empty string for a required arg returns an error message."""
    wrapped = require_args(_identity)
    result = await wrapped("")
    assert "Error:" in result
    assert "_identity" in result
    assert "non-empty" in result
    assert "'a'" in result


@pytest.mark.asyncio
async def test_returns_error_for_none() -> None:
    """None for a required arg returns an error message."""
    wrapped = require_args(_identity)
    result = await wrapped(None)
    assert "Error:" in result
    assert "_identity" in result


@pytest.mark.asyncio
async def test_optional_empty_is_allowed() -> None:
    """An optional parameter may be an empty string."""
    wrapped = require_args(_required_and_optional)
    result = await wrapped("ok", "")
    assert result == "ok:"


@pytest.mark.asyncio
async def test_optional_none_is_allowed() -> None:
    """An optional parameter may be None."""
    wrapped = require_args(_required_and_optional)
    result = await wrapped("ok", None)
    assert result == "ok:None"


@pytest.mark.asyncio
async def test_required_empty_fails_even_with_optional_given() -> None:
    """A required arg that is empty fails even when optional args are ok."""
    wrapped = require_args(_required_and_optional)
    result = await wrapped("", "opt")
    assert "Error:" in result
    assert "_required_and_optional" in result
    assert "'x'" in result


def test_all_optional_never_fails() -> None:
    """When every parameter has a default the wrapper is a passthrough."""
    wrapped = require_args(_all_optional)
    # The wrapper returns the original fn, so these are identical objects.
    assert wrapped is _all_optional


@pytest.mark.asyncio
async def test_multiple_required_all_valid() -> None:
    """All required args valid — the call passes through."""
    wrapped = require_args(_multiple_required)
    result = await wrapped("x", "y", 42)
    assert result == "x:y:42"


@pytest.mark.asyncio
async def test_multiple_required_first_empty() -> None:
    """The first of multiple required args is empty."""
    wrapped = require_args(_multiple_required)
    result = await wrapped("", "y")
    assert "Error:" in result
    assert "'a'" in result


@pytest.mark.asyncio
async def test_multiple_required_second_empty() -> None:
    """The second of multiple required args is empty."""
    wrapped = require_args(_multiple_required)
    result = await wrapped("x", "")
    assert "Error:" in result
    assert "'b'" in result


@pytest.mark.asyncio
async def test_multiple_required_first_none() -> None:
    """The first of multiple required args is None."""
    wrapped = require_args(_multiple_required)
    result = await wrapped(None, "y")
    assert "Error:" in result
    assert "'a'" in result


@pytest.mark.asyncio
async def test_error_message_names_the_tool() -> None:
    """The error message includes the tool function's name."""
    wrapped = require_args(_identity)
    result = await wrapped("")
    assert "_identity" in result


@pytest.mark.asyncio
async def test_error_message_names_the_missing_arg() -> None:
    """The error message names the specific parameter that was empty."""
    wrapped = require_args(_identity)
    result = await wrapped("")
    assert "'a'" in result


@pytest.mark.asyncio
async def test_preserves_docstring() -> None:
    """functools.wraps copies __doc__."""

    async def _with_doc(x: str) -> str:
        """Read the thing."""
        return x

    wrapped = require_args(_with_doc)
    assert wrapped.__doc__ == "Read the thing."


@pytest.mark.asyncio
async def test_preserves_name() -> None:
    """functools.wraps copies __name__."""

    async def _named_tool(x: str) -> str:
        return x

    wrapped = require_args(_named_tool)
    assert wrapped.__name__ == "_named_tool"


# ---------------------------------------------------------------------------
# Sync tools keep their sync nature (regression: read_skill on the keyed
# fallback tier raised ``TypeError: 'str' object can't be awaited``).
# ---------------------------------------------------------------------------


def _sync_required(name: str) -> str:
    return f"skill:{name}"


def test_sync_tool_stays_sync_and_validates() -> None:
    wrapped = require_args(_sync_required)
    assert not inspect.iscoroutinefunction(wrapped)
    assert wrapped("x") == "skill:x"
    assert wrapped("") == "Error: _sync_required requires a non-empty 'name' argument."


@pytest.mark.asyncio
async def test_async_tool_stays_async() -> None:
    wrapped = require_args(_identity)
    assert inspect.iscoroutinefunction(wrapped)
    assert await wrapped("a") == await _identity("a")

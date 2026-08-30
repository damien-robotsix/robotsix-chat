"""Shared utilities for LLM-facing tool functions.

Every tool in this package is a callable (``async def`` or plain ``def``)
that returns ``str`` — both on success and on error.  This module provides a
lightweight wrapper that validates required arguments **before** the
tool body runs, so an empty/missing required arg never silently
produces a confusing downstream error (or worse, an empty result).

Usage::

    from robotsix_chat.llm.tool_utils import require_args

    @require_args
    async def my_tool(path: str, name: str = "") -> str:
        ...

    # Or wrap an already-built list at the assembly point:
    tools = [require_args(t) for t in raw_tools]
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast, overload

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@overload
def require_args(
    fn: Callable[..., Awaitable[str]],
) -> Callable[..., Awaitable[str]]: ...


@overload
def require_args(fn: Callable[..., str]) -> Callable[..., str]: ...


def require_args(
    fn: Callable[..., Awaitable[str] | str],
) -> Callable[..., Awaitable[str] | str]:
    """Wrap *fn* so empty required arguments produce a clear error string.

    Only **required** parameters (those without a default) are checked.
    Optional parameters (those with a default value) are left alone —
    ``None`` or an empty string is a legitimate "not supplied" sentinel
    for many tools.
    """
    sig = inspect.signature(fn)
    required: list[str] = [
        name
        for name, param in sig.parameters.items()
        if param.default is inspect.Parameter.empty
    ]

    # Fast path: nothing to check.
    if not required:
        return fn

    def _missing(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        for name in required:
            value = bound.arguments.get(name)
            if value is None or (isinstance(value, str) and value == ""):
                return f"Error: {fn.__name__} requires a non-empty {name!r} argument."
        return None

    # Preserve the wrapped tool's sync/async nature. A sync tool (e.g.
    # ``read_skill``) wrapped in an ``async def`` would be awaited by the
    # keyed-provider (pydantic-ai) path and blow up with
    # ``TypeError: 'str' object can't be awaited`` — which is what killed
    # every turn served by the usage-exhausted fallback tier.
    if not inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> str:
            err = _missing(args, kwargs)
            if err is not None:
                return err
            return cast("str", fn(*args, **kwargs))

        return sync_wrapper

    afn = cast("Callable[..., Awaitable[str]]", fn)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        err = _missing(args, kwargs)
        if err is not None:
            return err
        return await afn(*args, **kwargs)

    return wrapper

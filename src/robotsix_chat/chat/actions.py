"""Per-turn actions log — what the agent *did*, not only what it said.

A persisted conversation turn is a ``(user_message, assistant_reply)`` pair.
The steps the assistant performed in between — tickets filed, PRs merged,
subsessions spawned, components restarted, files written — live only in the
tool calls of that run, which the reply usually paraphrases at best.  Any
later summariser (idle-timeout compaction, carryover) therefore cannot
recover them from the transcript alone.

This module captures those steps as a compact, plain-text list of
*action entries* — one per tool call, ``tool_name(identifying args) -> one
line of result`` — so the chat route can persist them next to the reply
(see :meth:`~robotsix_chat.chat.conversation.ConversationStore.record`) and
the transcript builder can render them under each assistant turn.

Two sources feed the log, covering both transports:

* **Activity events** — the live ``tool_call`` / ``tool_result`` signals the
  Claude Agent SDK streaming loop emits (see
  :func:`robotsix_llmio.claude_sdk.activity_events`).  This is the only
  source for the keyless SDK tiers, whose run result carries no message
  parts.
* **pydantic-ai messages** — ``result.all_messages()`` on the keyed
  OpenRouter tiers, whose ``ToolCallPart`` / ``ToolReturnPart`` pairs carry
  the full arguments and return values.

The collector is a :class:`contextvars.ContextVar` so the route wraps the
agent run in :func:`collect_actions` and the agent appends without any
signature change to the ``ChatAgent`` protocol (which only yields text).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
from collections.abc import Iterable, Iterator
from typing import Any

__all__ = [
    "MAX_ACTION_ENTRIES",
    "MAX_ACTION_ENTRY_CHARS",
    "actions_from_messages",
    "collect_actions",
    "current_actions",
    "format_action",
    "record_action",
]

#: Cap on entries kept per turn — a runaway tool loop must not bloat the
#: persisted session file.  The tail is replaced by a ``… (+N more)`` marker.
MAX_ACTION_ENTRIES = 40
#: Cap on the length of one rendered entry (args + result), in characters.
MAX_ACTION_ENTRY_CHARS = 240
#: Characters kept from the tool arguments / result when rendering an entry.
_ARGS_CHARS = 120
_RESULT_CHARS = 100

#: Tool names that are bookkeeping, not steps worth summarising.
_IGNORED_TOOLS = frozenset({"recall_memory"})

_current_actions: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "_current_actions", default=None
)


@contextlib.contextmanager
def collect_actions() -> Iterator[list[str]]:
    """Collect action entries recorded during the ``with`` block.

    Yields the list the entries are appended to; it is complete once the
    block exits.  Nested use shadows the outer collector for the block.
    """
    entries: list[str] = []
    token = _current_actions.set(entries)
    try:
        yield entries
    finally:
        _current_actions.reset(token)


def current_actions() -> list[str] | None:
    """Return the active collector, or ``None`` when nobody is collecting."""
    return _current_actions.get()


def _short(value: object, limit: int) -> str:
    """Render *value* on one line, truncated to *limit* characters."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError, ValueError:
            text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def format_action(
    tool_name: str,
    args: object = "",
    result: object = "",
    *,
    is_error: bool = False,
) -> str:
    """Render one action entry: ``tool(args) -> result``.

    *args* and *result* may be strings or JSON-serialisable objects; each is
    flattened to one line and truncated so the entry stays compact.  The
    result part is omitted when empty; ``is_error`` prefixes it with
    ``ERROR``.
    """
    args_text = _short(args, _ARGS_CHARS) if args not in ("", None) else ""
    entry = f"{tool_name}({args_text})"
    result_text = _short(result, _RESULT_CHARS) if result not in ("", None) else ""
    if is_error:
        result_text = f"ERROR {result_text}".rstrip()
    if result_text:
        entry = f"{entry} -> {result_text}"
    if len(entry) > MAX_ACTION_ENTRY_CHARS:
        entry = entry[: MAX_ACTION_ENTRY_CHARS - 1].rstrip() + "…"
    return entry


def record_action(
    tool_name: str,
    args: object = "",
    result: object = "",
    *,
    is_error: bool = False,
    entries: list[str] | None = None,
) -> None:
    """Append one action entry to *entries* (default: the active collector).

    A no-op when nothing is collecting or *tool_name* is bookkeeping (memory
    recall).  Once :data:`MAX_ACTION_ENTRIES` is reached, further entries
    are folded into a single trailing ``… (+N more actions)`` marker.
    """
    target = entries if entries is not None else _current_actions.get()
    if target is None or not tool_name or tool_name in _IGNORED_TOOLS:
        return
    if len(target) >= MAX_ACTION_ENTRIES:
        overflow_marker = target[-1]
        if overflow_marker.startswith("… (+"):
            count = int(overflow_marker[4:].split(" ", 1)[0]) + 1
            target[-1] = f"… (+{count} more actions)"
        else:
            target.append("… (+1 more actions)")
        return
    target.append(format_action(tool_name, args, result, is_error=is_error))


def _part_kind(part: Any) -> str:
    """Return a pydantic-ai part's ``part_kind`` (or the class name)."""
    kind = getattr(part, "part_kind", None)
    return str(kind) if kind else type(part).__name__


def actions_from_messages(messages: Iterable[Any]) -> list[str]:
    """Extract action entries from pydantic-ai ``all_messages()``.

    Pairs each ``ToolCallPart`` with the ``ToolReturnPart`` (or
    ``RetryPromptPart``, rendered as an error) carrying the same
    ``tool_call_id``.  Returns ``[]`` for a run without tool calls or for a
    result whose messages carry no recognisable parts (the SDK tiers).
    Never raises.
    """
    calls: list[tuple[str, str, object]] = []
    returns: dict[str, tuple[object, bool]] = {}
    try:
        for message in messages:
            for part in getattr(message, "parts", []) or []:
                kind = _part_kind(part)
                if kind == "tool-call":
                    calls.append(
                        (
                            str(getattr(part, "tool_call_id", "") or ""),
                            str(getattr(part, "tool_name", "?") or "?"),
                            getattr(part, "args", ""),
                        )
                    )
                elif kind == "tool-return":
                    returns[str(getattr(part, "tool_call_id", "") or "")] = (
                        getattr(part, "content", ""),
                        False,
                    )
                elif kind == "retry-prompt":
                    returns[str(getattr(part, "tool_call_id", "") or "")] = (
                        getattr(part, "content", ""),
                        True,
                    )
    except Exception:
        return []

    entries: list[str] = []
    for call_id, tool_name, args in calls:
        result, is_error = returns.get(call_id, ("", False))
        record_action(tool_name, args, result, is_error=is_error, entries=entries)
    return entries

"""Prompt and turn-input helpers for subsession workers.

Builds the ancestor-context preamble for nested subsessions, detects an
operator picking an offered suggestion, and renders inbox messages into the
next turn's input.
"""

from __future__ import annotations

import re

from .models import (
    InboxMessage,
    SubsessionInfo,
    SubsessionKind,
)
from .registry import SubsessionRegistry

# Character budget for the ancestor-context block prepended to a nested
# child's prompt when ``inherit_context=True``.  The budget covers the
# block header plus each ancestor (title + first 300 chars of its prompt);
# ancestors beyond the budget are silently dropped so the block never
# overwhelms the child's own instructions.
_MAX_ANCESTOR_CONTEXT_CHARS = 2000


def _build_ancestor_context(registry: SubsessionRegistry, parent_id: str) -> str:
    """Walk up the parent chain and build a compact context block.

    Returns a string of the form::

        # Ancestor context (inherited from the subsession tree above you)

        ## ancestor-1 title
        ancestor-1 prompt summary …

        ## ancestor-2 title
        ...

    or an empty string when the parent chain is unreachable.
    """
    ancestors: list[SubsessionInfo] = []
    current_id: str | None = parent_id
    while current_id is not None:
        info = registry.get(current_id)
        if info is None:
            break
        ancestors.append(info)
        current_id = info.parent_id
    if not ancestors:
        return ""

    # Build from root downward (reverse the walk-up order).
    ancestors.reverse()
    parts: list[str] = [
        "# Ancestor context (inherited from the subsession tree above you)\n"
    ]
    budget = _MAX_ANCESTOR_CONTEXT_CHARS - len(parts[0])
    for info in ancestors:
        snippet = info.prompt[:300]
        entry = f"## {info.title}\n{snippet}"
        if len(entry) > budget:
            break
        parts.append(entry)
        budget -= len(entry) + 1  # +1 for the blank line separator
    if not parts[1:]:  # only the header, no actual ancestor entries
        return ""
    return "\n\n".join(parts) + "\n\n"


_SUGGESTIONS_BLOCK_RE = re.compile(r"```suggestions\s*\n(.*?)```", re.DOTALL)


def _offered_suggestions(info: SubsessionInfo) -> list[str]:
    """Return the option lines of the panel's most recent ```suggestions block."""
    for entry in reversed(info.transcript):
        if entry.role != "assistant":
            continue
        blocks = _SUGGESTIONS_BLOCK_RE.findall(entry.text or "")
        if not blocks:
            return []
        return [ln.strip() for ln in blocks[-1].splitlines() if ln.strip()]
    return []


def _operator_picked_suggestion(
    info: SubsessionInfo, pending: list[InboxMessage]
) -> bool:
    """Return True when a user_chat operator reply equals an offered option.

    A clicked ```suggestions line is a settled decision: the worker appends
    :data:`USER_CHAT_SETTLED_NOTE` so the panel completes itself instead of
    waiting for the operator to close it by hand (operator report 2026-09-08).
    """
    if info.kind is not SubsessionKind.USER_CHAT:
        return False
    offered = {o.casefold() for o in _offered_suggestions(info)}
    if not offered:
        return False
    return any(
        m.role == "user" and (m.text or "").strip().casefold() in offered
        for m in pending
    )


def _render_turn_input(messages: list[InboxMessage]) -> str:
    """Merge an inbox batch into one turn input, labelled by role."""
    if len(messages) == 1:
        return messages[0].text
    return "\n\n".join(f"[{m.role}] {m.text}" for m in messages)

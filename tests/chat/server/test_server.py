"""Shared test helpers for the chat SSE server test modules."""

from __future__ import annotations

import json
from typing import Any

from robotsix_chat.subsessions import (
    SubsessionInfo,
    SubsessionKind,
    SubsessionRegistry,
)


def _register_subsession(
    registry: SubsessionRegistry,
    *,
    owner: str,
    kind: SubsessionKind = SubsessionKind.TASK,
    title: str = "job",
    **overrides: object,
) -> SubsessionInfo:
    """Register an active subsession record directly (no worker task)."""
    return registry.create(
        kind=kind,
        owner_session_id=owner,
        parent_id=None,
        depth=1,
        title=title,
        prompt="do the thing",
        model_level=3,
        **overrides,  # type: ignore[arg-type]
    )


def _parse_sse(response: Any) -> list[dict[str, object]]:
    """Split SSE text into parsed JSON frames from ``data:`` lines."""
    text: str = response.text
    events = [e for e in text.split("\n\n") if e]
    frames: list[dict[str, object]] = []
    for e in events:
        if e.startswith("data: "):
            frames.append(json.loads(e[len("data: ") :]))
    return frames

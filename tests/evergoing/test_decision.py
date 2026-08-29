"""Unit tests for the subject-aware trim decision parser + agent call."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from robotsix_chat.evergoing.decision import (
    build_trim_prompt,
    decide_trim,
    parse_trim_decision,
)


def test_parse_subject_changed_with_drop() -> None:
    decision = parse_trim_decision("SUBJECT_CHANGED: yes\nDROP_LEADING: 2", max_drop=5)
    assert decision.subject_changed is True
    assert decision.drop_leading == 2


def test_parse_subject_unchanged_forces_zero_drop() -> None:
    # Even if the model emits a positive DROP_LEADING, an unchanged subject
    # must never drop turns.
    decision = parse_trim_decision("SUBJECT_CHANGED: no\nDROP_LEADING: 4", max_drop=5)
    assert decision.subject_changed is False
    assert decision.drop_leading == 0


def test_parse_clamps_drop_to_max() -> None:
    decision = parse_trim_decision("SUBJECT_CHANGED: yes\nDROP_LEADING: 99", max_drop=3)
    assert decision.drop_leading == 3


def test_parse_garbled_reply_is_conservative() -> None:
    decision = parse_trim_decision("I am not sure what you mean.", max_drop=5)
    assert decision.subject_changed is False
    assert decision.drop_leading == 0


def test_build_prompt_mentions_counts() -> None:
    prompt = build_trim_prompt("User: hi", visible_count=4, max_drop=2)
    assert "4 turns" in prompt
    assert "at most 2" in prompt
    assert "User: hi" in prompt


class _ReplyAgent:
    """Minimal ChatAgent whose ``stream`` yields a canned reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
        trace_metadata: dict[str, str] | None = None,
        trace_name: str | None = None,
        model_level: int | None = None,
    ) -> AsyncIterator[str]:
        yield self.reply


class _RaisingAgent:
    """ChatAgent whose ``stream`` raises to exercise the safe fallback."""

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
        trace_metadata: dict[str, str] | None = None,
        trace_name: str | None = None,
        model_level: int | None = None,
    ) -> AsyncIterator[str]:
        raise RuntimeError("boom")
        yield ""  # pragma: no cover - unreachable, makes this an async gen


def test_decide_trim_parses_agent_reply() -> None:
    agent = _ReplyAgent("SUBJECT_CHANGED: yes\nDROP_LEADING: 1")
    decision = asyncio.run(decide_trim(agent, "User: a", visible_count=3, max_drop=1))
    assert decision.subject_changed is True
    assert decision.drop_leading == 1


def test_decide_trim_swallows_agent_error() -> None:
    decision = asyncio.run(
        decide_trim(_RaisingAgent(), "User: a", visible_count=3, max_drop=1)
    )
    assert decision.subject_changed is False
    assert decision.drop_leading == 0
    assert decision.reason == "decision failed"

"""Tests for the compaction summariser (:mod:`robotsix_chat.chat.summarize`)."""

from __future__ import annotations

import pytest

from robotsix_chat.chat.summarize import (
    MAX_CHUNKS,
    SUMMARY_SECTIONS,
    SUMMARY_SYSTEM_PROMPT,
    build_idle_summary_prompt,
    build_merge_prompt,
    build_transcript,
    chunk_turns,
    generate_idle_summary,
)
from robotsix_chat.config import Settings


def test_summary_system_prompt_is_a_dedicated_summariser_prompt() -> None:
    """The summariser prompt is not the chat prompt and carries no tool roles."""
    assert SUMMARY_SYSTEM_PROMPT.startswith("You are a conversation summarizer")
    chat_prompt = Settings().agent_instruction
    assert chat_prompt != SUMMARY_SYSTEM_PROMPT
    assert "helpful assistant" not in SUMMARY_SYSTEM_PROMPT
    assert "spawn_subsession" not in SUMMARY_SYSTEM_PROMPT
    assert "[actions]" in SUMMARY_SYSTEM_PROMPT
    # It must not continue or echo the conversation.
    assert "Never echo" in SUMMARY_SYSTEM_PROMPT
    assert "Never answer, continue" in SUMMARY_SYSTEM_PROMPT


def test_idle_summary_prompt_has_all_sections_and_no_brief_instruction() -> None:
    """Every section header is requested; the old 'brief' target is gone."""
    prompt = build_idle_summary_prompt("User: hi\nAssistant: hello")
    for name in SUMMARY_SECTIONS:
        assert f"## {name}" in prompt
    assert list(SUMMARY_SECTIONS) == [
        "Goal / context",
        "What was done",
        "Current state / evidence",
        "Pending confirmations",
        "Agreed next steps / open questions",
        "Verbatim plan",
    ]
    assert "brief" not in prompt.lower()
    assert "300-700 words" in prompt
    assert "Do not echo the assistant's last reply" in prompt
    assert "Do not invent identifiers" in prompt
    assert "NOT verified" in prompt
    assert prompt.rstrip().endswith("Summary:")
    assert "User: hi\nAssistant: hello" in prompt


def test_idle_summary_prompt_window_note_is_prepended() -> None:
    """A map-phase prompt starts with the part-N-of-M note."""
    prompt = build_idle_summary_prompt("User: x", window_note="This is part 2 of 3")
    assert prompt.startswith("This is part 2 of 3")


def test_merge_prompt_lists_parts_and_sections() -> None:
    """The reduce prompt carries every partial and the same section headers."""
    prompt = build_merge_prompt(["part one text", "part two text"])
    assert "=== Part 1 of 2 ===\npart one text" in prompt
    assert "=== Part 2 of 2 ===\npart two text" in prompt
    for name in SUMMARY_SECTIONS:
        assert f"## {name}" in prompt
    assert "chronological" in prompt


def test_build_transcript_renders_actions_under_assistant_turn() -> None:
    """Per-turn actions appear as an ``[actions]`` block after the reply."""
    turns = [("file it", "Filed."), ("thanks", "np")]
    actions = [["create_ticket(Volume file-write) -> T-04d8"], []]
    text = build_transcript(turns, actions=actions)
    assert text == (
        "User: file it\n"
        "Assistant: Filed.\n"
        "[actions]\n"
        "  - create_ticket(Volume file-write) -> T-04d8\n"
        "User: thanks\n"
        "Assistant: np"
    )


def test_build_transcript_without_actions_is_unchanged() -> None:
    """The legacy call shape (no actions) renders exactly as before."""
    assert build_transcript([("q", "a")]) == "User: q\nAssistant: a"
    assert "[actions]" not in build_transcript([("q", "a")], actions=[[]])
    long_reply = "x" * 3000
    assert build_transcript(
        [("q", long_reply)], max_len=10
    ) == "User: q\nAssistant: " + ("x" * 10 + "…")


def test_chunk_turns_splits_on_size_and_keeps_alignment() -> None:
    """Windows hold whole turns; each window stays aligned with its actions."""
    turns = [(f"q{i}", "a" * 400) for i in range(10)]
    actions = [[f"tool({i})"] for i in range(10)]
    windows = chunk_turns(turns, actions, max_chars=1000)
    assert len(windows) > 1
    flat_turns = [t for w_turns, _ in windows for t in w_turns]
    flat_actions = [a for _, w_actions in windows for a in w_actions]
    assert flat_turns == turns
    assert flat_actions == actions
    for w_turns, w_actions in windows:
        assert len(w_turns) == len(w_actions)
        assert len(w_turns) <= 2  # ~430 chars per turn against a 1000 budget


def test_chunk_turns_single_window_for_short_input_and_empty() -> None:
    """A short conversation is one window; no turns means no windows."""
    assert chunk_turns([]) == []
    windows = chunk_turns([("q", "a"), ("q2", "a2")])
    assert len(windows) == 1
    assert windows[0][1] == [[], []]


@pytest.mark.asyncio
async def test_generate_idle_summary_single_call_for_short_transcript() -> None:
    """A transcript within budget takes exactly one summariser call."""
    prompts: list[str] = []

    async def run(prompt: str) -> str:
        prompts.append(prompt)
        return "## Goal / context\nok"

    turns = [("file the ticket", "Filed T-04d8.")]
    out = await generate_idle_summary(run, turns, [["create_ticket() -> T-04d8"]])
    assert out == "## Goal / context\nok"
    assert len(prompts) == 1
    assert "[actions]" in prompts[0]
    assert "create_ticket() -> T-04d8" in prompts[0]
    assert "This is part" not in prompts[0]


@pytest.mark.asyncio
async def test_generate_idle_summary_map_reduces_long_transcript() -> None:
    """A long transcript is summarised per window, then merged."""
    prompts: list[str] = []

    async def run(prompt: str) -> str:
        prompts.append(prompt)
        if prompt.startswith("Below are structured summaries"):
            return "MERGED"
        return f"PARTIAL-{len(prompts)}"

    turns = [(f"q{i}", "a" * 500) for i in range(12)]
    out = await generate_idle_summary(run, turns, max_chunk_chars=1500)
    assert out == "MERGED"
    map_prompts = [p for p in prompts if p.startswith("This is part")]
    assert len(map_prompts) >= 2
    assert len(prompts) == len(map_prompts) + 1
    merge_prompt = prompts[-1]
    for i in range(1, len(map_prompts) + 1):
        assert f"PARTIAL-{i}" in merge_prompt
    # Every turn is covered by exactly one window.
    assert sum(p.count("User: q") for p in map_prompts) == 12


@pytest.mark.asyncio
async def test_generate_idle_summary_caps_windows() -> None:
    """Beyond MAX_CHUNKS the oldest windows collapse into an omission marker."""
    prompts: list[str] = []

    async def run(prompt: str) -> str:
        prompts.append(prompt)
        return "p"

    turns = [(f"q{i}", "a" * 500) for i in range(MAX_CHUNKS * 3)]
    await generate_idle_summary(run, turns, max_chunk_chars=600)
    map_prompts = [p for p in prompts if p.startswith("This is part")]
    assert len(map_prompts) == MAX_CHUNKS
    assert "earlier turns omitted" in map_prompts[0]


@pytest.mark.asyncio
async def test_generate_idle_summary_partial_failures_are_marked() -> None:
    """One failed window is marked lost; all failed windows yield ""."""
    calls = 0

    async def flaky(prompt: str) -> str:
        nonlocal calls
        calls += 1
        if prompt.startswith("Below are structured summaries"):
            return "MERGED"
        return "" if calls == 1 else "ok"

    turns = [(f"q{i}", "a" * 500) for i in range(6)]
    assert await generate_idle_summary(flaky, turns, max_chunk_chars=1500) == "MERGED"

    async def dead(prompt: str) -> str:
        return ""

    assert await generate_idle_summary(dead, turns, max_chunk_chars=1500) == ""
    assert await generate_idle_summary(dead, []) == ""


def test_summary_model_level_defaults_to_level_one() -> None:
    """The summariser runs on the cheap/frequent level by default."""
    assert Settings().summary_model_level == 1

"""Idle-timeout compaction summary — prompt, chunking and map-reduce driver.

Why this module exists (Langfuse ``robotsix-chat``, 2026-08-30): a compaction
summary run was handed 65k characters of transcript on the cheapest tier
under the *full chat system prompt* and produced 142 tokens — a verbatim echo
of the assistant's last reply.  Three things went wrong at once:

* the summary agent behaved as the chat assistant (its system prompt was
  the chat one) and "continued the conversation" instead of summarising;
* the transcript held only ``(user, assistant)`` pairs — none of the steps
  the assistant performed (tickets filed, PRs merged, subsessions spawned)
  were visible to the summariser;
* the prompt asked for "brief" prose with no structure and no size floor.

This module fixes the prompt side: a dedicated summariser system prompt
(:data:`SUMMARY_SYSTEM_PROMPT`), a structured section target
(:data:`SUMMARY_SECTIONS`), a per-turn ``[actions]`` rendering of the
persisted actions log (see :mod:`robotsix_chat.chat.actions`), and a bounded
map-reduce over long transcripts so the model is never handed more than
:data:`DEFAULT_CHUNK_CHARS` of conversation at once.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence

__all__ = [
    "DEFAULT_CHUNK_CHARS",
    "SUMMARY_SECTIONS",
    "SUMMARY_SYSTEM_PROMPT",
    "build_idle_summary_prompt",
    "build_merge_prompt",
    "build_transcript",
    "chunk_turns",
    "generate_idle_summary",
]

logger = logging.getLogger(__name__)

#: Upper bound on the transcript characters handed to one summariser call.
#: ~6k tokens of conversation — comfortably inside every tier's context while
#: leaving room for the structured output.  Longer transcripts are split into
#: windows of whole turns and summarised map-reduce style.
DEFAULT_CHUNK_CHARS = 24_000

#: Hard cap on the number of windows — beyond this the oldest windows are
#: folded into a single marker rather than summarised, so a pathological
#: session cannot fan out into dozens of model calls.
MAX_CHUNKS = 8

#: Dedicated system prompt for the summariser agent.  Deliberately free of any
#: tool, role or workflow instruction: the model's only job is to condense a
#: transcript it is *shown*, never to act on it or continue it.
SUMMARY_SYSTEM_PROMPT = (
    "You are a conversation summarizer for an autonomous engineering "
    "assistant that operates a software fleet (tickets, pull requests, "
    "deployments, background subsessions).\n"
    "\n"
    "You will be shown a transcript of a conversation between an operator "
    "(User) and the assistant (Assistant). Under some assistant turns an "
    "`[actions]` block lists the tool calls the assistant actually made "
    "during that turn — tool name, identifying arguments, one-line result. "
    "Those blocks are the authoritative record of what was DONE; the "
    "assistant's prose is only what it SAID.\n"
    "\n"
    "Your output is read by the assistant's future self after its context is "
    "reset, so it must be able to resume the work from your summary alone. "
    "Rules:\n"
    "- Write a summary. Never answer, continue, or role-play the "
    "conversation, and never address the operator.\n"
    "- Never echo or paraphrase the assistant's last reply as the summary; "
    "cover the whole transcript.\n"
    "- Reproduce every identifier verbatim (ticket ids, PR URLs and numbers, "
    "subsession ids, task ids, file paths, commands, HTTP status codes, "
    "message uids). Never abbreviate, paraphrase, or invent one — an "
    "identifier that does not appear in the transcript does not exist.\n"
    "- Distinguish verified facts (a tool result or the operator confirmed "
    "it) from claims and assumptions; when something was stated but not "
    "verified, say so explicitly.\n"
    "- Plain text with the requested section headers; no code fences, no "
    "JSON."
)

#: Section headers the compaction summary must carry, in order.
SUMMARY_SECTIONS: tuple[str, ...] = (
    "Goal / context",
    "What was done",
    "Current state / evidence",
    "Pending confirmations",
    "Agreed next steps / open questions",
    "Verbatim plan",
)

_SECTION_GUIDE = (
    "Goal / context — what the operator is trying to achieve and any "
    "constraints or decisions that frame the work.\n"
    "What was done — chronological, one bullet per step the assistant "
    "performed (use the [actions] blocks), each with its exact identifiers: "
    "ticket ids, PR URLs, subsession ids, file paths, commands, HTTP codes.\n"
    "Current state / evidence — where things stand now; for each fact say "
    "whether it was VERIFIED (tool result / operator confirmation) or only "
    "ASSUMED / claimed. State explicitly when a fact was NOT verified.\n"
    "Pending confirmations — proposals awaiting the operator's approval and "
    "exactly what is being confirmed.\n"
    "Agreed next steps / open questions — the concrete actions the assistant "
    "said it would take next, plus unanswered questions.\n"
    "Verbatim plan — if the assistant laid out a multi-item plan (especially "
    "one with per-item identifiers and decisions), reproduce it as a "
    "verbatim block; otherwise write 'none'."
)

_LENGTH_GUIDE = (
    "Target 300-700 words. Never shorter than the material warrants: a long "
    "session with many steps needs every step listed. Do not pad an empty "
    "section — write 'none' under it."
)


def build_transcript(
    turns: Iterable[tuple[str, str]],
    *,
    max_len: int = 2000,
    actions: Sequence[Sequence[str]] | None = None,
) -> str:
    """Build a compact conversation transcript from (user, assistant) pairs.

    Assistant replies longer than *max_len* are truncated with an ellipsis.
    *actions*, when given, is aligned with *turns*: the i-th entry lists the
    action-log lines of the i-th turn (see :mod:`robotsix_chat.chat.actions`)
    and is rendered under that assistant turn as an ``[actions]`` block so a
    summariser sees what was done, not only what was said.
    """
    parts: list[str] = []
    for idx, (user_msg, asst_msg) in enumerate(turns):
        parts.append(f"User: {user_msg}")
        if asst_msg:
            truncated = (
                asst_msg[:max_len] + "\u2026" if len(asst_msg) > max_len else asst_msg
            )
            parts.append(f"Assistant: {truncated}")
        turn_actions = (
            actions[idx] if actions is not None and idx < len(actions) else ()
        )
        if turn_actions:
            parts.append("[actions]")
            parts.extend(f"  - {entry}" for entry in turn_actions)
    return "\n".join(parts)


def build_idle_summary_prompt(transcript: str, *, window_note: str = "") -> str:
    """Return the user prompt asking for the structured compaction summary.

    *window_note* is prepended when *transcript* is one window of a longer
    conversation, so the model knows it is summarising a slice.
    """
    prefix = f"{window_note}\n\n" if window_note else ""
    return (
        f"{prefix}"
        "Summarize the conversation below for the assistant's future self. "
        "Use exactly these section headers, in this order, each on its own "
        "line:\n"
        + "\n".join(f"## {name}" for name in SUMMARY_SECTIONS)
        + "\n\nWhat goes under each header:\n"
        f"{_SECTION_GUIDE}\n\n"
        f"{_LENGTH_GUIDE} Do not echo the assistant's last reply. Do not invent "
        "identifiers that do not appear in the conversation.\n\n"
        "Conversation:\n"
        f"{transcript}\n\n"
        "Summary:"
    )


def build_merge_prompt(partials: Sequence[str]) -> str:
    """Return the reduce prompt merging per-window summaries into one."""
    blocks = "\n\n".join(
        f"=== Part {i} of {len(partials)} ===\n{text}"
        for i, text in enumerate(partials, start=1)
    )
    return (
        "Below are structured summaries of consecutive parts of one long "
        "conversation, oldest first. Merge them into ONE summary with exactly "
        "these section headers, in this order, each on its own line:\n"
        + "\n".join(f"## {name}" for name in SUMMARY_SECTIONS)
        + "\n\nKeep 'What was done' chronological across all parts and keep "
        "every identifier verbatim (drop nothing that a later part did not "
        "supersede). Where a later part resolves a pending item from an "
        "earlier one, record the resolution instead of the pending state. "
        f"Preserve the VERIFIED / NOT verified distinction. {_LENGTH_GUIDE}\n\n"
        f"{blocks}\n\n"
        "Merged summary:"
    )


def _turn_size(turn: tuple[str, str], actions: Sequence[str]) -> int:
    """Approximate rendered size of one turn in the transcript."""
    user_msg, asst_msg = turn
    return (
        len(user_msg) + min(len(asst_msg), 2000) + sum(len(a) + 1 for a in actions) + 32
    )


def chunk_turns(
    turns: Sequence[tuple[str, str]],
    actions: Sequence[Sequence[str]] | None = None,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
) -> list[tuple[list[tuple[str, str]], list[list[str]]]]:
    """Split *turns* (with aligned *actions*) into windows of whole turns.

    Each window renders to at most *max_chars* (a single oversized turn
    still forms its own window — turns are never split).  Returns a list of
    ``(turns, actions)`` pairs; empty input gives ``[]``.
    """
    acts: list[list[str]] = [
        list(actions[i]) if actions is not None and i < len(actions) else []
        for i in range(len(turns))
    ]
    windows: list[tuple[list[tuple[str, str]], list[list[str]]]] = []
    cur_turns: list[tuple[str, str]] = []
    cur_acts: list[list[str]] = []
    cur_size = 0
    for turn, turn_acts in zip(turns, acts, strict=True):
        size = _turn_size(turn, turn_acts)
        if cur_turns and cur_size + size > max_chars:
            windows.append((cur_turns, cur_acts))
            cur_turns, cur_acts, cur_size = [], [], 0
        cur_turns.append(turn)
        cur_acts.append(turn_acts)
        cur_size += size
    if cur_turns:
        windows.append((cur_turns, cur_acts))
    return windows


async def generate_idle_summary(
    run: Callable[[str], Awaitable[str]],
    turns: Sequence[tuple[str, str]],
    actions: Sequence[Sequence[str]] | None = None,
    *,
    max_chunk_chars: int = DEFAULT_CHUNK_CHARS,
) -> str:
    """Produce the structured compaction summary for *turns*.

    *run* executes one summariser call (prompt → text; ``""`` on failure).
    Short conversations take a single call.  Longer ones are split with
    :func:`chunk_turns`, each window summarised on its own (map), then the
    partial summaries are merged (reduce).  If every window fails the
    result is ``""``; if only some fail, the surviving partials are merged
    and the gap is marked so the future self knows a slice is missing.
    Returns ``""`` for an empty *turns*.
    """
    if not turns:
        return ""

    windows = chunk_turns(turns, actions, max_chars=max_chunk_chars)
    if len(windows) > MAX_CHUNKS:
        dropped = len(windows) - MAX_CHUNKS
        dropped_turns = sum(len(w[0]) for w in windows[:dropped])
        windows = windows[dropped:]
        marker_turn = (
            "",
            f"[{dropped_turns} earlier turns omitted from this summary window]",
        )
        windows[0] = ([marker_turn, *windows[0][0]], [[], *windows[0][1]])
        logger.warning(
            "Compaction transcript spans %d windows — summarising only the "
            "last %d (%d turns folded into an omission marker)",
            dropped + MAX_CHUNKS,
            MAX_CHUNKS,
            dropped_turns,
        )

    if len(windows) == 1:
        w_turns, w_actions = windows[0]
        return await run(
            build_idle_summary_prompt(build_transcript(w_turns, actions=w_actions))
        )

    partials: list[str] = []
    for idx, (w_turns, w_actions) in enumerate(windows, start=1):
        note = (
            f"This is part {idx} of {len(windows)} of a longer conversation; "
            "summarise only this part (later parts are summarised separately "
            "and merged afterwards)."
        )
        text = await run(
            build_idle_summary_prompt(
                build_transcript(w_turns, actions=w_actions), window_note=note
            )
        )
        partials.append(
            text or f"[Part {idx}: summary unavailable — {len(w_turns)} turns lost]"
        )
    if all(p.startswith("[Part ") for p in partials):
        return ""
    return await run(build_merge_prompt(partials))

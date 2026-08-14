"""Progressive disclosure for skill prompts.

Skill bodies used to be concatenated into the system prompt in full — all of
them, on every turn, whether or not the turn had anything to do with them.
That cost tokens on every request and, because the Claude Agent SDK spawns the
CLI as a subprocess and passes the system prompt as a single ``--system-prompt``
argv element, it also ran into a hard kernel ceiling: ``MAX_ARG_STRLEN``
(``PAGE_SIZE * 32`` = 128 KiB on x86-64) caps one argument, and ``execve``
answers ``E2BIG`` above it. On 2026-08-13 the bundled skills reached 85 KB and
autonomous sessions — which add their own preamble on top — crossed the line
and failed to start at every restart.

This module replaces that with an index: each skill contributes its title and
opening paragraph, and the agent reads a full body on demand via the
``read_skill`` tool. The index is a few KB regardless of how many skills exist,
so adding a skill no longer moves the prompt toward the ceiling.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Per-skill summary budget in the index. Enough for a title plus an opening
#: paragraph, which is what a skill needs to advertise *when* it applies —
#: the body carries the how.
_SUMMARY_CHARS = 320


def summarize_skill(body: str, *, max_chars: int = _SUMMARY_CHARS) -> tuple[str, str]:
    """Return ``(title, summary)`` for a skill body.

    Skills open with a Markdown ``# Title`` followed by a paragraph saying what
    the skill is for. That opening is exactly the routing information the index
    needs, so it is taken verbatim rather than re-summarised.

    Falls back gracefully: a body with no heading yields an empty title, and a
    body with no blank-line paragraph break uses whatever text precedes the
    first horizontal rule or the truncation budget.
    """
    title = ""
    lines = body.lstrip().splitlines()
    start = 0
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        start = 1

    para: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if stripped.startswith(("---", "#", "|", "```")):
            if para:
                break
            continue
        para.append(stripped)

    summary = " ".join(para)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    return title, summary


def build_skill_index(
    entries: list[tuple[str, Callable[[], str]]],
    *,
    max_chars: int = _SUMMARY_CHARS,
) -> str:
    """Build the system-prompt index for *entries* (``(name, loader)`` pairs).

    Returns an empty string when no entry yields a body so the caller can
    append unconditionally.

    """
    rows: list[str] = []
    for name, loader in entries:
        try:
            body = loader()
        except Exception:
            logger.warning(
                "skill %s failed to load; omitted from index", name, exc_info=True
            )
            continue
        if not body:
            continue
        title, summary = summarize_skill(body, max_chars=max_chars)
        label = f"**{name}**" + (f" — {title}" if title else "")
        rows.append(f"- {label}: {summary}" if summary else f"- {label}")

    if not rows:
        return ""

    listing = "\n".join(rows)
    return (
        "# Available skills\n"
        "\n"
        "Each entry below is a capability you have, summarised. The summary "
        "says what the skill covers; the full instructions — endpoints, "
        "arguments, safety rules, worked examples — live in the skill body.\n"
        "\n"
        "**Call `read_skill(name)` to read a body before acting on that "
        "capability.** Read it when a task touches the skill's area, and "
        "before your first use of it in a session. Do not guess an endpoint or "
        "argument from the summary alone — the summary is a router, not a "
        "reference. Once read, a body stays in context for the rest of the "
        "session; there is no need to re-read it.\n"
        "\n"
        f"{listing}\n"
    )

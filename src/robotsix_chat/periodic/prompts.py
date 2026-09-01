"""Shared preamble for periodic session initial prompts.

The preamble is prepended by the scheduler to every preset's
``initial_prompt`` — presets state only their task. It rides in the USER
message of the session's first turn, not in the system prompt: periodic
sessions run the exact same agent, instruction, and code path as an operator
session.
"""

from __future__ import annotations

PERIODIC_PREAMBLE = (
    "You are running a scheduled periodic session. Complete the task below "
    "in this turn: do the work now, then finish with a concise report of "
    "your findings and any actions you took. Do not schedule continuations, "
    "do not wait for future events, and do not leave the task half-done for "
    "a later run — the next scheduled run starts from a fresh session with "
    "no memory of this one beyond what your report says.\n\n"
    "The operator may reply in this session after your report. Such a reply "
    "is a live instruction that supersedes the task's constraints below "
    "(including any read-only framing): resolve it against this session's "
    "own turns first, and act on it as you would in a normal chat.\n\n"
    "---\n\n"
)


def build_initial_message(initial_prompt: str) -> str:
    """Return the first user message for a periodic session."""
    return PERIODIC_PREAMBLE + initial_prompt.strip()

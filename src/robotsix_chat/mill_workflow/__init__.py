"""Mill ticket workflow skill — the operating rules for working the mill board.

Until system-prompt v162 these rules (ticket lifecycle, approval gate, blocked
ticket handling, ticket-id fidelity, merge paths) sat inline in
``Settings.agent_instruction`` and were paid for on every turn of every
session.  They now ship as a skill: the system prompt carries a one-line
pointer and the agent fetches the body with ``read_skill("mill_workflow")``
before its first board action in a session.

Exposes :func:`load_mill_workflow_skill` for the skill registry.
"""

from __future__ import annotations

from pathlib import Path


def load_mill_workflow_skill() -> str:
    """Return the mill-workflow skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string.  Returns an empty string when the file is missing, so a missing
    skill document never prevents the agent from starting.
    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


__all__ = ["load_mill_workflow_skill"]

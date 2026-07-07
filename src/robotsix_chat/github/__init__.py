"""GitHub repository-administration component skill for the agent.

Exposes :func:`load_github_skill` which returns the github component skill
markdown — a description of the github API surface (create repos, set
metadata, register with mill), the confirmation gate required for every
write operation, and the safety rules.  Inject this into the agent's system
prompt so the LLM knows how to use the ``component_request`` tool to reach
the github component.

The github component itself is roster-based: the agent calls it through the
generic ``component_request(component_id="github", ...)`` tool.  This module
provides only the skill documentation — no local tools — because the
github component's implementation and its scoped GitHub token live on the
central-deploy server, never in the chat container.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["load_github_skill"]


def load_github_skill() -> str:
    """Return the github component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.

    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""

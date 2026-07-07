"""GitHub repository management tools for the agent.

Exposes :func:`build_github_tools` — a factory returning LLM tools
for creating and managing GitHub repositories (create, update settings,
read details).  Repo creation is confirmation-gated: the tool requires
``confirmed=True``, and the skill doc instructs the agent to ask the
user before calling it.

Also exposes :func:`load_github_skill` which returns the component skill
markdown — a description of the allowed GitHub operations, safety rules,
and the confirmation requirement for destructive actions.

All operations authenticate via a scoped personal access token from the
deploy EnvStore — the token never appears in the chat container
environment directly.  Returns no tools when disabled, so the chat runs
exactly as before.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import GitHubSettings

__all__ = ["build_github_tools", "load_github_skill"]


def load_github_skill() -> str:
    """Return the GitHub component skill markdown.

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


def build_github_tools(
    settings: GitHubSettings,
) -> list[Callable[..., Any]]:
    """Return the GitHub tool(s) for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    from .client import GitHubClient

    client = GitHubClient(settings)

    async def create_github_repo(
        name: str,
        description: str = "",
        visibility: str = "private",
        *,
        confirmed: bool = False,
    ) -> str:
        """Create a new GitHub repository.

        **Confirmation-gated:** set ``confirmed=True`` ONLY after the user
        has explicitly agreed to create this repository.  Call with
        ``confirmed=False`` first to validate the parameters; the tool
        will return a preview of what would be created.  Once the user
        confirms, call again with ``confirmed=True``.

        Args:
            name: Repository name (e.g. ``"robotsix-chat-mobile"``).
            description: Optional short description shown on the repo.
            visibility: ``"private"`` (default) or ``"public"``.
            confirmed: Must be ``True`` to actually create the repo.

        Returns:
            The created repository's details (full_name, html_url,
            clone_url), or a preview when ``confirmed=False``.

        """
        if not confirmed:
            return (
                "⚠ Confirmation required.  Would create repository:\n"
                f"  name: {name}\n"
                f"  description: {description or '(none)'}\n"
                f"  visibility: {visibility}\n\n"
                "Call again with confirmed=True after the user approves."
            )
        if visibility not in ("private", "public"):
            return "Error: visibility must be 'private' or 'public'"
        return await client.create_repo(name, description, visibility)

    async def update_github_repo(
        owner: str,
        repo: str,
        description: str | None = None,
        visibility: str | None = None,
        has_issues: bool | None = None,
        has_wiki: bool | None = None,
    ) -> str:
        """Update a GitHub repository's settings.

        Only the provided fields are changed — omitted fields are left
        as-is.  This is a low-risk administrative operation; no
        confirmation gate.

        Args:
            owner: Repository owner (user or org name).
            repo: Repository name.
            description: New short description, or ``None`` to leave
                unchanged.
            visibility: ``"private"`` or ``"public"``, or ``None`` to
                leave unchanged.
            has_issues: Enable/disable the issue tracker, or ``None``
                to leave unchanged.
            has_wiki: Enable/disable the wiki, or ``None`` to leave
                unchanged.

        Returns:
            The updated repository's details.

        """
        return await client.update_repo(
            owner, repo, description, visibility, has_issues, has_wiki
        )

    async def get_github_repo(owner: str, repo: str) -> str:
        """Read a GitHub repository's details.

        Returns the repo's full metadata: description, visibility, default
        branch, topics, open issues count, and more.  Read-only — no side
        effects.

        Args:
            owner: Repository owner (user or org name).
            repo: Repository name.

        Returns:
            The repository's details as formatted JSON, or an error
            message.

        """
        return await client.get_repo(owner, repo)

    return [
        create_github_repo,
        update_github_repo,
        get_github_repo,
    ]

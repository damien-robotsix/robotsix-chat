"""GitHub repository security-feature tools for the chat agent.

Exposes :func:`build_github_security_tools` — a factory returning an LLM
tool that lets the agent enable or disable repository-level security
features (dependency graph, advanced security, secret scanning) on repos
under the configured GitHub organisation.

Also exposes :func:`load_github_skill` which returns the component skill
markdown — a description of the ``PATCH /chat/github/repos/{owner}/{repo}/settings``
endpoint, its auth requirements, and its confirmation-gated mutation policy.

Returns no tools when the capability is disabled.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings, GitHubSecuritySettings

__all__ = ["build_github_security_tools", "load_github_skill"]


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


def build_github_security_tools(
    github_security: GitHubSecuritySettings,
    direct_repo: DirectRepoSettings,
) -> list[Callable[..., Any]]:
    """Return GitHub security-feature tool(s) for the agent, or ``[]`` when disabled."""
    if not github_security.enabled:
        return []

    from robotsix_chat.repo.direct.client import DirectRepoClient

    client = DirectRepoClient(direct_repo)
    org = github_security.github_org

    async def set_repo_security_and_analysis(
        repo_name: str,
        dependency_graph: str | None = None,
        advanced_security: str | None = None,
        secret_scanning: str | None = None,
        secret_scanning_push_protection: str | None = None,
    ) -> str:
        """Enable or disable repository-level security features.

        Applies the requested toggles to a single repository under the
        configured GitHub organisation.  Each toggle accepts ``"enabled"``
        or ``"disabled"``; omit (or pass ``None``) to leave a feature
        unchanged.

        **Scope:** Only repos within the GitHub App's current installation
        scope are modifiable — this is checked dynamically at call time.

        Args:
            repo_name: Repository name (not owner/name) — the org is
                configured server-side (default ``damien-robotsix``).
            dependency_graph: ``"enabled"`` or ``"disabled"``.
            advanced_security: ``"enabled"`` or ``"disabled"``.
            secret_scanning: ``"enabled"`` or ``"disabled"``.
            secret_scanning_push_protection: ``"enabled"`` or ``"disabled"``.

        Returns:
            A status message with the changed features on success, or an
            error message describing why the request was refused or failed.

        """
        repo_full_name = f"{org}/{repo_name}"

        allowed = await client.list_installation_repos()
        if repo_full_name not in allowed:
            return (
                f"Refused: repo '{repo_full_name}' is not in the GitHub App "
                f"installation scope. Allowed repos: "
                f"{', '.join(sorted(allowed))}"
            )

        return await client.set_security_and_analysis(
            repo_full_name,
            dependency_graph=dependency_graph,
            advanced_security=advanced_security,
            secret_scanning=secret_scanning,
            secret_scanning_push_protection=secret_scanning_push_protection,
        )

    return [set_repo_security_and_analysis]

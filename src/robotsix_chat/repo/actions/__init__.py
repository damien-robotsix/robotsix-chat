"""GitHub Actions tools for the chat agent.

Exposes :func:`build_github_actions_tools` — a factory returning LLM tools
for managing repository Actions secrets and dispatching workflows via the
GitHub App installation.

Also exposes :func:`load_github_actions_skill` which returns the component
skill markdown — a description of the Actions endpoints, their auth requirements,
and their confirmation-gated mutation policy.

Returns no tools when the capability is disabled.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings, GitHubActionsSettings

__all__ = ["build_github_actions_tools", "load_github_actions_skill"]


def load_github_actions_skill() -> str:
    """Return the GitHub Actions component skill markdown.

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


def build_github_actions_tools(
    github_actions: GitHubActionsSettings,
    direct_repo: DirectRepoSettings,
) -> list[Callable[..., Any]]:
    """Return GitHub Actions tool(s) for the agent, or ``[]`` when disabled."""
    if not github_actions.enabled:
        return []

    from robotsix_chat.repo.direct.client import DirectRepoClient

    client = DirectRepoClient(direct_repo)
    org = github_actions.github_org

    async def set_actions_secret(
        repo_name: str,
        secret_name: str,
        secret_value: str,
    ) -> str:
        """Set or update a repository Actions secret.

        **Confirmation-gated.** Before calling, confirm the repo and secret
        name with the user in-chat.  The secret value is encrypted with the
        repo's public key before transmission.

        **Scope:** Only repos within the GitHub App's current installation
        scope are modifiable — this is checked dynamically at call time.

        Args:
            repo_name: Repository name (not owner/name) — the org is
                configured server-side (default ``damien-robotsix``).
            secret_name: The name of the secret (e.g. ``"OVH_SFTP_HOST"``).
            secret_value: The plaintext value to store.  Neither the server
                nor the agent retains this after the API call.

        Returns:
            A success message on completion, or an error message describing
            why the request was refused or failed.

        """
        repo_full_name = f"{org}/{repo_name}"

        allowed = await client.list_installation_repos()
        if repo_full_name not in allowed:
            return (
                f"Refused: repo '{repo_full_name}' is not in the GitHub App "
                f"installation scope. Allowed repos: "
                f"{', '.join(sorted(allowed))}"
            )

        return await client.set_actions_secret(
            repo_full_name,
            secret_name=secret_name,
            secret_value=secret_value,
        )

    async def dispatch_workflow(
        repo_name: str,
        workflow_id: str,
        ref: str = "main",
        inputs: str | None = None,
    ) -> str:
        """Trigger a workflow_dispatch on a repository workflow.

        **Confirmation-gated.** Before calling, confirm the repo, workflow,
        ref, and inputs with the user in-chat.

        **Scope:** Only repos within the GitHub App's current installation
        scope are modifiable — this is checked dynamically at call time.

        Args:
            repo_name: Repository name (not owner/name) — the org is
                configured server-side (default ``damien-robotsix``).
            workflow_id: The workflow file name (e.g. ``"deploy.yml"``) or
                numeric workflow ID.
            ref: The branch or tag to run the workflow on (default ``"main"``).
            inputs: Optional JSON object string with workflow input keys/values
                (e.g. ``'{"environment": "production"}'``).

        Returns:
            A success message on completion, or an error message describing
            why the request was refused or failed.

        """
        repo_full_name = f"{org}/{repo_name}"

        allowed = await client.list_installation_repos()
        if repo_full_name not in allowed:
            return (
                f"Refused: repo '{repo_full_name}' is not in the GitHub App "
                f"installation scope. Allowed repos: "
                f"{', '.join(sorted(allowed))}"
            )

        parsed_inputs: dict[str, str] | None = None
        if inputs:
            import json

            try:
                parsed = json.loads(inputs)
                if not isinstance(parsed, dict):
                    return (
                        f"Error: inputs must be a JSON object, "
                        f"got {type(parsed).__name__}"
                    )
                parsed_inputs = {
                    str(k): str(v) for k, v in parsed.items()
                }
            except json.JSONDecodeError as exc:
                return f"Error parsing inputs JSON: {exc}"

        return await client.dispatch_workflow(
            repo_full_name,
            workflow_id=workflow_id,
            ref=ref,
            inputs=parsed_inputs,
        )

    return [set_actions_secret, dispatch_workflow]

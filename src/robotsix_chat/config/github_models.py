"""Github Settings Models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr


class GitHubSecuritySettings(BaseModel):
    """Repository security-feature toggle via the GitHub App installation.

    When enabled, the chat agent gains a ``set_repo_security_and_analysis``
    tool that can enable or disable repository-level security features
    (dependency graph, advanced security, secret scanning) on repos under
    the configured GitHub App's installation scope.

    **Guardrails built into the tool (not configurable):**
    - Repo scope is resolved dynamically from the GitHub App installation
      (list-installation-repositories) — no static allowlist.
    - Only repos within the installation scope are modifiable.
    - Each feature toggle explicitly requires ``"enabled"`` or ``"disabled"``
      — no accidental bulk changes.

    Attributes:
        enabled: Master switch.  When ``False``, no security-feature tool
            is offered.
        github_org: GitHub organisation name whose repos are in scope
            (e.g. ``"damien-robotsix"``).  The tool only targets repos
            under this org.
        deploy_api_key: API key that clients must present in the
            ``X-API-Key`` header when calling the
            ``PATCH /chat/github/repos/{owner}/{repo}/settings``
            endpoint.  When empty, the endpoint returns 503 (unconfigured).

    Note: GitHub App authentication is delegated to
    :class:`DirectRepoSettings` — those credentials must also be configured
    for the tool to function.

    """

    enabled: bool = False
    github_org: str = "damien-robotsix"
    deploy_api_key: SecretStr = SecretStr("")
    model_config = ConfigDict(extra="forbid")


class GitHubActionsSettings(BaseModel):
    """GitHub Actions secrets and workflow dispatch via the GitHub App installation.

    When enabled, the chat agent gains ``set_actions_secret`` and
    ``dispatch_workflow`` tools that can create/update repository Actions
    secrets and trigger ``workflow_dispatch`` events on repos under the
    configured GitHub App's installation scope.

    **Guardrails built into the tools (not configurable):**
    - Repo scope is resolved dynamically from the GitHub App installation
      (list-installation-repositories) — no static allowlist.
    - Only repos within the installation scope are modifiable.
    - Secret encryption uses libsodium sealed-box (requires ``pynacl``).
    - Both tools are confirmation-gated: the agent must confirm the exact
      repo, secret name (or workflow id + ref) with the user before calling.

    Attributes:
        enabled: Master switch.  When ``False``, no Actions tools are offered.
        github_org: GitHub organisation name whose repos are in scope
            (e.g. ``"damien-robotsix"``).
        deploy_api_key: API key that clients must present in the
            ``X-API-Key`` header when calling the Actions endpoints.
            When empty, the endpoints return 503 (unconfigured).

    Note: GitHub App authentication is delegated to
    :class:`DirectRepoSettings` — those credentials must also be configured
    for the tools to function.

    """

    enabled: bool = False
    github_org: str = "damien-robotsix"
    deploy_api_key: SecretStr = SecretStr("")
    model_config = ConfigDict(extra="forbid")

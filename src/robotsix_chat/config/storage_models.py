"""Storage Settings Models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr


class DirectRepoSettings(BaseModel):
    """Direct-repo push-branch, open-PR, and direct-fix capability.

    Authenticates as the robotsix-mill GitHub App.  When enabled, the chat
    agent gains tools: ``push_direct_repo_branch``
    (create/push a branch with file changes), ``open_direct_repo_pr``
    (open a PR from a branch), and — when *direct_fix_enabled* is also
    ``True`` — ``direct_fix`` (push a commit directly to a target branch,
    bypassing the PR flow).  All authenticate as the configured GitHub App
    installation (JWT → short-lived installation token) and dynamically
    resolve the allowed repo set from the installation at action time —
    no static allowlist.

    **Guardrails built into the tools (not configurable):**
    - Actions are ONLY permitted for tickets in BLOCKED state.
    - Repo scope is resolved dynamically from the GitHub App installation.
    - PRs are opened in a reviewable state with no auto-merge.
    - ``merge_direct_repo_pr`` can merge an approved, mergeable PR when the ticket is
      in BLOCKED state — do not merge before the human gate is satisfied.

    **Additional guardrails for ``direct_fix``:**
    - Ticket must have exhausted its spawn limit (≥3 implement cycles)
      verified against the board API.
    - Every direct-fix action is logged at WARNING level for auditability.

    Attributes:
        enabled: Master switch.  When ``False``, no direct-repo tools are
            offered.
        direct_fix_enabled: When ``True`` (and *enabled* is ``True``), the
            ``direct_fix`` tool is available for pushing commits directly
            to a target branch after mill exhaustion.  Default ``False``.
        github_app_id: The GitHub App's numeric or slug id.  Required when
            *enabled*.
        github_app_private_key: The app's RSA private key in PEM format.
            Required when *enabled*.  Stored in config only — never
            hardcoded.
        github_app_installation_id: The installation id to act as.  The
            app must be installed on the target org/account.  Required when
            *enabled*.
        github_api_base_url: Overridable base URL for GitHub Enterprise.
        board_api_base_url: Base URL of the board HTTP API for ticket-state
            lookups (verifying BLOCKED state).
        board_api_token: Optional bearer token for the board API.
        timeout: Per-request HTTP timeout in seconds.

    """

    enabled: bool = False
    direct_fix_enabled: bool = False
    github_app_id: str = ""
    github_app_private_key: SecretStr = SecretStr("")
    github_app_installation_id: str = ""
    github_api_base_url: str = "https://api.github.com"
    board_api_base_url: str = "http://mill:8077"
    board_api_token: SecretStr = SecretStr("")
    timeout: float = 30.0
    model_config = ConfigDict(extra="forbid")


class RepoStudySettings(BaseModel):
    """Temporary local repo workspaces the agent can fetch and study.

    When enabled, the chat agent gains read-only tools to download a GitHub
    repository snapshot (tarball — no ``git`` binary involved), extract it
    into a temporary workspace under *data_dir*, and study it locally
    (list / read / regex-search files) before dropping it.  Workspaces are
    transient: they are deleted on demand (``drop_repo_workspace``) and
    swept automatically once older than *ttl_minutes*.

    Authentication reuses the ``direct_repo`` GitHub App credentials when
    they are configured (the app's installation scope defines the private
    repos the agent may fetch); without them only public repositories are
    reachable.  No new credential fields are introduced.

    Attributes:
        enabled: Master switch.  When ``False``, no repo-study tools are
            offered.
        data_dir: Directory holding the temporary workspaces.  Default
            ``/data/repo_study`` (on the persistent volume, so a redeploy
            mid-study does not lose the workspace; the TTL sweep still
            bounds growth).
        ttl_minutes: Age after which a workspace is deleted by the sweep
            that runs on every repo-study tool call.
        max_archive_bytes: Maximum size of the downloaded tarball.
        max_extracted_bytes: Maximum total uncompressed size of a workspace.
        max_read_bytes: Maximum bytes returned by a single file read.
        timeout: Per-request HTTP timeout in seconds for the download.

    """

    enabled: bool = False
    data_dir: str = "/data/repo_study"
    ttl_minutes: int = 240
    max_archive_bytes: int = 67_108_864
    max_extracted_bytes: int = 268_435_456
    max_read_bytes: int = 204_800
    timeout: float = 60.0
    model_config = ConfigDict(extra="forbid")


class SftpSettings(BaseModel):
    """SFTP config-restore settings.

    Provides credentials and connection parameters for the SFTP config-restore
    capability.  When enabled, the agent gains tools to read, list, and
    (confirmation-gated) write files on a remote SFTP server — used to
    restore known-good configuration files when diagnostics detect they are
    missing.

    Attributes:
        enabled: Master switch.  When ``False`` (default), no SFTP tools
            are registered and the agent runs exactly as before.
        host: SFTP server hostname or IP address.
        port: SFTP server port (default 22).
        username: SFTP username for authentication.
        password: Password for password-based authentication.  Leave empty
            when using key-based auth.
        private_key: OpenSSH-format private key for key-based
            authentication.  Leave empty when using password auth.
        private_key_passphrase: Passphrase for *private_key*, if the key
            is encrypted.
        known_hosts: OpenSSH-format known-hosts entries (one or more lines)
            for host key verification.  When empty, host key verification
            is skipped (insecure — only suitable for isolated networks).
        remote_root: Optional base directory on the remote server to
            restrict all operations under (e.g. ``/var/www``).  When set,
            paths are resolved relative to this root and traversal outside
            it is refused.

    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = ""
    port: int = 22
    username: str = ""
    password: SecretStr = SecretStr("")
    private_key: SecretStr = SecretStr("")
    private_key_passphrase: SecretStr = SecretStr("")
    known_hosts: str = ""
    remote_root: str = ""

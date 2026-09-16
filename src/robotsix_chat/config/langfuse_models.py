"""Langfuse configuration models.

Configure Langfuse observability: credentials, region (cloud.langfuse.com,
self-hosted, or EU region), and settings for the trace-inspection tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    pass

from robotsix_chat.config.models import PROJECT_MAIN, ProjectKey


class LangfuseCreds(BaseModel):
    """Public/secret key pair for a single Langfuse project.

    Attributes:
        project_id: The Langfuse project id (for the API; not used directly
            by the chat agent, mainly for debugging and documentation).
        public_key: Langfuse public (client) key.
        secret_key: Langfuse secret key.
    """

    project_id: str = ""
    public_key: str = ""  # type: ignore[assignment]
    secret_key: str = ""  # type: ignore[assignment]

    model_config = ConfigDict(extra="forbid")


class LangfuseCreds(BaseModel):
    """Public/secret key pair for a single Langfuse project.

    Attributes:
        project_id: The Langfuse project id (for the API; not used directly
            by the chat agent, mainly for debugging and documentation).
        public_key: Langfuse public (client) key.
        secret_key: Langfuse secret key.
    """

    project_id: str = ""
    public_key: str = ""  # type: ignore[assignment]
    secret_key: str = ""  # type: ignore[assignment]

    model_config = ConfigDict(extra="forbid")


class LangfuseInspectSettings(BaseModel):
    """Langfuse trace-inspection tool — lets the agent query recent traces.

    Reuses the main ``langfuse`` credentials (public key + secret key + host)
    for API authentication — no separate credential fields.  When enabled, the
    agent gains an ``inspect_langfuse_trace`` tool that fetches and summarises
    recent implement traces for a given ticket or trace id.

    Attributes:
        enabled: Master switch.  Default ``False``.
        max_traces: Maximum number of traces returned per query.  Default ``10``.

    """

    enabled: bool = False
    max_traces: int = 10
    model_config = ConfigDict(extra="forbid")


class LangfuseSettings(BaseModel):
    """Langfuse configuration — credentials, region, and trace inspection.

    Attributes:
        enabled: Master switch — when False, all Langfuse instrumentation is
            disabled (no LLM call tracing, no subsession tracing, no
            inspect-trace tool).  Default ``False``.
        public_key_env: Environment variable name holding the Langfuse public
            key (default ``LANGFUSE_PUBLIC_KEY``).  Passed to
            ``robotsix_chat.config.SecretStr`` for credential masking.
        secret_key_env: Environment variable name holding the Langfuse secret
            key (default ``LANGFUSE_SECRET_KEY``).
        host: Langfuse API host (default ``https://cloud.langfuse.com``).  Set
            to ``https://eu.cloud.langfuse.com`` for EU region.
        sdk_version: SDK version reported in trace metadata (default inferred
            from ``langfuse`` package version).
        projects: Mapping of project keys to their credentials. The key
            ``PROJECT_MAIN`` (default) holds the main project's creds. Other
            keys can be used for project-specific routing (not yet used).
        inspect: Trace-inspection tool config (enabled, max_traces).

    """

    enabled: bool = False
    public_key_env: str = "LANGFUSE_PUBLIC_KEY"
    secret_key_env: str = "LANGFUSE_SECRET_KEY"
    host: str = "https://cloud.langfuse.com"
    sdk_version: str = ""
    projects: dict[ProjectKey, LangfuseCreds] = Field(
        default_factory=lambda: {PROJECT_MAIN: LangfuseCreds()}
    )
    inspect: LangfuseInspectSettings = Field(
        default_factory=LangfuseInspectSettings
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("projects", mode="after")
    @classmethod
    def ensure_main_project(cls, v: dict[ProjectKey, LangfuseCreds]) -> dict:
        """Ensure PROJECT_MAIN is always present, even if empty."""
        if PROJECT_MAIN not in v:
            v[PROJECT_MAIN] = LangfuseCreds()
        return v

    def creds(self, project_key: ProjectKey = PROJECT_MAIN) -> LangfuseCreds:
        """Return credentials for a named project (defaults to PROJECT_MAIN)."""
        return self.projects.get(project_key, LangfuseCreds())

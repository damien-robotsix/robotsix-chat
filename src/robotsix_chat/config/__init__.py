"""Configuration package for robotsix-chat (split from ``config.py``).

Re-exports every public symbol at the package top level so existing
``from robotsix_chat.config import <X>`` imports keep working unchanged.
"""

from robotsix_chat.config.constants import (
    ConfigError,
    level_needs_api_key,
)
from robotsix_chat.config.models import (
    CentralDeploySettings,
    ComponentClientSettings,
    ComponentTarget,
    ConversationSettings,
    DiagnosticsSettings,
    DirectRepoSettings,
    GithubSettings,
    KnowledgeSettings,
    LangfuseSettings,
    LifecycleSettings,
    MailSettings,
    MemoryEmbeddingSettings,
    MemoryLlmSettings,
    MemorySettings,
    RefDocsSettings,
    RepoStudySettings,
    SelfReviewSettings,
    SubsessionsSettings,
    VersionCheckSettings,
)
from robotsix_chat.config.settings import (
    SYSTEM_PROMPT_VERSION,
    Settings,
)

__all__ = [
    "CentralDeploySettings",
    "ComponentClientSettings",
    "ComponentTarget",
    "ConfigError",
    "ConversationSettings",
    "DiagnosticsSettings",
    "DirectRepoSettings",
    "GithubSettings",
    "KnowledgeSettings",
    "LangfuseSettings",
    "LifecycleSettings",
    "MailSettings",
    "MemoryEmbeddingSettings",
    "MemoryLlmSettings",
    "MemorySettings",
    "RefDocsSettings",
    "RepoStudySettings",
    "SYSTEM_PROMPT_VERSION",
    "SelfReviewSettings",
    "Settings",
    "SubsessionsSettings",
    "VersionCheckSettings",
    "level_needs_api_key",
]

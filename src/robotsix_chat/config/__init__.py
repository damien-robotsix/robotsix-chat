"""Configuration package for robotsix-chat (split from ``config.py``).

Re-exports every public symbol at the package top level so existing
``from robotsix_chat.config import <X>`` imports keep working unchanged.
"""

from robotsix_chat.config.constants import (
    ConfigError,
    level_needs_api_key,
)
from robotsix_chat.config.models import (
    BoardSettings,
    CalendarSettings,
    ComponentAgentSettings,
    ComponentClientSettings,
    ComponentTarget,
    ConversationSettings,
    DiagnosticsSettings,
    DirectRepoSettings,
    KnowledgeSettings,
    LangfuseSettings,
    MailSettings,
    MemoryEmbeddingSettings,
    MemoryLlmSettings,
    MemorySettings,
    MillSettings,
    RefDocsSettings,
    SelfReviewSettings,
    SkillsSettings,
    SubsessionsSettings,
    VersionCheckSettings,
)
from robotsix_chat.config.settings import (
    SYSTEM_PROMPT_VERSION,
    Settings,
)

__all__ = [
    "BoardSettings",
    "CalendarSettings",
    "ComponentAgentSettings",
    "ComponentClientSettings",
    "ComponentTarget",
    "ConfigError",
    "ConversationSettings",
    "DiagnosticsSettings",
    "DirectRepoSettings",
    "KnowledgeSettings",
    "LangfuseSettings",
    "MailSettings",
    "MemoryEmbeddingSettings",
    "MemoryLlmSettings",
    "MemorySettings",
    "MillSettings",
    "RefDocsSettings",
    "SYSTEM_PROMPT_VERSION",
    "SelfReviewSettings",
    "Settings",
    "SkillsSettings",
    "SubsessionsSettings",
    "VersionCheckSettings",
    "level_needs_api_key",
]

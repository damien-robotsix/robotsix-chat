"""Configuration package for robotsix-chat (split from ``config.py``).

Re-exports every public symbol at the package top level so existing
``from robotsix_chat.config import <X>`` imports keep working unchanged.
"""

from robotsix_chat.config.constants import (
    _YAML_PATH_TO_FIELD,
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    ConfigError,
    level_needs_api_key,
)
from robotsix_chat.config.models import (
    AuthSettings,
    BoardReaderSettings,
    CalendarSettings,
    ComponentAgentSettings,
    ComponentClientSettings,
    ComponentTarget,
    ConversationSettings,
    DiagnosticsSettings,
    DirectRepoSettings,
    KnowledgeSettings,
    MailSettings,
    MemoryEmbeddingSettings,
    MemoryLlmSettings,
    MemorySettings,
    MillSettings,
    PendingQuestionsSettings,
    RefDocsSettings,
    SelfReviewSettings,
    VersionCheckSettings,
)
from robotsix_chat.config.settings import (
    SYSTEM_PROMPT_VERSION,
    Settings,
)

__all__ = [
    "AuthSettings",
    "BoardReaderSettings",
    "CONFIG_PATH_ENV",
    "CalendarSettings",
    "ComponentAgentSettings",
    "ComponentClientSettings",
    "ComponentTarget",
    "ConfigError",
    "ConversationSettings",
    "DEFAULT_CONFIG_PATH",
    "DiagnosticsSettings",
    "DirectRepoSettings",
    "KnowledgeSettings",
    "MailSettings",
    "MemoryEmbeddingSettings",
    "MemoryLlmSettings",
    "MemorySettings",
    "MillSettings",
    "PendingQuestionsSettings",
    "RefDocsSettings",
    "SYSTEM_PROMPT_VERSION",
    "SelfReviewSettings",
    "Settings",
    "VersionCheckSettings",
    "_YAML_PATH_TO_FIELD",
    "level_needs_api_key",
]

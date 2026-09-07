"""Top-level :class:`Settings` model and its factories.

Composes the sub-models from :mod:`robotsix_chat.config.models` and
loads from a single JSON file located by ``ROBOTSIX_CONFIG_FILE``.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from robotsix_config import load_config

from robotsix_chat.config.constants import (
    VALID_MODEL_LEVELS,
    drop_blank_numeric_sentinels,
)
from robotsix_chat.config.models import (
    CentralDeploySettings,
    ComponentClientSettings,
    ContinuationSettings,
    ConversationSettings,
    DiagnosticsSettings,
    DirectRepoSettings,
    DockerDigestSettings,
    EvergoingSettings,
    FeedbackSettings,
    FileHubToolsSettings,
    GatewayRouteSettings,
    GitHubActionsSettings,
    GitHubSecuritySettings,
    HealthSettings,
    HttpProbeSettings,
    KnowledgeSettings,
    LangfuseInspectSettings,
    LangfuseSettings,
    LifecycleSettings,
    MemoryComponentSettings,
    MobileAuthSettings,
    OpenRouterSettings,
    PeriodicSettings,
    PublicFetchSettings,
    RefDocsSettings,
    RenderUrlSettings,
    RepoStudySettings,
    SelfReviewSettings,
    SftpSettings,
    SubsessionsSettings,
    VersionCheckSettings,
    VolumeToolsSettings,
)
from robotsix_chat.config.system_prompt_history import KNOWN_SYSTEM_PROMPT_SHA256S

logger = logging.getLogger(__name__)


class ConfigValidationError(ValueError):
    """Raised when one or more config preconditions fail.

    Carries a ``failures`` list so callers can report per-precondition
    details (which check failed, what value was seen) rather than a
    single opaque string.
    """

    def __init__(self, failures: list[str]) -> None:
        """Store *failures* and set a combined message."""
        self.failures: list[str] = failures
        super().__init__("; ".join(failures))


# Version stamp for the agent_instruction default literal.
# Bump on every change to Settings.agent_instruction and update
# docs/system_prompt_changelog.md with a new entry + SHA256.
SYSTEM_PROMPT_VERSION = 161

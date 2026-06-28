"""Layered configuration for robotsix-chat.

Settings resolve through a single, predictable cascade that matches the
rest of the robotsix stack (``robotsix-mill`` / ``robotsix-auto-mail``):

    pydantic field defaults  →  YAML config file  →  environment variables

with each later layer overriding the earlier one field-by-field. YAML
loading is delegated to the shared ``robotsix-yaml-config`` library so
every repo in the stack reads YAML the same way.

The LLM is selected through ``robotsix-llmio``'s consumer-facing
``provider-model`` tier identifier (``robotsix_llmio.config``): you pick a
capability level and llmio resolves the provider + model from its baked
defaults, never a concrete provider class.

The YAML file lives at ``config/chat.local.yaml`` by default (gitignored
so credentials never land in the repo); copy ``config/chat.local.example.yaml``
to create it. Override the path with the ``CHAT_CONFIG_PATH`` env var.

A ``.env`` file in the working directory is still loaded (via
``python-dotenv``) before environment variables are read, so existing
``.env``-based deployments keep working.
"""

from __future__ import annotations

from pathlib import Path

from robotsix_llmio.config import LEVEL1_DEFAULT, LEVEL2_DEFAULT, LEVEL3_DEFAULT
from robotsix_yaml_config import YamlConfigError

# Default YAML config file (gitignored; copy from the committed example).
DEFAULT_CONFIG_PATH = Path("config") / "chat.local.yaml"

# Env var that overrides the YAML config file path.
CONFIG_PATH_ENV = "CHAT_CONFIG_PATH"

__all__ = [
    "_YAML_PATH_TO_FIELD",
    "CONFIG_PATH_ENV",
    "DEFAULT_CONFIG_PATH",
    "ConfigError",
    "level_needs_api_key",
]

# Case-insensitive truthy spellings for the AUTH_ENABLED env override.
_TRUE_VALUES = {"1", "true", "yes", "on"}

# robotsix-llmio now owns the level → provider-model mapping. The chat
# just picks a capability *level*; the combined provider-model identifier for
# that level comes from llmio's baked default TierLevelConfig (single source
# of truth):
#   level 1 → openrouter[deepseek]-deepseek/deepseek-v4-flash  (cheapest)
#   level 2 → openrouter[deepseek]-deepseek/deepseek-v4-pro
#   level 3 → claudeSDK-opus  (most capable; keyless)
_LEVEL_DEFAULTS = {1: LEVEL1_DEFAULT, 2: LEVEL2_DEFAULT, 3: LEVEL3_DEFAULT}

# Provider prefix for the keyless Claude SDK tier (auth via logged-in
# `claude` CLI — no API key needed).
_KEYLESS_PROVIDER = "claudeSDK"


def level_needs_api_key(level: int) -> bool:
    """Whether *level*'s default provider requires an ``api_key``.

    True for key-bearing providers (e.g. ``openrouter``), False for the
    keyless ``claudeSDK`` provider. Unknown levels are treated as needing a
    key (model_level is validated separately before this matters).
    """
    tlc = _LEVEL_DEFAULTS.get(level)
    return tlc is None or tlc.provider != _KEYLESS_PROVIDER


# Maps nested YAML ``section.field`` paths to ``Settings`` field names.
# The whole ``auth`` mapping is passed through as-is (a dict) so pydantic
# parses it into the nested :class:`AuthSettings` model.
_YAML_PATH_TO_FIELD: dict[str, str] = {
    "llmio.model_level": "llmio_model_level",
    "llmio.api_key": "llmio_api_key",  # pragma: allowlist secret
    "llmio.subagent_model": "subagent_model",
    "llmio.check_loop_model": "check_loop_model",
    "agent.instruction": "agent_instruction",
    "server.host": "server_host",
    "server.port": "server_port",
    "server.idle_timeout_minutes": "idle_timeout_minutes",
    "server.max_background_tasks": "max_background_tasks",
    "server.max_check_loops": "max_check_loops",
    "server.min_check_loop_interval_seconds": "min_check_loop_interval_seconds",
    "server.log_level": "log_level",
    "server.cors_allow_origins": "cors_allow_origins",
    "server.correlation_id_header": "correlation_id_header",
    "auth": "auth",
    "memory": "memory",
    "mill": "mill",
    "mail": "mail",
    "calendar": "calendar",
    "conversation": "conversation",
    "refdocs": "refdocs",
    "board_reader": "board_reader",
    "diagnostics": "diagnostics",
    "version_check": "version_check",
    "knowledge": "knowledge",
    "self_review": "self_review",
    "component_agent": "component_agent",
    "component_client": "component_client",
    "pending_questions": "pending_questions",
    "direct_repo": "direct_repo",
    "skills": "skills",
}


class ConfigError(YamlConfigError):
    """Raised for config-loading failures (missing file, malformed YAML).

    Subclasses the shared base so ``except YamlConfigError`` handlers in
    the stack keep working.
    """


def _parse_bool(value: str) -> bool:
    """Parse an env-var string into a bool (``"true"``/``"1"``/… → True)."""
    return value.strip().lower() in _TRUE_VALUES

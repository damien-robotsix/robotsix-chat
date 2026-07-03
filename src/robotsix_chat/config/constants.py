"""Layered configuration for robotsix-chat.

Settings resolve through a single cascade: pydantic field defaults are
overlaid with values from a single JSON config file located by the
``ROBOTSIX_CONFIG_FILE`` environment variable (defaults to
``config/config.json``).  There is no environment-variable overlay —
``environment:`` is never a config channel for first-party code.

The LLM is selected through ``robotsix-llmio``'s consumer-facing
``provider-model`` tier identifier (``robotsix_llmio.config``): you pick a
capability level and llmio resolves the provider + model from its baked
defaults, never a concrete provider class.
"""

from __future__ import annotations

from typing import Any

from robotsix_llmio.config import (
    LEVEL1_DEFAULT,
    LEVEL2_DEFAULT,
    LEVEL3_DEFAULT,
)

try:
    from robotsix_llmio.config import LEVEL4_DEFAULT
except ImportError:
    LEVEL4_DEFAULT = LEVEL3_DEFAULT  # fallback when llmio doesn't ship level 4 yet

__all__ = [
    "ConfigError",
    "level_needs_api_key",
]

# robotsix-llmio now owns the level → provider-model mapping. The chat
# just picks a capability *level*; the combined provider-model identifier for
# that level comes from llmio's baked default TierLevelConfig (single source
# of truth):
#   level 1 → openrouter-deepseek/deepseek-v4-flash  (cheapest)
#   level 2 → openrouter-deepseek/deepseek-v4-pro
#   level 3 → claudeSDK-opus  (keyless)
#   level 4 → claudeSDK-claude-fable-5  (frontier; keyless)
_LEVEL_DEFAULTS: dict[int, Any] = {
    1: LEVEL1_DEFAULT,
    2: LEVEL2_DEFAULT,
    3: LEVEL3_DEFAULT,
    4: LEVEL4_DEFAULT,
}

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


class ConfigError(Exception):
    """Raised for config-loading failures (missing file, malformed JSON)."""

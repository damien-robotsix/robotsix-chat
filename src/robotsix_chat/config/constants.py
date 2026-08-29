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

from robotsix_llmio import default_tier_config
from robotsix_llmio.config import (
    LEVEL1_DEFAULT,
    LEVEL2_DEFAULT,
    LEVEL3_DEFAULT,
    LEVEL4_DEFAULT,
    LEVEL5_DEFAULT,
)

__all__ = [
    "FRONTIER_MODEL_LEVEL",
    "level_display_name",
    "level_needs_api_key",
]

# robotsix-llmio now owns the level → provider-model mapping. The chat
# just picks a capability *level*; the combined provider-model identifier for
# that level comes from llmio's baked default TierLevelConfig (single source
# of truth) — see ``robotsix_llmio.config.tier``.
_LEVEL_DEFAULTS: dict[int, Any] = {
    1: LEVEL1_DEFAULT,
    2: LEVEL2_DEFAULT,
    3: LEVEL3_DEFAULT,
    4: LEVEL4_DEFAULT,
    5: LEVEL5_DEFAULT,
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


#: The strongest capability level a session can escalate to (currently 5).
#: Named rather than hard-coded at call sites so the frontier is a
#: one-line change here.
FRONTIER_MODEL_LEVEL = 5


def level_display_name(level: int) -> str:
    """Return the human-facing model name for *level*.

    Resolved from llmio's baked default tier config so the display always
    matches what actually served the turn. Unknown levels render as
    ``"level N"`` rather than raising — this is display text, never a
    control-flow input.
    """
    try:
        tlc = default_tier_config().for_level(level)
    except ValueError:
        return f"level {level}"
    return str(tlc.model_name)

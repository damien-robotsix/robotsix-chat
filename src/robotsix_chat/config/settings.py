"""Top-level :class:`Settings` model and its factories.

Composes the sub-models from :mod:`robotsix_chat.config.models` and
loads from a single JSON file located by ``ROBOTSIX_CONFIG_FILE``.
"""

from __future__ import annotations

import logging

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

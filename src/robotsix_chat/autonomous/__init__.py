"""Autonomous session support — self-directed chat sessions with auto-cycling."""

from robotsix_chat.autonomous.models import (
    AUTONOMOUS_KIND,
    AUTONOMOUS_TITLE_PREFIX,
    DEFAULT_KIND,
    VALID_KINDS,
    AutonomousState,
)
from robotsix_chat.autonomous.prompts import (
    AUTONOMOUS_SYSTEM_PROMPT_SUPPLEMENT,
    build_autonomous_instruction,
)
from robotsix_chat.autonomous.runner import AutonomousRunner

__all__ = [
    "AUTONOMOUS_KIND",
    "AUTONOMOUS_SYSTEM_PROMPT_SUPPLEMENT",
    "AUTONOMOUS_TITLE_PREFIX",
    "DEFAULT_KIND",
    "VALID_KINDS",
    "AutonomousRunner",
    "AutonomousState",
    "build_autonomous_instruction",
]

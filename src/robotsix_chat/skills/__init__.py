"""Skill/capability loading — discover, load, and surface broker capabilities.

The skill system replaces the hardcoded per-broker ``build_*_tools()`` pattern
with a declarative, capability-scoped mechanism.  Each broker's capabilities
are declared in a YAML manifest (``config/skills/*.skill.yaml``); the loader
discovers enabled manifests, instantiates a :class:`BrokerSkill` for each, and
returns LLM-callable tools — one per capability — that call the broker with
the capability's structured ``kind`` and parameters.

Per-broker migration tickets port each existing broker integration (mill,
calendar, mail, component_client) to a skill manifest, removing the
corresponding hardcoded ``build_*_tools()`` call from
:func:`~robotsix_chat.chat.server.app.create_agent_from_settings`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from robotsix_chat.skills.broker_skill import BrokerSkill
from robotsix_chat.skills.loader import discover_manifests
from robotsix_chat.skills.spec import SkillManifest

logger = logging.getLogger(__name__)

__all__ = [
    "BrokerSkill",
    "SkillManifest",
    "build_skill_tools",
    "discover_manifests",
    "load_skills",
]


def load_skills(manifests_dir: str) -> list[BrokerSkill]:
    """Discover and instantiate :class:`BrokerSkill` for every enabled manifest.

    Args:
        manifests_dir: Directory containing ``*.skill.yaml`` files.

    Returns:
        One :class:`BrokerSkill` per enabled, valid manifest.  Disabled or
        malformed manifests are skipped with a warning.

    """
    manifests = discover_manifests(manifests_dir)
    skills: list[BrokerSkill] = []
    for manifest in manifests:
        if not manifest.enabled:
            logger.debug("Skill %r is disabled, skipping.", manifest.skill_id)
            continue
        try:
            skills.append(BrokerSkill(manifest))
        except Exception:
            logger.warning(
                "Failed to instantiate skill %r", manifest.skill_id, exc_info=True
            )
    return skills


def build_skill_tools(manifests_dir: str | None = None) -> list[Callable[..., Any]]:
    """Build the full tool list from all enabled skill manifests.

    This is the integration point called from
    :func:`~robotsix_chat.chat.server.app.create_agent_from_settings`.

    Args:
        manifests_dir: Directory containing ``*.skill.yaml`` files.
            Defaults to ``config/skills/`` relative to the repo root.

    Returns:
        A flat list of async callables — one per capability across all enabled
        skills.  Returns ``[]`` when no manifests are found or none are enabled.

    """
    if manifests_dir is None:
        manifests_dir = "config/skills"

    skills = load_skills(manifests_dir)
    tools: list[Callable[..., Any]] = []
    for skill in skills:
        tools.extend(skill.get_tools())
    return tools

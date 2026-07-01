"""Manifest discovery — find and parse ``*.skill.yaml`` files.

Scans a directory for YAML skill manifests, resolves ``${ENV_VAR}``
references in broker tokens, and returns parsed :class:`SkillManifest`
objects.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml

from robotsix_chat.skills.spec import SkillManifest

logger = logging.getLogger(__name__)

__all__ = ["discover_manifests"]

# Matches ${VAR_NAME} patterns (bash-style, no default-value syntax).
_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _resolve_env_vars(value: str) -> str:
    """Replace ``${VAR}`` references in *value* with environment variable values.

    Unset variables are left as-is (``${MISSING}`` stays literal) so the
    operator can spot them in logs.
    """

    def _replace(match: re.Match[str]) -> str:
        var = match.group(1)
        return os.environ.get(var, match.group(0))

    return _ENV_VAR_RE.sub(_replace, value)


def discover_manifests(manifests_dir: str) -> list[SkillManifest]:
    """Find and parse all ``*.skill.yaml`` files in *manifests_dir*.

    Args:
        manifests_dir: Directory path (relative or absolute).  If the
            directory does not exist, returns ``[]`` silently.

    Returns:
        Parsed manifest objects.  Unparsable files are skipped with a
        warning.

    """
    dir_path = Path(manifests_dir)
    if not dir_path.is_dir():
        logger.debug("Skill manifests directory %r not found, skipping.", manifests_dir)
        return []

    manifests: list[SkillManifest] = []
    for yaml_file in sorted(dir_path.glob("*.skill.yaml")):
        try:
            raw = yaml.safe_load(yaml_file.read_text())
        except yaml.YAMLError:
            logger.warning(
                "Failed to parse skill manifest %r, skipping.",
                str(yaml_file),
                exc_info=True,
            )
            continue
        except OSError:
            logger.warning(
                "Failed to read skill manifest %r, skipping.",
                str(yaml_file),
                exc_info=True,
            )
            continue

        if not isinstance(raw, dict):
            logger.warning(
                "Skill manifest %r is not a mapping, skipping.", str(yaml_file)
            )
            continue

        # Resolve env vars in the broker token before pydantic validation.
        if "broker" in raw and isinstance(raw["broker"], dict):
            token = raw["broker"].get("token", "")
            if isinstance(token, str):
                raw["broker"]["token"] = _resolve_env_vars(token)

        try:
            manifests.append(SkillManifest.model_validate(raw))
        except Exception:
            logger.warning(
                "Invalid skill manifest %r, skipping.",
                str(yaml_file),
                exc_info=True,
            )

    return manifests

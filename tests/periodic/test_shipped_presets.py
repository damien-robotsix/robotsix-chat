"""Guard the periodic presets shipped in the committed ``config/config.json``.

The committed template is what a developer gets on checkout and what
central-deploy merges operator edits into. These tests load that exact file,
validate it against the real ``Settings`` model, and assert the shipped
``dependabot-drain`` preset parses with its documented schedule — so a typo in
the template (or a schema drift) fails here instead of at deploy time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from robotsix_chat.config import Settings
from robotsix_chat.config.periodic_models import PeriodicSessionDefinition

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.json"


def _load_settings() -> Settings:
    raw = json.loads(_CONFIG_PATH.read_text())
    return Settings.model_validate(raw)


def _preset(settings: Settings, name: str) -> PeriodicSessionDefinition:
    matches = [s for s in settings.periodic.sessions if s.name == name]
    assert matches, f"preset {name!r} not found in committed config.json"
    return matches[0]


def test_committed_config_validates_against_settings() -> None:
    """The shipped template must load cleanly into the ``Settings`` model."""
    settings = _load_settings()
    assert isinstance(settings.periodic.sessions, list)


def test_dependabot_drain_preset_parses() -> None:
    """The ``dependabot-drain`` preset parses with its documented schedule."""
    preset = _preset(_load_settings(), "dependabot-drain")

    assert preset.schedule_interval_seconds == 604800  # weekly
    assert preset.anchor_utc == datetime(2026, 9, 7, 6, 0, 0, tzinfo=UTC)
    assert preset.model_level == 3
    # Ships disabled per the feature-flag convention (AGENT.md).
    assert preset.enabled is False
    # The initial prompt is a self-contained task brief.
    assert "list_open_prs" in preset.initial_prompt
    assert "/tickets/ingest" in preset.initial_prompt

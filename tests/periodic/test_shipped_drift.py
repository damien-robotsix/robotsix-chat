"""Shipped-template vs live-config preset drift (``periodic/shipped.py``).

A preset edit in the committed ``config/config.json`` template only reaches the
running chat when the same preset name exists in the live config volume. These
tests prove the mirror tracks the template and that a template-only edit for a
preset absent from the live config is surfaced, not silently treated as live.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from robotsix_chat.config import Settings
from robotsix_chat.config.periodic_models import PeriodicSessionDefinition
from robotsix_chat.periodic.shipped import (
    SHIPPED_PRESET_PROMPT_SHA256S,
    check_shipped_presets,
    prompt_sha256,
)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.json"


def _template_presets() -> list[PeriodicSessionDefinition]:
    raw = json.loads(_CONFIG_PATH.read_text())
    return Settings.model_validate(raw).periodic.sessions


def _preset(name: str, prompt: str) -> PeriodicSessionDefinition:
    return PeriodicSessionDefinition.model_validate(
        {"name": name, "initial_prompt": prompt, "schedule_interval_seconds": 3600}
    )


def test_mirror_matches_committed_template() -> None:
    """Editing a template preset prompt must update the shipped mirror too."""
    template = {d.name: prompt_sha256(d.initial_prompt) for d in _template_presets()}
    assert template == SHIPPED_PRESET_PROMPT_SHA256S, (
        "config/config.json periodic presets drifted from "
        "SHIPPED_PRESET_PROMPT_SHA256S in src/robotsix_chat/periodic/shipped.py. "
        "Update the mirror AND confirm the preset exists in the live config "
        "(GET /config) — a template-only edit does not reach the running chat."
    )


def test_template_matching_live_has_no_findings() -> None:
    assert check_shipped_presets(_template_presets()) == []


def test_template_only_edit_for_absent_preset_is_surfaced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 2026-10-22 case: template edits dependabot-drain; live has repo-hygiene."""
    shipped = {"dependabot-drain": prompt_sha256("new shipped prompt")}
    live = [_preset("repo-hygiene", "operator prompt")]

    with caplog.at_level(logging.WARNING, logger="robotsix_chat.periodic.shipped"):
        findings = check_shipped_presets(live, shipped)

    assert len(findings) == 1
    assert "'dependabot-drain'" in findings[0]
    assert "absent from the live config" in findings[0]
    assert "dependabot-drain" in caplog.text


def test_live_prompt_differing_from_template_is_surfaced() -> None:
    shipped = {"repo-hygiene": prompt_sha256("new shipped prompt")}
    live = [_preset("repo-hygiene", "old live prompt")]

    findings = check_shipped_presets(live, shipped)

    assert len(findings) == 1
    assert "'repo-hygiene'" in findings[0]
    assert "differs from the shipped template" in findings[0]
    # The live preset is reported, never mutated.
    assert live[0].initial_prompt == "old live prompt"


def test_live_prompt_matching_template_is_quiet() -> None:
    shipped = {"repo-hygiene": prompt_sha256("same prompt")}
    live = [_preset("repo-hygiene", "same prompt")]
    assert check_shipped_presets(live, shipped) == []

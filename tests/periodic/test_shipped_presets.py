"""Guard the periodic presets shipped in the committed ``config/config.json``.

The committed template is what a developer gets on checkout and what
central-deploy merges operator edits into. These tests load that exact file,
validate it against the real ``Settings`` model, and assert the shipped
``dependabot-drain`` preset parses with its documented schedule — so a typo in
the template (or a schema drift) fails here instead of at deploy time. The
``gate-drain`` preset is also guarded for its escalation-verification
requirement.
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


def test_gate_drain_preset_parses_and_demands_scope_report() -> None:
    """The ``gate-drain`` preset parses and requires up-front scope reporting.

    The prompt must instruct the agent to enumerate every gated state, report
    the total discovered count and per-state summary in the main conversation
    before detailed processing, and flag any discovered-vs-analyzed mismatch —
    the behaviour missed by the 2026-09-09 run that motivated this preset. It
    must also read each gated ticket's history for an existing ESCALATED
    comment / open decision panel before listing it as awaiting-operator, and
    state in the report that this history-verification was performed (motivated
    by a 2026-09-22 feedback run whose report listed escalated tickets without
    confirming the history check).
    """
    preset = _preset(_load_settings(), "gate-drain")

    assert preset.schedule_interval_seconds == 14400  # every four hours
    assert preset.model_level == 2
    # Ships disabled per the feature-flag convention (AGENT.md).
    assert preset.enabled is False

    prompt = preset.initial_prompt
    # Every gated state is enumerated.
    for state in (
        "human_issue_approval",
        "human_mr_approval",
        "awaiting_user_reply",
        "blocked",
    ):
        assert state in prompt
    # Scope reporting is mandated up front, in the main conversation.
    assert "TOTAL" in prompt
    assert "main conversation" in prompt
    assert "mismatch" in prompt
    # Subsession summaries must not swallow the scope report.
    assert "subsession" in prompt.lower()
    # Before listing a gated ticket as awaiting-operator, the agent must verify
    # its history for an existing ESCALATED comment / open decision panel.
    assert "ESCALATED" in prompt
    assert "awaiting operator" in prompt
    # The final report must explicitly confirm the history verification.
    assert "history-verification for pending escalations" in prompt
    assert "no operator answer recorded since" in prompt

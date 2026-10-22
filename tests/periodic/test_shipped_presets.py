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


def test_dependabot_drain_preset_updates_stale_branches_before_holding() -> None:
    """A bot PR red on its branch but green on main is updated, not held.

    Motivated by the 2026-10-22 repo-hygiene run, which held dependabot PRs as
    "systemic CI-red" when their branches merely predated a main-branch fix.
    The prompt must name the update-branch step, place it before the hold /
    classification decision, and make the report distinguish the two classes.
    """
    prompt = _preset(_load_settings(), "dependabot-drain").initial_prompt

    # The update-branch step is named, with the tool and the GitHub API call.
    assert "update-branch" in prompt
    assert "recover_auto_merge" in prompt
    assert "PUT /repos/{owner}/{repo}/pulls/{n}/update-branch" in prompt
    # The base branch's latest run of the same workflow is the comparison.
    assert "check_ci_health" in prompt
    assert "SAME workflow on the base branch" in prompt
    # The step comes BEFORE the classify / hold decision.
    assert prompt.index("Stale-branch check") < prompt.index("Classify it as")
    # Both classes are named, with the right disposition for each.
    assert "RED-ON-PR-GREEN-ON-MAIN" in prompt
    assert "RED-ON-MAIN-TOO" in prompt
    assert "This is a rebase, not a blocker" in prompt
    assert "This is a real blocker" in prompt
    assert "re-verify the checks on the next run" in prompt
    assert (
        "Never hold such a PR as CI-red without an update-branch attempt "
        "recorded in the report" in prompt
    )
    # The report keeps the two classes apart.
    assert "**Stale branches vs real blockers:**" in prompt


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
    # A ci-failure class ticket must be surfaced in the main conversation
    # before any subsession is opened for it — never escalated silently to
    # subsession metadata (motivated by the 2026-09-26 gate-drain run that
    # buried ticket 20260919T091747Z-ci-failure-docs-on-main in subsession
    # metadata without surfacing it in main chat).
    assert "ci-failure class ticket" in prompt or "ci_failure" in prompt.lower()
    assert "surface it in the main conversation" in prompt.lower()
    assert "before opening any subsession" in prompt.lower()
    # A stabilized cascade of file-classification tickets (module_curator
    # tickets blocking on module assignment) must spawn a triage user_chat
    # that enumerates the specific unclaimed files and per-file classification
    # options, so the operator decides without digging through ticket comments
    # (motivated by session 331c2099197b4606bfafea0d26c21f1d).
    assert "module_curator" in prompt
    assert "module assignment" in prompt
    assert "classification options" in prompt.lower()
    # The triage subsession must inline the specific files and per-file options.
    assert "unclaimed" in prompt.lower()
    assert "path pattern" in prompt.lower()
    assert "direct team input" in prompt.lower()
    # Board-API filters may be silently ignored, so the prompt must document the
    # limitation and give a local-parsing fallback with batching + PARTIAL
    # REPORT progress tracking that completes enumeration across turns
    # (motivated by the 2026-09-08 run that exhausted tokens re-fetching whole
    # boards because filter parameters were ignored).
    assert "silently ignore" in prompt.lower()
    assert "fetch each board once" in prompt.lower()
    assert "parse that response locally" in prompt.lower()
    assert "batch" in prompt.lower()
    assert "still remaining" in prompt.lower()
    assert "resumes" in prompt.lower()

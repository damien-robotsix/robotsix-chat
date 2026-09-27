"""Tests for the session-config models' backward-compatibility validators.

``src/robotsix_chat/config/session_models.py`` is mostly declarative pydantic
models, but it carries a few validators that silently guard config migration:

- ``KindTurnBudget._validate_ordering`` — enforces
  ``soft_warn_turns < hard_stop_turns`` when both are > 0.
- ``KindTurnBudget._strip_blank_numeric`` — drops legacy ``""`` sentinels so
  old configs load.
- ``ConversationSettings._strip_removed_cap_fields`` — strips removed fields so
  old configs load under ``extra="forbid"``.

These are the paths that break unnoticed, so they get explicit coverage here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from robotsix_chat.config import (
    ConversationSettings,
    KindTurnBudget,
    SubsessionsSettings,
    TurnBudgetSettings,
)

# ---------------------------------------------------------------------------
# KindTurnBudget._validate_ordering
# ---------------------------------------------------------------------------


def test_default_budget_is_valid() -> None:
    """The documented defaults (25 / 40) satisfy the ordering rule."""
    budget = KindTurnBudget()
    assert budget.soft_warn_turns == 25
    assert budget.hard_stop_turns == 40


def test_valid_ordering_accepted() -> None:
    """A soft warn strictly below the hard stop is accepted."""
    budget = KindTurnBudget(soft_warn_turns=10, hard_stop_turns=20)
    assert budget.soft_warn_turns == 10
    assert budget.hard_stop_turns == 20


def test_equal_thresholds_rejected() -> None:
    """Equal thresholds violate the strict ``<`` ordering."""
    with pytest.raises(ValidationError, match="must be less"):
        KindTurnBudget(soft_warn_turns=30, hard_stop_turns=30)


def test_reversed_thresholds_rejected() -> None:
    """A soft warn above the hard stop is rejected."""
    with pytest.raises(ValidationError, match="must be less"):
        KindTurnBudget(soft_warn_turns=40, hard_stop_turns=25)


def test_soft_warn_zero_disables_ordering_check() -> None:
    """A zero soft-warn disables the ordering guard (feature toggled off)."""
    budget = KindTurnBudget(soft_warn_turns=0, hard_stop_turns=40)
    assert budget.soft_warn_turns == 0
    assert budget.hard_stop_turns == 40


def test_hard_stop_zero_disables_ordering_check() -> None:
    """A zero hard-stop disables the ordering guard (the periodic default)."""
    budget = KindTurnBudget(soft_warn_turns=25, hard_stop_turns=0)
    assert budget.hard_stop_turns == 0


def test_both_zero_disables_ordering_check() -> None:
    """Both zeroed — the disabled/periodic budget — is accepted."""
    budget = KindTurnBudget(soft_warn_turns=0, hard_stop_turns=0)
    assert budget.soft_warn_turns == 0
    assert budget.hard_stop_turns == 0


def test_negative_thresholds_skip_ordering_check() -> None:
    """Negative values (not > 0) bypass the ordering guard rather than raising."""
    budget = KindTurnBudget(soft_warn_turns=-5, hard_stop_turns=-10)
    assert budget.soft_warn_turns == -5
    assert budget.hard_stop_turns == -10


# ---------------------------------------------------------------------------
# KindTurnBudget._strip_blank_numeric
# ---------------------------------------------------------------------------


def test_blank_sentinels_fall_back_to_defaults() -> None:
    """Legacy ``""`` sentinels are stripped so the fields take their defaults."""
    budget = KindTurnBudget.model_validate(
        {"soft_warn_turns": "", "hard_stop_turns": ""}
    )
    assert budget.soft_warn_turns == 25
    assert budget.hard_stop_turns == 40


def test_mixed_blank_and_valid_inputs() -> None:
    """A blank sentinel is stripped while a co-supplied real value is kept."""
    budget = KindTurnBudget.model_validate(
        {"soft_warn_turns": "", "hard_stop_turns": 50}
    )
    assert budget.soft_warn_turns == 25
    assert budget.hard_stop_turns == 50


def test_blank_sentinel_strip_preserves_ordering_validation() -> None:
    """Stripping ``""`` leaves the ordering guard active for real values.

    ``hard_stop_turns`` falls back to its default of 40, which is < 60, so the
    ordering guard still fires.
    """
    with pytest.raises(ValidationError, match="must be less"):
        KindTurnBudget.model_validate(
            {"soft_warn_turns": 60, "hard_stop_turns": ""}
        )


def test_non_dict_input_rejected() -> None:
    """A non-dict payload is rejected by pydantic, not the before-validator."""
    with pytest.raises(ValidationError):
        KindTurnBudget.model_validate([1, 2, 3])


def test_unknown_key_still_forbidden() -> None:
    """The blank-strip does not relax ``extra="forbid"`` for unknown keys."""
    with pytest.raises(ValidationError):
        KindTurnBudget.model_validate({"bogus_field": 1})


def test_turn_budget_settings_periodic_default_disabled() -> None:
    """The periodic budget default is the disabled 0/0 pair."""
    settings = TurnBudgetSettings()
    assert settings.periodic.soft_warn_turns == 0
    assert settings.periodic.hard_stop_turns == 0
    assert settings.task.soft_warn_turns == 25
    assert settings.task.hard_stop_turns == 40


def test_subsessions_turn_budget_blank_sentinels_nested() -> None:
    """Nested blank sentinels load cleanly through the full subsessions model."""
    settings = SubsessionsSettings.model_validate(
        {"turn_budget": {"task": {"soft_warn_turns": "", "hard_stop_turns": ""}}}
    )
    assert settings.turn_budget.task.soft_warn_turns == 25
    assert settings.turn_budget.task.hard_stop_turns == 40


# ---------------------------------------------------------------------------
# ConversationSettings._strip_removed_cap_fields
# ---------------------------------------------------------------------------


def test_conversation_defaults() -> None:
    """The default persist path is the documented ``/data`` location."""
    settings = ConversationSettings()
    assert settings.persist_path == "/data/conversations.json"


def test_removed_cap_fields_stripped() -> None:
    """Legacy ``max_history_turns``/``max_conversations`` keys are dropped."""
    settings = ConversationSettings.model_validate(
        {
            "persist_path": "/tmp/conv.json",
            "max_history_turns": 100,
            "max_conversations": 50,
        }
    )
    assert settings.persist_path == "/tmp/conv.json"
    assert not hasattr(settings, "max_history_turns")
    assert not hasattr(settings, "max_conversations")


def test_removed_cap_fields_stripped_preserves_defaults() -> None:
    """A pure-legacy dict loads and the valid field keeps its default."""
    settings = ConversationSettings.model_validate(
        {"max_history_turns": 10, "max_conversations": 5}
    )
    assert settings.persist_path == "/data/conversations.json"


def test_conversation_unknown_key_still_forbidden() -> None:
    """Only the two named legacy keys are stripped; others still fail."""
    with pytest.raises(ValidationError):
        ConversationSettings.model_validate({"unexpected_key": 1})

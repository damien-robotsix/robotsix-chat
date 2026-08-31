"""The primary Claude probe is skipped while llmio's tracker cools the tier."""

from __future__ import annotations

from robotsix_llmio.config.tier import TierConfig
from robotsix_llmio.core import get_health_tracker, reset_health_tracker

from robotsix_chat.llm.agent import _primary_in_cooldown


def _claude_level() -> tuple[int, str]:
    cfg = TierConfig()
    for n in (2, 4, 5):
        model = str(getattr(cfg, f"level{n}").model)
        if model.startswith("claudeSDK"):
            return n, model
    raise AssertionError("no claudeSDK tier in the default map")


def test_not_in_cooldown_by_default() -> None:
    reset_health_tracker()
    level, _ = _claude_level()
    assert _primary_in_cooldown(level) is False


def test_detects_cooldown() -> None:
    reset_health_tracker()
    level, model = _claude_level()
    tracker = get_health_tracker()
    for _ in range(tracker.failure_threshold):
        tracker.record_failure(model)  # exc=None: caller asserts terminal
    try:
        assert _primary_in_cooldown(level) is True
    finally:
        reset_health_tracker()


def test_keyed_tier_never_reports_cooldown() -> None:
    reset_health_tracker()
    cfg = TierConfig()
    keyed = [
        n
        for n in (1, 3)
        if not str(getattr(cfg, f"level{n}").model).startswith("claudeSDK")
    ]
    assert keyed
    assert _primary_in_cooldown(keyed[0]) is False

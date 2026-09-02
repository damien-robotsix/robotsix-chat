"""The system prompt must carry an authoritative current-date/time signal.

The agent has no reliable internal clock, so ``_inject_skills`` stamps the
build-time UTC clock into every system prompt and instructs the agent to treat
it as the source of truth for date-relative reasoning. These tests pin that
contract so a scheduled-event "missed" conclusion can never be drawn from a
hallucinated date.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Pre-existing import cycle (see test_skill_disclosure): entering the skill
# ring from ``periodic`` completes it cleanly.
import robotsix_chat.periodic  # noqa: F401
from robotsix_chat.chat.server.app import _current_datetime_directive, _inject_skills
from robotsix_chat.config import Settings


def _settings() -> Settings:
    return Settings()


class TestCurrentDatetimeDirective:
    """The directive itself."""

    def test_stamps_the_injected_instant_in_utc(self) -> None:
        """A pinned ``now`` renders as an ISO-8601 UTC stamp."""
        now = datetime(2026, 9, 1, 13, 45, 30, tzinfo=UTC)
        directive = _current_datetime_directive(now)
        assert "2026-09-01T13:45:30Z" in directive

    def test_normalises_non_utc_input_to_utc(self) -> None:
        """A non-UTC ``now`` is converted before stamping."""
        from datetime import timedelta, timezone

        now = datetime(2026, 9, 1, 15, 45, 30, tzinfo=timezone(timedelta(hours=2)))
        directive = _current_datetime_directive(now)
        assert "2026-09-01T13:45:30Z" in directive

    def test_forbids_missed_event_claims_before_the_scheduled_time(self) -> None:
        """The directive spells out the acceptance criterion's guardrail."""
        directive = _current_datetime_directive(datetime(2026, 9, 1, tzinfo=UTC))
        assert "authoritative" in directive.lower()
        # The core rule: no "missed" conclusion unless now is after the event.
        assert "strictly after" in directive


class TestInjection:
    """``_inject_skills`` always appends the directive."""

    def test_directive_is_injected_for_full_agents(self) -> None:
        """A tool-enabled build carries the stamped clock signal."""
        now = datetime(2026, 9, 1, 13, 45, 30, tzinfo=UTC)
        prompt = _inject_skills(_settings(), "Base instruction.", now=now)
        assert "Current date and time (authoritative)" in prompt
        assert "2026-09-01T13:45:30Z" in prompt

    def test_directive_is_injected_for_bare_agents(self) -> None:
        """Even bare (tool-less) agents get the clock signal."""
        now = datetime(2026, 9, 1, 13, 45, 30, tzinfo=UTC)
        prompt = _inject_skills(_settings(), "Base instruction.", bare=True, now=now)
        assert "2026-09-01T13:45:30Z" in prompt

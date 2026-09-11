"""The two halves of progressive disclosure must agree.

The system prompt advertises skills by name; ``read_skill`` serves the bodies.
Both read ``_skill_registry``, so a skill can never be advertised without being
fetchable — that agreement is what these tests pin.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import patch

# Pre-existing import cycle: ``subsessions/__init__`` -> ``delivery`` ->
# ``periodic`` -> ``scheduler`` -> ``subsessions.worker`` -> ``delivery`` (still
# initialising). It only bites when ``_skill_registry`` is the first thing in
# the process to enter that ring, which happens here but not in the running app
# — and it reproduces identically on main, so it is not part of this change.
# Entering from ``periodic`` completes the ring cleanly.
import pytest

import robotsix_chat.periodic  # noqa: F401
from robotsix_chat.chat.server.app import (
    _inject_skills,
    _skill_registry,
    build_skill_tools,
)
from robotsix_chat.config import Settings

#: PAGE_SIZE * 32 on x86-64 — the ceiling that made this change necessary.
MAX_ARG_STRLEN = 131_072


def _settings() -> Settings:
    return Settings()


def _read_skill(settings: Settings):
    (tool,) = build_skill_tools(settings)
    return tool


class TestSystemPromptSize:
    """The prompt must stay well clear of the argv ceiling."""

    def test_prompt_stays_far_below_the_argv_ceiling(self) -> None:
        """The regression this change exists to prevent.

        The prompt travels as one ``--system-prompt`` argv element, so crossing
        MAX_ARG_STRLEN makes ``execve`` fail with E2BIG and the turn never
        starts. Half the ceiling leaves room for the periodic preamble, which
        is what tipped it over on 2026-08-13.
        """
        prompt = _inject_skills(_settings(), "Base instruction.")
        assert len(prompt.encode()) < MAX_ARG_STRLEN // 2

    def test_prompt_advertises_skills_without_embedding_them(self) -> None:
        """Names in, bodies out."""
        settings = _settings()
        prompt = _inject_skills(settings, "Base instruction.")
        enabled = [n for on, n, _ in _skill_registry(settings) if on]
        if not enabled:
            return
        assert "read_skill" in prompt
        # A body is far longer than its index line; the prompt must carry the
        # latter, not the former.
        for name in enabled:
            assert f"**{name}**" in prompt


class TestReadSkillTool:
    """The read half — and its agreement with the index."""

    def test_returns_the_full_body(self) -> None:
        """The tool serves exactly what the loader produces."""
        settings = _settings()
        registry = {n: ld for on, n, ld in _skill_registry(settings) if on}
        if not registry:
            return
        name = min(registry)
        assert _read_skill(settings)(name) == registry[name]()

    def test_every_advertised_skill_is_fetchable(self) -> None:
        """The invariant: the index cannot advertise what the tool can't serve."""
        settings = _settings()
        prompt = _inject_skills(settings, "Base instruction.")
        read_skill = _read_skill(settings)
        for on, name, _ in _skill_registry(settings):
            if not on:
                continue
            if f"**{name}**" not in prompt:
                continue
            body = read_skill(name)
            assert body
            assert not body.startswith("No skill named")

    def test_unknown_name_names_the_valid_ones(self) -> None:
        """A dead end costs a wasted turn; a helpful error costs none."""
        settings = _settings()
        out = _read_skill(settings)("nope")
        assert "No skill named 'nope'" in out
        assert "Available skills:" in out


class TestReadSkillRetryAndCache:
    """Retry logic and stale-cache fallback for transient loader failures."""

    def test_retries_on_transient_failure(self) -> None:
        """A loader that fails twice then succeeds returns the body."""
        settings = _settings()
        enabled_names = [n for on, n, _ in _skill_registry(settings) if on]
        if not enabled_names:
            pytest.skip("No enabled skills to test retry logic.")
        target_name = enabled_names[0]

        call_count = 0

        def flaky_loader() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("transient failure")
            return "# Flaky Skill\n\nBody after retry.\n"

        with (
            patch(
                "robotsix_chat.chat.server.app._skill_registry",
                return_value=[(True, target_name, flaky_loader)],
            ),
            patch("robotsix_chat.chat.server.app.time.sleep") as mock_sleep,
        ):
            (read_skill,) = build_skill_tools(settings)
            result = read_skill(target_name)

        mock_sleep.assert_called()
        assert "Body after retry" in result
        assert call_count == 3

    def test_serves_cached_body_on_failure(self) -> None:
        """If a loader fails but we have a cached copy, serve the cache."""
        settings = _settings()
        enabled_names = [n for on, n, _ in _skill_registry(settings) if on]
        if not enabled_names:
            pytest.skip("No enabled skills to test cache fallback.")
        target_name = enabled_names[0]

        cached_body = "# Cached Skill\n\nCached body content.\n"
        call_count = 0

        def loader() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return cached_body
            raise RuntimeError("permanent failure")

        with (
            patch(
                "robotsix_chat.chat.server.app._skill_registry",
                return_value=[(True, target_name, loader)],
            ),
            patch("robotsix_chat.chat.server.app.time.sleep"),
        ):
            (read_skill,) = build_skill_tools(settings)
            # First call succeeds and populates the cache.
            assert read_skill(target_name) == cached_body
            # Second call fails but should serve the stale cached body.
            assert read_skill(target_name) == cached_body

        assert call_count == 4  # 1 success + 3 retries on the second call.

    def test_error_message_guides_agent_on_total_failure(self) -> None:
        """When all retries fail and no cache exists, the error guides the agent."""
        settings = _settings()
        enabled_names = [n for on, n, _ in _skill_registry(settings) if on]
        if not enabled_names:
            pytest.skip("No enabled skills to test total failure guidance.")
        target_name = enabled_names[0]

        def failing_loader() -> str:
            raise RuntimeError("permanent failure")

        with (
            patch(
                "robotsix_chat.chat.server.app._skill_registry",
                return_value=[(True, target_name, failing_loader)],
            ),
            patch("robotsix_chat.chat.server.app.time.sleep"),
        ):
            (read_skill,) = build_skill_tools(settings)
            result = read_skill(target_name)

        assert "could not be loaded after multiple attempts" in result
        assert "Inform the user" in result
        assert "temporarily unavailable" in result


class TestComponentSkills:
    """Roster component skills ride the same two halves as bundled skills."""

    _ROSTER: ClassVar[list[dict[str, str]]] = [
        {
            "id": "mail",
            "base_url": "http://mail:8080",
            "skill": (
                "---\nname: robotsix-auto-mail\n"
                "description: Mail triage board over HTTP.\n---\n\n"
                "## Read-only HTTP API\n\n- `GET /accounts` → …\n" + "x" * 20_000
            ),
        },
        {"id": "_error", "base_url": "", "skill": "", "_error": "fail"},
    ]

    def _settings(self) -> Settings:
        return Settings(central_deploy={"url": "http://cd:8100"})

    def test_prompt_indexes_component_skill_but_not_its_body(self) -> None:
        """Names in, bodies out — for component skills too."""
        settings = self._settings()
        with patch(
            "robotsix_chat.component_access.roster.fetch_roster_sync",
            return_value=self._ROSTER,
        ):
            prompt = _inject_skills(settings, "Base instruction.")
        assert "**mail**" in prompt
        assert "Mail triage board over HTTP." in prompt
        assert "read_skill(<component id>)" in prompt
        assert "GET /accounts" not in prompt
        assert "x" * 200 not in prompt

    def test_read_skill_serves_the_component_body(self) -> None:
        """The tool serves the roster skill verbatim, keyed by component id."""
        settings = self._settings()
        with patch(
            "robotsix_chat.component_access.roster.fetch_roster_sync",
            return_value=self._ROSTER,
        ):
            read_skill = _read_skill(settings)
            body = read_skill("mail")
        assert body == self._ROSTER[0]["skill"]

    def test_unreachable_roster_leaves_bundled_skills_intact(self) -> None:
        """A roster outage degrades to bundled skills only, never to a crash."""
        settings = self._settings()
        with patch(
            "robotsix_chat.component_access.roster.fetch_roster_sync",
            side_effect=RuntimeError("boom"),
        ):
            names = [n for on, n, _ in _skill_registry(settings) if on]
        assert "subsessions" in names
        assert "mail" not in names

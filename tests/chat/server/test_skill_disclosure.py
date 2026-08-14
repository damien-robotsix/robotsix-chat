"""The two halves of progressive disclosure must agree.

The system prompt advertises skills by name; ``read_skill`` serves the bodies.
Both read ``_skill_registry``, so a skill can never be advertised without being
fetchable — that agreement is what these tests pin.
"""

from __future__ import annotations

# Pre-existing import cycle: ``subsessions/__init__`` -> ``delivery`` ->
# ``autonomous`` -> ``runner`` -> ``subsessions.worker`` -> ``delivery`` (still
# initialising). It only bites when ``_skill_registry`` is the first thing in
# the process to enter that ring, which happens here but not in the running app
# — and it reproduces identically on main, so it is not part of this change.
# Entering from ``autonomous`` completes the ring cleanly.
import robotsix_chat.autonomous  # noqa: F401
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
        starts. Half the ceiling leaves room for the autonomous preamble, which
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

"""Tests for progressive skill disclosure.

Skill bodies used to be concatenated into the system prompt in full. Besides
costing tokens on every turn, that pushed the prompt toward a hard kernel
ceiling: the Claude Agent SDK spawns the CLI and passes the system prompt as a
single ``--system-prompt`` argv element, and ``MAX_ARG_STRLEN`` caps one
argument at ``PAGE_SIZE * 32`` (128 KiB on x86-64). On 2026-08-13 the bundled
skills reached 85 KB and autonomous sessions failed to start with ``E2BIG``.
"""

from __future__ import annotations

import pathlib

from robotsix_chat.skill_index import build_skill_index, summarize_skill

#: PAGE_SIZE * 32 on x86-64 — the kernel's per-argument ceiling.
MAX_ARG_STRLEN = 131_072


class TestSummarizeSkill:
    """Title + opening-paragraph extraction."""

    def test_extracts_title_and_opening_paragraph(self) -> None:
        """The opening of a skill is its routing information."""
        title, summary = summarize_skill(
            "# Mail (robotsix-auto-mail)\n"
            "\n"
            "The mail tools connect directly to the board server.\n"
            "\n"
            "## Endpoints\n"
            "Lots of detail that must NOT reach the index.\n"
        )
        assert title == "Mail (robotsix-auto-mail)"
        assert summary == "The mail tools connect directly to the board server."
        assert "Endpoints" not in summary

    def test_joins_a_wrapped_paragraph(self) -> None:
        """Skill intros are hard-wrapped; the index wants one line."""
        _, summary = summarize_skill("# T\n\nfirst line\nsecond line\n\nlater\n")
        assert summary == "first line second line"

    def test_truncates_to_the_budget(self) -> None:
        """Long intros are cut to the per-skill budget."""
        _, summary = summarize_skill("# T\n\n" + "x" * 999, max_chars=50)
        assert len(summary) == 50
        assert summary.endswith("…")

    def test_body_without_a_heading(self) -> None:
        """A skill with no ``# Title`` still contributes a summary."""
        title, summary = summarize_skill("Just prose, no heading.\n")
        assert title == ""
        assert summary == "Just prose, no heading."

    def test_skips_rules_and_tables_before_the_paragraph(self) -> None:
        """Horizontal rules and tables are not the intro."""
        title, summary = summarize_skill("# T\n\n---\n\nthe real intro\n")
        assert title == "T"
        assert summary == "the real intro"

    def test_empty_body(self) -> None:
        """An empty body yields empty strings, never a crash."""
        assert summarize_skill("") == ("", "")


class TestBuildSkillIndex:
    """The index advertises skills without embedding their bodies."""

    def _entries(self):
        return [
            ("alpha", lambda: "# Alpha\n\nDoes alpha things.\n\n## Detail\nSECRET\n"),
            ("beta", lambda: "# Beta\n\nDoes beta things.\n"),
        ]

    def test_lists_every_skill_by_name(self) -> None:
        """The name is the key the agent passes to ``read_skill``."""
        index = build_skill_index(self._entries())
        assert "**alpha**" in index
        assert "**beta**" in index

    def test_carries_summaries_not_bodies(self) -> None:
        """The whole point: the body must not ride in the prompt."""
        index = build_skill_index(self._entries())
        assert "Does alpha things." in index
        assert "SECRET" not in index

    def test_tells_the_agent_how_to_get_the_body(self) -> None:
        """An index without the retrieval instruction is a dead end."""
        assert "read_skill(name)" in build_skill_index(self._entries())

    def test_empty_when_nothing_is_enabled(self) -> None:
        """Callers append unconditionally, so this must be falsy."""
        """Callers append unconditionally, so this must be falsy, not a header."""
        assert build_skill_index([]) == ""
        assert build_skill_index([("x", lambda: "")]) == ""

    def test_a_broken_loader_does_not_break_the_turn(self) -> None:
        """A broken skill is skipped, not fatal to the turn."""

        def boom() -> str:
            raise RuntimeError("skill file missing")

        index = build_skill_index([("broken", boom), *self._entries()])
        assert "**broken**" not in index
        assert "**alpha**" in index

    def test_stays_small_against_the_real_skill_set(self) -> None:
        """Regression guard on the failure this replaced.

        The index must stay a small fraction of the concatenated bodies, so
        adding a skill no longer moves the prompt toward MAX_ARG_STRLEN.
        """
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "robotsix_chat"
        skills = sorted(root.rglob("skill.md"))
        assert skills, "expected bundled skill.md files"

        concatenated = sum(len(p.read_text().encode()) for p in skills)
        index = build_skill_index(
            [(p.parent.name, (lambda p=p: p.read_text())) for p in skills]
        )
        assert len(index.encode()) < concatenated // 4
        assert len(index.encode()) < MAX_ARG_STRLEN // 8

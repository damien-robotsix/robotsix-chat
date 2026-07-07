"""Tests for the github component skill loader.

:func:`load_github_skill` — verifies the shipped skill.md is loadable and
that a missing file gracefully returns an empty string.
"""

from __future__ import annotations

from unittest.mock import patch

from robotsix_chat.github import load_github_skill


def test_load_github_skill_returns_non_empty_markdown() -> None:
    """The shipped skill.md is loadable and contains expected content."""
    skill = load_github_skill()
    assert len(skill) > 100
    assert "component_request" in skill
    assert "github" in skill.lower()
    assert "Safety" in skill
    assert "POST /repos" in skill
    assert "Confirmation gate" in skill


def test_load_github_skill_missing_file_returns_empty_string() -> None:
    """When skill.md is missing, load_github_skill returns an empty string."""
    with patch("pathlib.Path.read_text", side_effect=FileNotFoundError):
        result = load_github_skill()
        assert result == ""

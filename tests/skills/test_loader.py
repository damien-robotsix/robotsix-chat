"""Tests for skill manifest loading and discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_chat.skills.loader import _resolve_env_vars, discover_manifests

# ---------------------------------------------------------------------------
# _resolve_env_vars
# ---------------------------------------------------------------------------


def test_resolve_env_vars_no_vars() -> None:
    """Plain text passes through unchanged."""
    assert _resolve_env_vars("plain text") == "plain text"


def test_resolve_env_vars_single_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single ``${VAR}`` reference is replaced."""
    monkeypatch.setenv("MY_TOKEN", "abc123")
    assert _resolve_env_vars("token: ${MY_TOKEN}") == "token: abc123"


def test_resolve_env_vars_unset_left_as_is() -> None:
    """Unset variables stay literal so the operator can spot them."""
    assert _resolve_env_vars("${MISSING_VAR}") == "${MISSING_VAR}"


def test_resolve_env_vars_multiple(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple variables are all resolved."""
    monkeypatch.setenv("HOST", "broker.example.com")
    monkeypatch.setenv("PORT", "443")
    result = _resolve_env_vars("${HOST}:${PORT}")
    assert result == "broker.example.com:443"


# ---------------------------------------------------------------------------
# discover_manifests
# ---------------------------------------------------------------------------


def test_discover_missing_directory_returns_empty() -> None:
    """Non-existent directory returns an empty list."""
    assert discover_manifests("/nonexistent/path/xyz") == []


def test_discover_empty_directory_returns_empty(tmp_path: Path) -> None:
    """An empty directory returns an empty list."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    assert discover_manifests(str(skills_dir)) == []


def test_discover_skips_non_yaml_files(tmp_path: Path) -> None:
    """Files not matching ``*.skill.yaml`` are ignored."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "README.md").write_text("# Skills")
    assert discover_manifests(str(skills_dir)) == []


def test_discover_parses_valid_manifest(tmp_path: Path) -> None:
    """A valid manifest is parsed into a SkillManifest."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "test.skill.yaml").write_text(
        """\
skill_id: test
display_name: "Test Skill"
enabled: true
broker:
  target_agent_id: test-agent
capabilities:
  - name: query
    description: "Query the test agent."
"""
    )
    manifests = discover_manifests(str(skills_dir))
    assert len(manifests) == 1
    m = manifests[0]
    assert m.skill_id == "test"
    assert m.enabled is True
    assert m.broker is not None
    assert m.broker.target_agent_id == "test-agent"
    assert len(m.capabilities) == 1
    assert m.capabilities[0].name == "query"


def test_discover_resolves_env_vars_in_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``${VAR}`` references in broker tokens are resolved at load time."""
    monkeypatch.setenv("TEST_TOKEN", "secret123")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "test.skill.yaml").write_text(
        """\
skill_id: test
broker:
  target_agent_id: ta
  token: "${TEST_TOKEN}"
"""
    )
    manifests = discover_manifests(str(skills_dir))
    assert manifests[0].broker is not None
    assert manifests[0].broker.token == "secret123"


def test_discover_skips_invalid_yaml(tmp_path: Path) -> None:
    """Unparsable YAML is skipped with a warning."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "bad.skill.yaml").write_text(":: not valid yaml ::")
    manifests = discover_manifests(str(skills_dir))
    assert manifests == []


def test_discover_skips_non_mapping_yaml(tmp_path: Path) -> None:
    """A YAML list (not a mapping) is skipped."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "list.skill.yaml").write_text("- item1\n- item2\n")
    manifests = discover_manifests(str(skills_dir))
    assert manifests == []


def test_discover_multiple_manifests_sorted(tmp_path: Path) -> None:
    """Manifests are returned in alphabetical filename order."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "b.skill.yaml").write_text(
        "skill_id: b\nbroker:\n  target_agent_id: b\n"
    )
    (skills_dir / "a.skill.yaml").write_text(
        "skill_id: a\nbroker:\n  target_agent_id: a\n"
    )
    manifests = discover_manifests(str(skills_dir))
    assert [m.skill_id for m in manifests] == ["a", "b"]

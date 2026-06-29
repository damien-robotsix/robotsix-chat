"""Tests for the skills public API (__init__.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_chat.skills import build_skill_tools, load_skills
from tests.common.agent_comm_fakes import _install_fake_agent_comm


def _write_manifest(dir_path: Path, skill_id: str, enabled: bool = True) -> None:
    """Write a minimal valid ``*.skill.yaml`` manifest to *dir_path*."""
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / f"{skill_id}.skill.yaml").write_text(
        f"""\
skill_id: {skill_id}
display_name: "{skill_id} skill"
enabled: {str(enabled).lower()}
broker:
  target_agent_id: {skill_id}-agent
capabilities:
  - name: query
    description: "Query {skill_id}."
"""
    )


# ---------------------------------------------------------------------------
# load_skills
# ---------------------------------------------------------------------------


def test_load_skills_missing_dir() -> None:
    """Non-existent directory returns an empty list."""
    assert load_skills("/nonexistent/skills") == []


def test_load_skills_skips_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled manifests produce no skills."""
    _install_fake_agent_comm(monkeypatch, reply="ok")
    _write_manifest(tmp_path, "disabled-skill", enabled=False)
    skills = load_skills(str(tmp_path))
    assert len(skills) == 0


def test_load_skills_loads_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled manifests produce one BrokerSkill each."""
    _install_fake_agent_comm(monkeypatch, reply="ok")
    _write_manifest(tmp_path, "skill-a", enabled=True)
    _write_manifest(tmp_path, "skill-b", enabled=True)
    skills = load_skills(str(tmp_path))
    assert len(skills) == 2
    assert {s.skill_id for s in skills} == {"skill-a", "skill-b"}


def test_load_skills_skips_invalid_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid manifest files are skipped, valid ones are still loaded."""
    _install_fake_agent_comm(monkeypatch, reply="ok")
    _write_manifest(tmp_path, "good", enabled=True)
    (tmp_path / "bad.skill.yaml").write_text(":: not yaml ::")
    skills = load_skills(str(tmp_path))
    assert len(skills) == 1
    assert skills[0].skill_id == "good"


# ---------------------------------------------------------------------------
# build_skill_tools
# ---------------------------------------------------------------------------


def test_build_skill_tools_none_dir() -> None:
    """``None`` manifests_dir returns an empty list."""
    tools = build_skill_tools(None)
    assert tools == []


def test_build_skill_tools_default_dir() -> None:
    """The default ``config/skills/`` returns [] when absent."""
    tools = build_skill_tools()
    assert tools == []


def test_build_skill_tools_from_enabled_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled skills produce tools with prefixed names."""
    _install_fake_agent_comm(monkeypatch, reply="ok")
    _write_manifest(tmp_path, "s1", enabled=True)
    tools = build_skill_tools(str(tmp_path))
    assert len(tools) == 1
    assert tools[0].__name__ == "s1_query"

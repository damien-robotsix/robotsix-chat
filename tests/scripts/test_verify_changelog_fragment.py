"""Tests for the post-implementation changelog fragment verification script."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "verify_changelog_fragment.py"
)

_TICKET_ID = "20260813T102912Z-test-fragment-7a95"


@pytest.fixture()
def verify() -> Any:
    """Load ``scripts/verify_changelog_fragment.py`` as an importable module."""
    spec = importlib.util.spec_from_file_location("verify_changelog_fragment", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_changelog_fragment"] = module
    spec.loader.exec_module(module)
    return module


def _write_modules_yaml(repo: Path, patterns: list[str]) -> None:
    """Write a minimal ``docs/modules.yaml`` listing *patterns* under ``paths``."""
    docs = repo / "docs"
    docs.mkdir(exist_ok=True)
    lines = [
        "modules:",
        "  - id: housekeeping",
        "    description: housekeeping",
        "    paths:",
        *(f"      - {pattern}" for pattern in patterns),
    ]
    (docs / "modules.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_skip_short_circuits_without_fragment(tmp_path: Path, verify: Any) -> None:
    """``skip=True`` passes even when no fragment directory exists."""
    assert verify.verify_fragment(tmp_path, _TICKET_ID, skip=True) == 0


def test_missing_fragment_directory_returns_one(tmp_path: Path, verify: Any) -> None:
    """A missing fragment directory is a hard failure."""
    assert verify.verify_fragment(tmp_path, _TICKET_ID) == 1


def test_missing_fragment_file_returns_one(tmp_path: Path, verify: Any) -> None:
    """An empty fragment directory is a hard failure."""
    (tmp_path / "changelog.d").mkdir()
    _write_modules_yaml(tmp_path, ["changelog.d/**/*"])
    assert verify.verify_fragment(tmp_path, _TICKET_ID) == 1


def test_registered_fragment_returns_zero(tmp_path: Path, verify: Any) -> None:
    """A fragment covered by ``changelog.d/**/*`` passes."""
    fragment_dir = tmp_path / "changelog.d"
    fragment_dir.mkdir()
    (fragment_dir / f"{_TICKET_ID}.misc.md").write_text(
        "Internal fix.\n", encoding="utf-8"
    )
    _write_modules_yaml(tmp_path, ["changelog.d/**/*"])
    assert verify.verify_fragment(tmp_path, _TICKET_ID) == 0


def test_unregistered_fragment_returns_one(tmp_path: Path, verify: Any) -> None:
    """A fragment with no matching ``docs/modules.yaml`` path fails."""
    fragment_dir = tmp_path / "changelog.d"
    fragment_dir.mkdir()
    (fragment_dir / f"{_TICKET_ID}.misc.md").write_text(
        "Internal fix.\n", encoding="utf-8"
    )
    _write_modules_yaml(tmp_path, ["scripts/*"])
    assert verify.verify_fragment(tmp_path, _TICKET_ID) == 1


def test_directory_override(tmp_path: Path, verify: Any) -> None:
    """``--directory`` (via ``directory=``) checks an alternate fragment dir."""
    fragment_dir = tmp_path / "changes"
    fragment_dir.mkdir()
    (fragment_dir / f"{_TICKET_ID}.feature.md").write_text(
        "New feature.\n", encoding="utf-8"
    )
    _write_modules_yaml(tmp_path, ["changes/**/*"])
    assert verify.verify_fragment(tmp_path, _TICKET_ID, directory="changes") == 0

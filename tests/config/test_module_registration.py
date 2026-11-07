"""Guard: every file under ``src/robotsix_chat/`` and ``tests/`` is registered.

Mirrors the ``modules-registration`` pre-commit hook
(``robotsix-modules check-registration docs/modules.yaml --root .``) so the
drift is caught by ``uv run pytest`` before push, instead of only in the
``Baseline (shared) / Modules drift`` CI job.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from robotsix_modules import check_registration, load_taxonomy

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULES_YAML = REPO_ROOT / "docs" / "modules.yaml"
GUARDED_PREFIXES = ("src/robotsix_chat/", "tests/")


def _git_ls_files(*args: str) -> list[str]:
    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout; registration check needs git ls-files")
    result = subprocess.run(  # noqa: S603 - fixed argv, no user input
        [git, "ls-files", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _candidate_files() -> list[str]:
    """Tracked files plus new, not-yet-committed files under guarded dirs."""
    tracked = _git_ls_files()
    untracked = [
        path
        for path in _git_ls_files("--others", "--exclude-standard")
        if path.startswith(GUARDED_PREFIXES)
    ]
    return sorted(set(tracked) | set(untracked))


def test_all_files_registered_in_modules_yaml() -> None:
    taxonomy = load_taxonomy(MODULES_YAML)
    findings = check_registration(taxonomy, REPO_ROOT, tracked_files=_candidate_files())
    messages = [finding.message for finding in findings]
    assert not messages, (
        "docs/modules.yaml is out of sync — register new files under the owning "
        "module's `paths:` (verify with `uv run robotsix-modules "
        "check-registration docs/modules.yaml --root .`):\n" + "\n".join(messages)
    )


@pytest.mark.parametrize(
    "unregistered",
    [
        "src/robotsix_chat/__unregistered_probe__.py",
        "tests/__unregistered_probe__/test_probe.py",
    ],
)
def test_unregistered_file_is_reported(unregistered: str) -> None:
    taxonomy = load_taxonomy(MODULES_YAML)
    findings = check_registration(
        taxonomy, REPO_ROOT, tracked_files=[*_candidate_files(), unregistered]
    )
    assert any(
        finding.kind == "unclassified_file" and finding.file == unregistered
        for finding in findings
    )

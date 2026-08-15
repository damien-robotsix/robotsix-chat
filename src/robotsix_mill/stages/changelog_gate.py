"""Programmatic pre-summary gate for changelog fragments.

The implement agent's free-text summary can claim a changelog fragment
that never landed on disk.  This module closes that gap with a
programmatic gate: run the repo's own
``scripts/verify_changelog_fragment.py <ticket_id>`` on the working tree
before the implement stage accepts the pass, and treat a non-zero exit
as a blocker.

The gate is deliberately repo-agnostic and never raises: a repo that
does not carry the verification script simply passes through, and
subprocess/git failures degrade to a pass so the implement loop is never
crashed by a broken working tree.

``run_changelog_fragment_gate`` is pure stdlib so tests can import it
directly from this source file without the installed ``robotsix_mill``
package (mirroring ``tests/stages/test_towncrier.py``).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("robotsix_mill.stages.changelog_gate")

_VERIFY_SCRIPT = "scripts/verify_changelog_fragment.py"

# Paths that are considered "internal-only": no user-facing changelog
# fragment is required.  Mirrors the repo's changelog convention (test
# files, CI workflows, and tooling) plus the mill-side shadow package
# that lives under ``src/robotsix_mill/``.
_INTERNAL_PREFIXES: tuple[str, ...] = (
    "tests/",
    ".github/",
    "scripts/",
    "src/robotsix_mill/",
    "_stubs/",
    "tasks/",
    ".robotsix-mill/",
)

# Repo-root files that are tooling/config rather than user-facing content.
_INTERNAL_FILES: frozenset[str] = frozenset(
    {
        "CHANGELOG.md",
        "docs/modules.yaml",
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        "package.json",
        "package-lock.json",
        "vitest.config.js",
        "vulture_whitelist.py",
        "memory-ledger.json",
        "typos.toml",
        ".pre-commit-config.yaml",
        ".gitignore",
        ".dockerignore",
        ".editorconfig",
        ".markdownlint-cli2.jsonc",
        ".markdownlintignore",
        ".python-version",
        ".release-please-manifest.json",
        ".trufflehog.yaml",
        "release-please-config.json",
    }
)

_TIMEOUT_SECONDS = 120


def _is_internal_path(path: str) -> bool:
    """Return True when *path* is an internal-only (non user-facing) path."""
    return path.startswith(_INTERNAL_PREFIXES) or path in _INTERNAL_FILES


def _changed_paths(repo_dir: Path) -> set[str]:
    """Return repo-relative paths with working-tree changes.

    Combines tracked modifications (``git diff --name-only HEAD``) and
    untracked files (``git ls-files --others --exclude-standard``) so a
    new fragment or source file is visible to the classifier.  A git
    failure degrades to an empty set, which the caller treats as "no
    changes" (skip).
    """
    paths: set[str] = set()
    for argv in (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        try:
            proc = subprocess.run(  # noqa: S603
                argv,
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError, OSError:
            continue
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if stripped:
                paths.add(stripped)
    return paths


def _is_internal_only_change(repo_dir: Path) -> bool:
    """Return True when the working tree needs no changelog fragment.

    An empty working tree (e.g. a ``no_change_needed`` pass) and a diff
    that touches only internal-only paths both pass through.
    """
    paths = _changed_paths(repo_dir)
    if not paths:
        return True
    return all(_is_internal_path(p) for p in paths)


def run_changelog_fragment_gate(repo_dir: Path, ticket_id: str) -> str | None:
    """Run the repo's changelog-fragment verification script.

    Returns ``None`` when the gate passes (fragment present, internal-only
    change, or the script is absent), or a short human-readable error
    string explaining what is missing and how to fix it.  Never raises.
    """
    repo_dir = Path(repo_dir)
    script = repo_dir / _VERIFY_SCRIPT
    if not script.is_file():
        return None

    argv = [sys.executable, str(script), str(ticket_id)]
    if _is_internal_only_change(repo_dir):
        # Internal-only: the script's ``--skip`` short-circuit passes
        # through without requiring a fragment.
        argv.append("--skip")

    try:
        proc = subprocess.run(  # noqa: S603
            argv,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"changelog fragment verification timed out after {_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return f"changelog fragment verification could not run: {exc}"

    if proc.returncode == 0:
        return None

    detail = (proc.stderr or proc.stdout or "").strip()
    if not detail:
        detail = f"scripts/verify_changelog_fragment.py exited {proc.returncode}"
    return (
        "changelog fragment verification failed:\n"
        f"{detail}\n"
        "Create the missing fragment at "
        f"`changelog.d/{ticket_id}.<fragment_type>.md` "
        "(use the `add_changelog_fragment` tool) and ensure it is "
        "registered in `docs/modules.yaml`, then re-run "
        f"`scripts/verify_changelog_fragment.py {ticket_id}`."
    )

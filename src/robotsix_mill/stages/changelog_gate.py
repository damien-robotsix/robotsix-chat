"""Programmatic changelog-fragment gates.

The implement agent's free-text summary can claim a changelog fragment
that never landed on disk.  This module closes that gap with two
programmatic gates:

* The **pre-summary gate** runs the repo's own
  ``scripts/verify_changelog_fragment.py <ticket_id>`` on the working tree
  before the implement stage accepts the pass, and treats a non-zero exit
  as a blocker.

* The **review rebuttal gate** re-checks the implement agent's
  ``implement.md`` rebuttal after the ``ready`` transition: if the
  rebuttal claims a fragment was added but no ``changelog.d/<slug>*``
  file is present in the committed branch diff, the review stage must
  reject the claim instead of trusting the free-text summary.

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
import re
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


# ---------------------------------------------------------------------------
# Review rebuttal gate
# ---------------------------------------------------------------------------

# Affirmative fragment-claim phrases in the implement agent's free-text
# rebuttal.  Mirrors the patterns used by the implement stage's
# ``_verify_summary_claims`` so a claim detected there is detected here.
_CHANGELOG_CLAIM_AFTER_RE = re.compile(
    r"\bchangelog(?:\s+fragment)?(?:\s+file)?\s+"
    r"(?:created|added|written|generated|registered)\b",
    re.IGNORECASE,
)
_CHANGELOG_CLAIM_BEFORE_RE = re.compile(
    r"\b(?:created|added|written|generated|registered)\s+"
    r"(?:a\s+)?(?:new\s+)?changelog(?:\s+fragment)?(?:\s+file)?\b",
    re.IGNORECASE,
)
_CHANGELOG_D_PATH_RE = re.compile(r"\bchangelog\.d/[\w./-]+")

# A trailing negation word right before a claim phrase ("no changelog
# fragment added", "skip changelog") means the rebuttal is asserting the
# fragment was NOT needed — not that it was created.
_NEGATION_RE = re.compile(r"\b(?:no|not|skip|without|avoid)\s*$", re.IGNORECASE)

# Conventional towncrier fragment directories.
_FRAGMENT_DIRS: tuple[str, ...] = ("changelog.d", "changelog", "changes")


def _ticket_slug(ticket_id: str) -> str:
    """Filesystem-safe stem used for a ticket's changelog fragment file."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in ticket_id)
    return safe.strip("-") or "entry"


def _claims_changelog_fragment(text: str) -> bool:
    """Return True when *text* affirmatively claims a fragment was added."""
    if _CHANGELOG_D_PATH_RE.search(text or ""):
        return True
    for regex in (_CHANGELOG_CLAIM_AFTER_RE, _CHANGELOG_CLAIM_BEFORE_RE):
        for match in regex.finditer(text or ""):
            prefix = text[max(0, match.start() - 40) : match.start()].lower()
            if _NEGATION_RE.search(prefix):
                continue
            return True
    return False


def _fragment_in_modified_paths(modified_paths: list[str], slug: str) -> bool:
    """Return True when a committed path is a ``<dir>/<slug>*`` fragment."""
    for path in modified_paths or []:
        parts = path.replace("\\", "/").split("/")
        if len(parts) < 2:
            continue
        parent, name = parts[-2], parts[-1]
        if parent not in _FRAGMENT_DIRS:
            continue
        if name == slug or name.startswith(f"{slug}."):
            return True
    return False


def _fragment_on_working_tree(repo_dir: Path, slug: str) -> bool:
    """Return True when a ``<dir>/<slug>*`` fragment exists on disk."""
    for directory in _FRAGMENT_DIRS:
        fragment_dir = repo_dir / directory
        if fragment_dir.is_dir() and any(fragment_dir.glob(f"{slug}*")):
            return True
    return False


def run_review_changelog_fragment_gate(
    ticket_id: str,
    repo_dir: Path,
    rebuttal_text: str,
    modified_paths: list[str],
) -> str | None:
    """Verify a changelog-fragment claim in the implement rebuttal.

    Returns ``None`` when the gate passes — the rebuttal makes no
    affirmative fragment claim, or the claimed fragment is present in the
    committed branch diff — or a short human-readable error string when a
    fragment was claimed but is not committed.  Never raises.
    """
    if not _claims_changelog_fragment(rebuttal_text):
        return None

    slug = _ticket_slug(ticket_id)
    if _fragment_in_modified_paths(modified_paths, slug):
        return None

    location = (
        "exists in the working tree but is not committed"
        if _fragment_on_working_tree(Path(repo_dir), slug)
        else "does not exist on disk"
    )
    return (
        "changelog fragment claimed in the implement rebuttal but not "
        f"committed: `changelog.d/{slug}*` {location}. "
        "Create the missing fragment (use the `add_changelog_fragment` "
        "tool) and commit it so the merged branch actually ships it."
    )

#!/usr/bin/env python3
"""Post-implementation gate: verify a changelog fragment exists and is registered.

Usage::

    python scripts/verify_changelog_fragment.py <ticket_id> [--directory DIR] [--skip]

Exit codes:

- ``0`` — the fragment exists and is registered in ``docs/modules.yaml``, or
  the check was skipped for an internal-only change.
- ``1`` — a user-facing change is missing its ``changelog.d/<ticket_id>*``
  fragment, or the fragment is not registered in ``docs/modules.yaml``.
- ``2`` — usage error (missing ticket id, unknown flag, etc.).

The implement agent runs this before emitting its status summary so it can
never claim a fragment exists when none was actually written to disk.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# YAML list items under ``paths:`` — glob patterns like ``changelog.d/**/*``
# or literal paths like ``src/robotsix_chat/__init__.py``.
_PATH_ENTRY_RE = re.compile(r"^\s*-\s+(\S+)", re.MULTILINE)

_GLOB_MAGIC = frozenset("*?[")


def _towncrier_directory(repo_root: Path) -> str:
    """Return the changelog fragment directory from ``[tool.towncrier]``.

    Falls back to ``changelog.d`` when ``pyproject.toml`` is absent or has
    no towncrier config (this repo's conventional directory).
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return "changelog.d"
    try:
        import tomllib

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return "changelog.d"
    towncrier = (data.get("tool", {}) or {}).get("towncrier")
    if not towncrier:
        return "changelog.d"
    return str(towncrier.get("directory") or "changelog.d")


def _literal_prefix(pattern: str) -> str:
    """Return the non-glob prefix of *pattern* (up to the first magic char)."""
    first = next(
        (i for i, ch in enumerate(pattern) if ch in _GLOB_MAGIC),
        len(pattern),
    )
    return pattern[:first]


def _fragment_is_registered(manifest: Path, fragment_rel: str) -> bool:
    """Return True when *manifest* lists (or glob-covers) *fragment_rel*."""
    text = manifest.read_text(encoding="utf-8")
    patterns = _PATH_ENTRY_RE.findall(text)
    target = (manifest.parent.parent / fragment_rel).resolve()

    for pattern in patterns:
        if pattern == fragment_rel:
            return True
        if _literal_prefix(pattern) == pattern:
            continue  # no glob magic — a non-matching literal path
        # Only expand globs whose literal prefix is a prefix of the target
        # path — avoids walking the whole tree for patterns that can't match.
        prefix = _literal_prefix(pattern)
        if prefix and not fragment_rel.startswith(prefix):
            continue
        try:
            for matched in (manifest.parent.parent).glob(pattern):
                if matched.resolve() == target:
                    return True
        except OSError, ValueError:
            continue

    return False


def verify_fragment(
    repo_root: Path,
    ticket_id: str,
    *,
    directory: str | None = None,
    skip: bool = False,
) -> int:
    """Verify a ``changelog.d/<ticket_id>*`` fragment exists and is registered.

    Returns 0 when the check passes and 1 when it fails.  ``skip=True``
    short-circuits for internal-only changes that need no fragment.
    """
    if skip:
        print(
            "verify_changelog_fragment: skipping — internal-only change;"
            " no changelog fragment required.",
            file=sys.stderr,
        )
        return 0

    if not ticket_id:
        print(
            "verify_changelog_fragment: ERROR: empty ticket id",
            file=sys.stderr,
        )
        return 1

    fragment_dir_name = directory or _towncrier_directory(repo_root)
    fragment_dir = repo_root / fragment_dir_name
    if not fragment_dir.is_dir():
        print(
            "verify_changelog_fragment: ERROR: fragment directory"
            f" {fragment_dir_name!r} not found — expected a"
            f" `{fragment_dir_name}/<ticket_id>*` fragment.",
            file=sys.stderr,
        )
        return 1

    fragments = sorted(fragment_dir.glob(f"{ticket_id}*"))
    if not fragments:
        print(
            "verify_changelog_fragment: ERROR: no changelog fragment found"
            f" for {ticket_id!r} — expected a"
            f" `{fragment_dir_name}/{ticket_id}*` file.",
            file=sys.stderr,
        )
        return 1

    rel_fragments = [f.relative_to(repo_root) for f in fragments]
    manifest = repo_root / "docs" / "modules.yaml"
    if manifest.is_file():
        unregistered = [
            rel
            for rel in rel_fragments
            if not _fragment_is_registered(manifest, rel.as_posix())
        ]
        if unregistered:
            for rel in unregistered:
                print(
                    "verify_changelog_fragment: ERROR: fragment"
                    f" {rel.as_posix()} is not registered in"
                    " docs/modules.yaml.",
                    file=sys.stderr,
                )
            return 1
    else:
        print(
            "verify_changelog_fragment: note: docs/modules.yaml not found —"
            " skipping registration check.",
            file=sys.stderr,
        )

    names = ", ".join(rel.as_posix() for rel in rel_fragments)
    print(f"verify_changelog_fragment: OK — fragment(s) found: {names}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and run the verification."""
    parser = argparse.ArgumentParser(
        description="Verify a changelog fragment exists and is registered.",
    )
    parser.add_argument(
        "ticket_id",
        help="Ticket identifier (e.g. 20260813T102912Z-...)",
    )
    parser.add_argument(
        "--directory",
        default=None,
        help=(
            "Changelog fragment directory"
            " (default: from pyproject.toml or `changelog.d`)."
        ),
    )
    parser.add_argument(
        "--skip",
        action="store_true",
        help="Short-circuit: the change is internal-only and needs no fragment.",
    )
    args = parser.parse_args(argv)
    return verify_fragment(
        _REPO_ROOT,
        args.ticket_id,
        directory=args.directory,
        skip=args.skip,
    )


if __name__ == "__main__":
    raise SystemExit(main())

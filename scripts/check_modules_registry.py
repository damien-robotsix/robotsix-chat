#!/usr/bin/env python3
"""Fail when docs/modules.yaml drifts from the tracked file tree.

Repo-baseline standard: every file is registered in ``docs/modules.yaml``
under exactly one module. This check blocks structural drift in CI; the mill's
``module_curator`` periodic workflow maintains descriptions and ownership.

Checked trees: ``src/``, ``tests/``, ``docs/`` (the manifest itself is
exempt). Failures: a tracked file registered nowhere, a registered path no
longer tracked (stale), or a path registered under more than one module.
"""

from __future__ import annotations

import subprocess  # nosec B404 — fixed argv, no user input
import sys
from collections import Counter
from pathlib import Path

import yaml

CHECKED_TREES = ("src", "tests", "docs")
EXEMPT = {"docs/modules.yaml"}
MANIFEST = Path("docs/modules.yaml")


def tracked_files() -> set[str]:
    """Return git-tracked files under the checked trees, minus exemptions."""
    out = subprocess.run(  # noqa: S603  # nosec B603, B607 — fixed argv, no user input
        ["git", "ls-files", "--", *CHECKED_TREES],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {line for line in out.splitlines() if line and line not in EXEMPT}


def main() -> int:
    """Compare the manifest against the tracked tree; report drift."""
    modules = yaml.safe_load(MANIFEST.read_text())["modules"]
    registered = Counter(path for module in modules for path in module["paths"])
    tracked = tracked_files()

    unregistered = sorted(tracked - registered.keys())
    stale = sorted(registered.keys() - tracked)
    duplicated = sorted(path for path, n in registered.items() if n > 1)

    for label, paths in (
        ("unregistered (add to docs/modules.yaml)", unregistered),
        ("stale (remove from docs/modules.yaml)", stale),
        ("registered under more than one module", duplicated),
    ):
        for path in paths:
            print(f"::error::modules.yaml drift — {label}: {path}")

    if unregistered or stale or duplicated:
        return 1
    print(f"modules.yaml OK: {sum(registered.values())} paths, {len(modules)} modules")
    return 0


if __name__ == "__main__":
    sys.exit(main())

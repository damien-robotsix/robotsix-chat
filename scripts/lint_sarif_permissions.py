#!/usr/bin/env python3
"""CI gate: verify SARIF-uploading workflows declare ``security-events: write``.

Called by the shared ``lint-workflows.yml`` reusable workflow (from
robotsix-github-workflows).  Reads the ``SARIF_WORKFLOWS`` env var
(space-separated workflow filenames relative to ``.github/workflows/``)
and exits non-zero if any listed workflow is missing the required
permission.

No third-party dependencies — uses only the stdlib so it runs with the
bare ``python3`` available on GitHub-hosted runners without a ``pip
install`` step.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

WORKFLOW_DIR = Path(".github/workflows")


def _has_security_events_write(path: Path) -> bool:
    """Return True if *path* declares ``security-events: write`` anywhere."""
    text = path.read_text()
    # Simple substring check — sufficient for YAML where the permission key
    # is always a top-level ``security-events:`` mapping key inside a
    # ``permissions:`` block.  A full YAML parse would need PyYAML, which
    # is not available on the bare runner Python.
    return "security-events:" in text and ": write" in text


def main() -> int:
    sarif_workflows = os.environ.get("SARIF_WORKFLOWS", "").split()
    if not sarif_workflows:
        print("No SARIF workflows configured; nothing to check.")
        return 0

    errors: list[str] = []
    for wf_name in sarif_workflows:
        wf_path = WORKFLOW_DIR / wf_name
        if not wf_path.exists():
            errors.append(f"Workflow file not found: {wf_path}")
            continue

        if not _has_security_events_write(wf_path):
            errors.append(
                f"{wf_name}: missing 'security-events: write' permission. "
                "SARIF upload (e.g. CodeQL, Trivy) requires this permission."
            )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        "All SARIF workflows have security-events: write permission."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

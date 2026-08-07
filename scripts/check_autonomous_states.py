#!/usr/bin/env python3
"""CI gate: verify AutonomousState strings in the browser UI match Python constants.

Extracts ``AutonomousState`` enum values from
``src/robotsix_chat/autonomous/models.py``, then scans
``src/robotsix_chat/ui/static/chat.js`` for ``autonomous_state`` /
``aState`` comparisons that use bare string literals.  Exits non-zero when:

1. A canonical AutonomousState value is **missing** from chat.js
   (Python renamed → frontend silently broken).
2. A bare string literal in a ``aState === "..."`` /
   ``autonomous_state === "..."`` comparison in chat.js is
   **not** one of the canonical values (typo, stale reference,
   orphaned string).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from _check_common import (
    iter_matching_strings,
    parse_str_enum,
    run_consistency_checks,
)

# ---------------------------------------------------------------------------
# Step 1 — extract canonical AutonomousState values from models.py
# ---------------------------------------------------------------------------

# Match ``NAME = "value"`` inside a class body.
_STATE_VALUE_RE = re.compile(r'^\s+(\w+)\s*=\s*"(?P<value>[^"]+)"$')

# Matches the start of the AutonomousState class.
_CLASS_HEADER_RE = re.compile(r"^class AutonomousState\((?:enum\.)?StrEnum\):")

# ---------------------------------------------------------------------------
# Step 2 — find AutonomousState-looking string literals in chat.js
# ---------------------------------------------------------------------------

# aState === "planning"  /  autonomous_state === "proposal"
_STATE_COMPARISON_RE = re.compile(
    r'(?:aState|autonomous_state)\s*[=!]==\s*"(?P<state>[a-z_][a-z_0-9]*)"'
)


def main() -> int:
    """Check AutonomousState string consistency and return 0 (ok) or 1 (violations)."""
    repo_root = Path(__file__).resolve().parent.parent
    models_py = repo_root / "src" / "robotsix_chat" / "autonomous" / "models.py"
    chat_js = repo_root / "src" / "robotsix_chat" / "ui" / "static" / "chat.js"

    canonical = parse_str_enum(models_py, _CLASS_HEADER_RE, _STATE_VALUE_RE)
    if not canonical:
        print(
            "ERROR: no AutonomousState members found in"
            f" {models_py.relative_to(repo_root)}",
            file=sys.stderr,
        )
        return 1

    canonical_values = set(canonical.values())
    js_values = set(iter_matching_strings(chat_js, _STATE_COMPARISON_RE))

    violations = run_consistency_checks(
        canonical_values=canonical_values,
        scanned_values=js_values,
        missing_label=(
            "AutonomousState values missing from chat.js"
            " (aState / autonomous_state comparisons):"
        ),
        unrecognised_label=(
            "Unrecognised AutonomousState strings in chat.js"
            " (aState / autonomous_state comparisons — no matching Python constant):"
        ),
        report_names_for_value=lambda val: ", ".join(
            k for k, v in canonical.items() if v == val
        ),
    )

    if violations:
        print(
            "\nRun `python scripts/check_autonomous_states.py` locally to reproduce.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

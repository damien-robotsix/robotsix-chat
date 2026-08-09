#!/usr/bin/env python3
"""CI gate: verify SubsessionStatus strings in the browser UI match Python constants.

Extracts ``SubsessionStatus`` enum values from
``src/robotsix_chat/subsessions/models.py``, then scans
``src/robotsix_chat/ui/static/chat.js`` for JavaScript ``sub.status``
comparisons and fallback defaults that use bare string literals.
Exits non-zero when:

- A ``sub.status === "..."`` / ``|| "..."`` string literal in the JS
  is **not** one of the canonical values (typo, stale reference,
  orphaned string).

Note: the reverse check (canonical status missing from chat.js) is
deliberately skipped — some statuses (e.g. ``"sleeping"``,
``"waiting"``) may be handled implicitly by an ``else`` fallthrough
rather than an explicit comparison.
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
# Step 1 — extract canonical SubsessionStatus values from models.py
# ---------------------------------------------------------------------------

# Match ``NAME = "value"`` inside a class body.
_STATUS_VALUE_RE = re.compile(r'^\s+(\w+)\s*=\s*"(?P<value>[^"]+)"')

# Matches the start of the SubsessionStatus class.
_CLASS_HEADER_RE = re.compile(r"^class SubsessionStatus\(StrEnum\):")

# ---------------------------------------------------------------------------
# Step 2 — find SubsessionStatus-looking string literals in chat.js
# ---------------------------------------------------------------------------

# sub.status === "closed"  /  sub.status == "running"
_STATUS_COMPARISON_RE = re.compile(
    r'sub\.status\s*[=!]==?\s*"(?P<status>[a-z_][a-z_0-9]*)"'
)

# var status = sub.status || "running"  — fallback defaults
_STATUS_FALLBACK_RE = re.compile(r'sub\.status\s*\|\|\s*"(?P<status>[a-z_][a-z_0-9]*)"')


def main() -> int:
    """Check SubsessionStatus string consistency and return 0 (ok) or 1 (violations)."""
    repo_root = Path(__file__).resolve().parent.parent
    models_py = repo_root / "src" / "robotsix_chat" / "subsessions" / "models.py"
    chat_js = repo_root / "src" / "robotsix_chat" / "ui" / "static" / "chat.js"

    canonical = parse_str_enum(models_py, _CLASS_HEADER_RE, _STATUS_VALUE_RE)
    if not canonical:
        print(
            "ERROR: no SubsessionStatus members found in"
            f" {models_py.relative_to(repo_root)}",
            file=sys.stderr,
        )
        return 1

    canonical_values = set(canonical.values())
    js_values = set(
        iter_matching_strings(chat_js, _STATUS_COMPARISON_RE, _STATUS_FALLBACK_RE)
    )

    violations = run_consistency_checks(
        canonical_values=canonical_values,
        scanned_values=js_values,
        missing_label=None,
        unrecognised_label=(
            "Unrecognised SubsessionStatus strings in chat.js"
            " (sub.status comparisons / fallback defaults —"
            " no matching Python constant):"
        ),
    )

    if violations:
        print(
            "\nRun `python scripts/check_subsession_statuses.py` locally to reproduce.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

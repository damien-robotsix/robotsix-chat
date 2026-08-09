#!/usr/bin/env python3
"""CI gate: verify SubsessionKind strings in the browser UI match Python constants.

Extracts ``SubsessionKind`` enum values from
``src/robotsix_chat/subsessions/models.py``, then scans
``src/robotsix_chat/ui/index.html`` for JavaScript ``.kind`` comparisons
that use bare string literals.  Exits non-zero when:

1. A canonical SubsessionKind value is **missing** from the HTML
   (Python renamed → frontend silently broken).
2. A ``.kind === "..."`` / ``kind === "..."`` string literal in the HTML
   is **not** one of the canonical values (typo, stale reference,
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
# Step 1 — extract canonical SubsessionKind values from models.py
# ---------------------------------------------------------------------------

# Match ``NAME = "value"`` inside a class body.
_KIND_VALUE_RE = re.compile(r'^\s+(\w+)\s*=\s*"(?P<value>[^"]+)"$')

# Matches the start of the SubsessionKind class.
_CLASS_HEADER_RE = re.compile(r"^class SubsessionKind\(StrEnum\):")

# ---------------------------------------------------------------------------
# Step 2 — find SubsessionKind-looking string literals in HTML JavaScript
# ---------------------------------------------------------------------------

# sub.kind === "periodic"  /  kind === "periodic"
# NOTE: frame.kind comparisons are deliberately excluded — those are
# FrameKind values (tool_call, tool_result, thinking, ...) from
# chat/events.py, not SubsessionKind values.
_KIND_COMPARISON_RE = re.compile(
    r'(?:^|\s|\()(?:sub\.)?kind\s*[=!]==?\s*"(?P<kind>[a-z_][a-z_0-9]*)"'
)


def main() -> int:
    """Check SubsessionKind string consistency and return 0 (ok) or 1 (violations)."""
    repo_root = Path(__file__).resolve().parent.parent
    models_py = repo_root / "src" / "robotsix_chat" / "subsessions" / "models.py"
    index_html = repo_root / "src" / "robotsix_chat" / "ui" / "index.html"
    chat_js = repo_root / "src" / "robotsix_chat" / "ui" / "static" / "chat.js"

    canonical = parse_str_enum(models_py, _CLASS_HEADER_RE, _KIND_VALUE_RE)
    if not canonical:
        print(
            "ERROR: no SubsessionKind members found in"
            f" {models_py.relative_to(repo_root)}",
            file=sys.stderr,
        )
        return 1

    canonical_values = set(canonical.values())
    html_strings = iter_matching_strings(index_html, _KIND_COMPARISON_RE) + (
        iter_matching_strings(chat_js, _KIND_COMPARISON_RE)
    )
    html_values = set(html_strings)

    violations = run_consistency_checks(
        canonical_values=canonical_values,
        scanned_values=html_values,
        missing_label=(
            "SubsessionKind values missing from index.html (.kind / kind comparisons):"
        ),
        unrecognised_label=(
            "Unrecognised SubsessionKind strings in index.html"
            " (.kind / kind comparisons — no matching Python constant):"
        ),
        report_names_for_value=lambda val: ", ".join(
            k for k, v in canonical.items() if v == val
        ),
    )

    if violations:
        print(
            "\nRun `python scripts/check_subsession_kinds.py` locally to reproduce.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

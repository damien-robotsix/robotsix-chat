#!/usr/bin/env python3
"""CI gate: verify SSE event-type strings in the browser UI match Python constants.

Extracts ``SSE_*_TYPE = "..."`` assignments from
``src/robotsix_chat/chat/events.py``, then scans
``src/robotsix_chat/ui/index.html`` for JavaScript string literals that
look like SSE event types, and scans ``tests/**/*.py`` for bare string
literals matching canonical event-type values.  Exits non-zero when:

1. A canonical value from ``events.py`` is **missing** from the HTML
   (Python renamed → frontend silently broken).
2. A string literal in the HTML that looks like an SSE frame-type
   comparison / assignment (``frame.type === "…"``, ``type: "…"``) is
   **not** one of the canonical constants (typo, stale reference,
   orphaned string).
3. A bare string literal in a test file matches a canonical event-type
   value (e.g. ``"task_started"`` instead of ``SSE_TASK_STARTED_TYPE``).

This covers the role previously filled by the now-removed
``scripts/check_kind_literals.py`` for enum-constant hygiene.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from _check_common import (
    iter_matching_strings,
    parse_constant_sets,
    run_consistency_checks,
)

# ---------------------------------------------------------------------------
# Step 1 — extract canonical SSE_TYPE values from events.py
# ---------------------------------------------------------------------------

_SSE_CONST_RE = re.compile(r'^SSE_\w+_TYPE\s*=\s*"(?P<value>[^"]+)"$')

# ---------------------------------------------------------------------------
# Step 2 — find SSE-type string literals in HTML JavaScript
# ---------------------------------------------------------------------------

# frame.type === "task_started"  /  frame.type == "task_started"
_FRAME_COMPARISON_RE = re.compile(
    r'frame\.type\s*[=!]==?\s*"(?P<type>[a-z_][a-z_0-9]*)"'
)

# type: "pending_question_added"  (object-literal property)
_TYPE_PROPERTY_RE = re.compile(r'type\s*:\s*"(?P<type>[a-z_][a-z_0-9]*)"')


def main() -> int:
    """Check SSE event-type string consistency and return 0 (ok) or 1 (violations)."""
    repo_root = Path(__file__).resolve().parent.parent
    events_py = repo_root / "src" / "robotsix_chat" / "chat" / "events.py"
    index_html = repo_root / "src" / "robotsix_chat" / "ui" / "index.html"
    chat_js = repo_root / "src" / "robotsix_chat" / "ui" / "static" / "chat.js"

    # ------------------------------------------------------------------
    # Parse canonical constants
    # ------------------------------------------------------------------
    canonical = parse_constant_sets(events_py, _SSE_CONST_RE)

    if not canonical:
        print(
            "ERROR: no SSE_*_TYPE constants found in events.py",
            file=sys.stderr,
        )
        return 1

    canonical_values = set(canonical.values())

    # ------------------------------------------------------------------
    # Collect SSE-type strings from HTML + chat.js
    # ------------------------------------------------------------------
    html_strings = iter_matching_strings(
        index_html, _FRAME_COMPARISON_RE, _TYPE_PROPERTY_RE
    ) + iter_matching_strings(chat_js, _FRAME_COMPARISON_RE, _TYPE_PROPERTY_RE)
    # We only care about strings that match any canonical value; avoid
    # flagging unrelated strings like "error", "warn", etc.
    html_sse_strings = [s for s in html_strings if s in canonical_values]

    html_values = set(html_sse_strings)

    violations = False

    # ------------------------------------------------------------------
    # Check 1: canonical value missing from HTML
    # ------------------------------------------------------------------
    violations = run_consistency_checks(
        canonical_values=canonical_values,
        scanned_values=html_values,
        missing_label="SSE event-type constant values missing from index.html:",
        unrecognised_label=None,
        report_names_for_value=lambda val: ", ".join(
            k for k, v in canonical.items() if v == val
        ),
    )

    # ------------------------------------------------------------------
    # Check 2: HTML string not in canonical set
    # ------------------------------------------------------------------
    # Deduplicate while preserving uniqueness.
    unique_html: list[str] = []
    seen: set[str] = set()
    for s in html_strings:
        if s not in seen:
            seen.add(s)
            unique_html.append(s)

    # Only flag strings that look like SSE event-type names (contain an
    # underscore — all canonical values are snake_case with underscores).
    # This avoids false positives on the main SSE stream keys ("done",
    # "error", "token") which live in a different namespace.
    sse_like = [s for s in unique_html if "_" in s and s not in canonical_values]
    if sse_like:
        violations = True
        print(
            "Unrecognised SSE event-type strings in index.html"
            " (no matching Python constant):",
            file=sys.stderr,
        )
        for val in sorted(sse_like):
            print(f"  {val}", file=sys.stderr)
        print(file=sys.stderr)

    # ------------------------------------------------------------------
    # Step 3 — scan test files for bare event-type string literals
    # ------------------------------------------------------------------
    test_dir = repo_root / "tests"
    # Build a regex that matches any canonical value as a quoted string.
    _alt = "|".join(
        re.escape(v) for v in sorted(canonical_values, key=len, reverse=True)
    )
    _BARE_SSE_LITERAL_RE = re.compile(rf'"({_alt})"|\'({_alt})\'')
    # Strip Python comments (both full-line and inline) before scanning.
    _PY_COMMENT_RE = re.compile(r"#.*$", re.MULTILINE)

    for py_file in sorted(test_dir.rglob("*.py")):
        raw = py_file.read_text(encoding="utf-8")
        # Remove comment lines so we don't flag documentation references.
        code_only = _PY_COMMENT_RE.sub("", raw)
        for m in _BARE_SSE_LITERAL_RE.finditer(code_only):
            val = m.group(1) or m.group(2)
            violations = True
            print(
                f"{py_file.relative_to(repo_root)}: bare string "
                f'"{val}" — replace with the corresponding '
                f"SSE_*_TYPE constant from robotsix_chat.chat.events",
                file=sys.stderr,
            )

    if violations:
        print(
            "\nRun `python scripts/check_sse_event_types.py` locally to reproduce.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

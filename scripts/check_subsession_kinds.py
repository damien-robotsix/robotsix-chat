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

# ---------------------------------------------------------------------------
# Step 1 — extract canonical SubsessionKind values from models.py
# ---------------------------------------------------------------------------

# Match ``NAME = "value"`` inside a class body.
_KIND_VALUE_RE = re.compile(r'^\s+(\w+)\s*=\s*"(?P<value>[^"]+)"$')

# Matches the start of the SubsessionKind class.
_CLASS_HEADER_RE = re.compile(r"^class SubsessionKind\(StrEnum\):")


def _parse_subsession_kinds(models_path: Path) -> dict[str, str]:
    """Return {constant_name: value} for every SubsessionKind member."""
    lines = models_path.read_text(encoding="utf-8").splitlines()
    in_class = False
    canonical: dict[str, str] = {}
    for line in lines:
        if _CLASS_HEADER_RE.match(line.rstrip()):
            in_class = True
            continue
        if in_class:
            # Class body ended — dedented or next class.
            if line and not line[0].isspace():
                break
            m = _KIND_VALUE_RE.match(line.rstrip())
            if m:
                canonical[m.group(1)] = m.group("value")
    return canonical


# ---------------------------------------------------------------------------
# Step 2 — find SubsessionKind-looking string literals in HTML JavaScript
# ---------------------------------------------------------------------------

# sub.kind === "periodic"  /  frame.kind === "user_chat"  /  kind === "periodic"
_KIND_COMPARISON_RE = re.compile(
    r'(?:\.)?kind\s*[=!]==?\s*"(?P<kind>[a-z_][a-z_0-9]*)"'
)


def _iter_html_kind_strings(html_path: Path) -> list[str]:
    """Yield every kind-comparison string literal found in the HTML JS."""
    text = html_path.read_text(encoding="utf-8")
    # Strip HTML comments so they aren't scanned.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    found: list[str] = []
    for m in _KIND_COMPARISON_RE.finditer(text):
        found.append(m.group("kind"))
    return found


def main() -> int:
    """Check SubsessionKind string consistency and return 0 (ok) or 1 (violations)."""
    repo_root = Path(__file__).resolve().parent.parent
    models_py = repo_root / "src" / "robotsix_chat" / "subsessions" / "models.py"
    index_html = repo_root / "src" / "robotsix_chat" / "ui" / "index.html"

    # ------------------------------------------------------------------
    # Parse canonical constants
    # ------------------------------------------------------------------
    canonical = _parse_subsession_kinds(models_py)

    if not canonical:
        print(
            "ERROR: no SubsessionKind members found in"
            f" {models_py.relative_to(repo_root)}",
            file=sys.stderr,
        )
        return 1

    canonical_values: set[str] = set(canonical.values())

    # ------------------------------------------------------------------
    # Collect kind-comparison strings from HTML
    # ------------------------------------------------------------------
    html_strings = _iter_html_kind_strings(index_html)
    html_values: set[str] = set(html_strings)

    violations = False

    # ------------------------------------------------------------------
    # Check 1: canonical value missing from HTML
    # ------------------------------------------------------------------
    missing_from_html = canonical_values - html_values
    if missing_from_html:
        violations = True
        print(
            "SubsessionKind values missing from index.html (.kind / kind comparisons):",
            file=sys.stderr,
        )
        for val in sorted(missing_from_html):
            names = [k for k, v in canonical.items() if v == val]
            print(
                f"  {val}  (Python constant: {', '.join(names)})",
                file=sys.stderr,
            )
        print(file=sys.stderr)

    # ------------------------------------------------------------------
    # Check 2: HTML kind string not in canonical set
    # ------------------------------------------------------------------
    unrecognised = html_values - canonical_values
    if unrecognised:
        violations = True
        print(
            "Unrecognised SubsessionKind strings in index.html"
            " (.kind / kind comparisons — no matching Python constant):",
            file=sys.stderr,
        )
        for val in sorted(unrecognised):
            print(f"  {val}", file=sys.stderr)
        print(file=sys.stderr)

    if violations:
        print(
            "\nRun `python scripts/check_subsession_kinds.py` locally to reproduce.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

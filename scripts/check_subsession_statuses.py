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

# ---------------------------------------------------------------------------
# Step 1 — extract canonical SubsessionStatus values from models.py
# ---------------------------------------------------------------------------

# Match ``NAME = "value"`` inside a class body.
_STATUS_VALUE_RE = re.compile(r'^\s+(\w+)\s*=\s*"(?P<value>[^"]+)"')

# Matches the start of the SubsessionStatus class.
_CLASS_HEADER_RE = re.compile(r"^class SubsessionStatus\(StrEnum\):")


def _parse_subsession_statuses(models_path: Path) -> dict[str, str]:
    """Return {constant_name: value} for every SubsessionStatus member."""
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
            m = _STATUS_VALUE_RE.match(line.rstrip())
            if m:
                canonical[m.group(1)] = m.group("value")
    return canonical


# ---------------------------------------------------------------------------
# Step 2 — find SubsessionStatus-looking string literals in chat.js
# ---------------------------------------------------------------------------

# sub.status === "closed"  /  sub.status == "running"
_STATUS_COMPARISON_RE = re.compile(
    r'sub\.status\s*[=!]==?\s*"(?P<status>[a-z_][a-z_0-9]*)"'
)

# var status = sub.status || "running"  — fallback defaults
_STATUS_FALLBACK_RE = re.compile(r'sub\.status\s*\|\|\s*"(?P<status>[a-z_][a-z_0-9]*)"')


def _iter_js_status_strings(js_path: Path) -> list[str]:
    """Yield every sub.status comparison / fallback string literal in the JS."""
    text = js_path.read_text(encoding="utf-8")
    found: list[str] = []
    for m in _STATUS_COMPARISON_RE.finditer(text):
        found.append(m.group("status"))
    for m in _STATUS_FALLBACK_RE.finditer(text):
        found.append(m.group("status"))
    return found


def main() -> int:
    """Check SubsessionStatus string consistency and return 0 (ok) or 1 (violations)."""
    repo_root = Path(__file__).resolve().parent.parent
    models_py = repo_root / "src" / "robotsix_chat" / "subsessions" / "models.py"
    chat_js = repo_root / "src" / "robotsix_chat" / "ui" / "static" / "chat.js"

    # ------------------------------------------------------------------
    # Parse canonical constants
    # ------------------------------------------------------------------
    canonical = _parse_subsession_statuses(models_py)

    if not canonical:
        print(
            "ERROR: no SubsessionStatus members found in"
            f" {models_py.relative_to(repo_root)}",
            file=sys.stderr,
        )
        return 1

    canonical_values: set[str] = set(canonical.values())

    # ------------------------------------------------------------------
    # Collect status-comparison strings from chat.js
    # ------------------------------------------------------------------
    js_strings = _iter_js_status_strings(chat_js)
    js_values: set[str] = set(js_strings)

    violations = False

    # ------------------------------------------------------------------
    # Check: JS status string not in canonical set
    # ------------------------------------------------------------------
    unrecognised = js_values - canonical_values
    if unrecognised:
        violations = True
        print(
            "Unrecognised SubsessionStatus strings in chat.js"
            " (sub.status comparisons / fallback defaults —"
            " no matching Python constant):",
            file=sys.stderr,
        )
        for val in sorted(unrecognised):
            print(f"  {val}", file=sys.stderr)
        print(file=sys.stderr)

    if violations:
        print(
            "\nRun `python scripts/check_subsession_statuses.py` locally to reproduce.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

# ---------------------------------------------------------------------------
# Step 1 — extract canonical AutonomousState values from models.py
# ---------------------------------------------------------------------------

# Match ``NAME = "value"`` inside a class body.
_STATE_VALUE_RE = re.compile(r'^\s+(\w+)\s*=\s*"(?P<value>[^"]+)"$')

# Matches the start of the AutonomousState class.
_CLASS_HEADER_RE = re.compile(r"^class AutonomousState\((?:enum\.)?StrEnum\):")


def _parse_autonomous_states(models_path: Path) -> dict[str, str]:
    """Return {constant_name: value} for every AutonomousState member."""
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
            m = _STATE_VALUE_RE.match(line.rstrip())
            if m:
                canonical[m.group(1)] = m.group("value")
    return canonical


# ---------------------------------------------------------------------------
# Step 2 — find AutonomousState-looking string literals in chat.js
# ---------------------------------------------------------------------------

# aState === "selecting_subject"  /  autonomous_state === "awaiting_approval"
_STATE_COMPARISON_RE = re.compile(
    r'(?:aState|autonomous_state)\s*[=!]==\s*"(?P<state>[a-z_][a-z_0-9]*)"'
)


def _iter_js_state_strings(js_path: Path) -> list[str]:
    """Yield every autonomous-state string literal found in the JS."""
    text = js_path.read_text(encoding="utf-8")
    found: list[str] = []
    for m in _STATE_COMPARISON_RE.finditer(text):
        found.append(m.group("state"))
    return found


def main() -> int:
    """Check AutonomousState string consistency and return 0 (ok) or 1 (violations)."""
    repo_root = Path(__file__).resolve().parent.parent
    models_py = repo_root / "src" / "robotsix_chat" / "autonomous" / "models.py"
    chat_js = repo_root / "src" / "robotsix_chat" / "ui" / "static" / "chat.js"

    # ------------------------------------------------------------------
    # Parse canonical constants
    # ------------------------------------------------------------------
    canonical = _parse_autonomous_states(models_py)

    if not canonical:
        print(
            "ERROR: no AutonomousState members found in"
            f" {models_py.relative_to(repo_root)}",
            file=sys.stderr,
        )
        return 1

    canonical_values: set[str] = set(canonical.values())

    # ------------------------------------------------------------------
    # Collect state-comparison strings from chat.js
    # ------------------------------------------------------------------
    js_strings = _iter_js_state_strings(chat_js)
    js_values: set[str] = set(js_strings)

    violations = False

    # ------------------------------------------------------------------
    # Check 1: canonical value missing from chat.js
    # ------------------------------------------------------------------
    missing_from_js = canonical_values - js_values
    if missing_from_js:
        violations = True
        print(
            "AutonomousState values missing from chat.js"
            " (aState / autonomous_state comparisons):",
            file=sys.stderr,
        )
        for val in sorted(missing_from_js):
            names = [k for k, v in canonical.items() if v == val]
            print(
                f"  {val}  (Python constant: {', '.join(names)})",
                file=sys.stderr,
            )
        print(file=sys.stderr)

    # ------------------------------------------------------------------
    # Check 2: chat.js state string not in canonical set
    # ------------------------------------------------------------------
    unrecognised = js_values - canonical_values
    if unrecognised:
        violations = True
        print(
            "Unrecognised AutonomousState strings in chat.js"
            " (aState / autonomous_state comparisons — no matching Python constant):",
            file=sys.stderr,
        )
        for val in sorted(unrecognised):
            print(f"  {val}", file=sys.stderr)
        print(file=sys.stderr)

    if violations:
        print(
            "\nRun `python scripts/check_autonomous_states.py` locally to reproduce.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

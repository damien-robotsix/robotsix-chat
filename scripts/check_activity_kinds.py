#!/usr/bin/env python3
"""CI gate: verify activity-frame kind strings in the browser UI.

Extracts ``ACTIVITY_KINDS`` from ``src/robotsix_chat/chat/events.py``,
then scans ``src/robotsix_chat/ui/static/chat.js`` for ``frame.kind``
comparisons that use bare string literals.  Exits non-zero when:

- A ``frame.kind === "..."`` string literal in chat.js is **not**
  one of the canonical activity kinds (typo, stale reference,
  orphaned string), after excluding known SubsessionKind values
  (``frame.kind`` is also used in ``subsession_added`` frames for
  SubsessionKind comparisons).

Note: the reverse check (canonical kind missing from chat.js) is
deliberately skipped — some kinds (e.g. ``"text"``) may be handled
implicitly by an ``else`` fallthrough rather than an explicit
comparison.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Step 1 — extract canonical ACTIVITY_KINDS from events.py
# ---------------------------------------------------------------------------

_ACTIVITY_KINDS_START_RE = re.compile(
    r"^ACTIVITY_KINDS\s*:\s*frozenset\[str\]\s*=\s*frozenset\("
)

# Matches a single- or double-quoted string and captures its content.
_QUOTED_STRING_RE = re.compile(r"""["']([^"']+)["']""")

# ---------------------------------------------------------------------------
# Step 2 — extract SubsessionKind values to avoid false positives
# ---------------------------------------------------------------------------

_KIND_VALUE_RE = re.compile(r'^\s+(\w+)\s*=\s*"(?P<value>[^"]+)"$')
_CLASS_HEADER_RE = re.compile(r"^class SubsessionKind\(StrEnum\):")


def _parse_activity_kinds(events_path: Path) -> set[str]:
    """Return the set of canonical activity kind values from ACTIVITY_KINDS."""
    text = events_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _ACTIVITY_KINDS_START_RE.match(line.rstrip()):
            # Collect the frozenset body — may span multiple lines.
            # The opening line ends with "frozenset("; "{...}" follows.
            body = line.partition("frozenset(")[2]
            if "}" not in body:
                for j in range(i + 1, len(lines)):
                    body += lines[j]
                    if "}" in lines[j]:
                        break
            values: set[str] = set()
            for m in _QUOTED_STRING_RE.finditer(body):
                values.add(m.group(1))
            return values
    return set()


def _parse_subsession_kinds(models_path: Path) -> set[str]:
    """Return the set of canonical SubsessionKind string values."""
    lines = models_path.read_text(encoding="utf-8").splitlines()
    in_class = False
    values: set[str] = set()
    for line in lines:
        if _CLASS_HEADER_RE.match(line.rstrip()):
            in_class = True
            continue
        if in_class:
            if line and not line[0].isspace():
                break
            m = _KIND_VALUE_RE.match(line.rstrip())
            if m:
                values.add(m.group("value"))
    return values


# ---------------------------------------------------------------------------
# Step 3 — find frame.kind comparisons in chat.js
# ---------------------------------------------------------------------------

_FRAME_KIND_RE = re.compile(r'frame\.kind\s*[=!]==?\s*"(?P<kind>[a-z_][a-z_0-9]*)"')


def _iter_frame_kind_strings(js_path: Path) -> list[str]:
    """Yield every frame.kind comparison string literal found in the JS."""
    text = js_path.read_text(encoding="utf-8")
    found: list[str] = []
    for m in _FRAME_KIND_RE.finditer(text):
        found.append(m.group("kind"))
    return found


def main() -> int:
    """Check activity-kind string consistency and return 0 (ok) or 1 (violations)."""
    repo_root = Path(__file__).resolve().parent.parent
    events_py = repo_root / "src" / "robotsix_chat" / "chat" / "events.py"
    models_py = repo_root / "src" / "robotsix_chat" / "subsessions" / "models.py"
    chat_js = repo_root / "src" / "robotsix_chat" / "ui" / "static" / "chat.js"

    # ------------------------------------------------------------------
    # Parse canonical ACTIVITY_KINDS
    # ------------------------------------------------------------------
    canonical_values = _parse_activity_kinds(events_py)

    if not canonical_values:
        print(
            "ERROR: no ACTIVITY_KINDS frozenset found in"
            f" {events_py.relative_to(repo_root)}",
            file=sys.stderr,
        )
        return 1

    # ------------------------------------------------------------------
    # Parse SubsessionKind values (for false-positive exclusion)
    # ------------------------------------------------------------------
    subsession_values = _parse_subsession_kinds(models_py)

    # ------------------------------------------------------------------
    # Collect frame.kind comparison strings from chat.js
    # ------------------------------------------------------------------
    js_strings = _iter_frame_kind_strings(chat_js)
    js_values: set[str] = set(js_strings)

    # Exclude SubsessionKind values — those are checked by
    # check_subsession_kinds.py (which deliberately excludes
    # frame.kind, but frame.kind === "user_chat" is semantically
    # a SubsessionKind comparison, not an activity-kind comparison).
    activity_like = js_values - subsession_values

    violations = False

    # ------------------------------------------------------------------
    # Check: frame.kind string not in canonical activity set
    # ------------------------------------------------------------------
    unrecognised = activity_like - canonical_values
    if unrecognised:
        violations = True
        print(
            "Unrecognised frame.kind strings in chat.js"
            " (no matching activity kind in ACTIVITY_KINDS):",
            file=sys.stderr,
        )
        for val in sorted(unrecognised):
            print(f"  {val}", file=sys.stderr)
        print(file=sys.stderr)

    if violations:
        print(
            "\nRun `python scripts/check_activity_kinds.py` locally to reproduce.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

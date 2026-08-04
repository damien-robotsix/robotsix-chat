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

from _check_common import (
    iter_matching_strings,
    parse_str_enum,
    run_consistency_checks,
)

# ---------------------------------------------------------------------------
# Step 1 — extract canonical ACTIVITY_KINDS from events.py
# ---------------------------------------------------------------------------

_ACTIVITY_KINDS_START_RE = re.compile(
    r"^ACTIVITY_KINDS\s*:\s*frozenset\[str\]\s*=\s*frozenset\("
)

# Matches a single- or double-quoted string and captures its content.
_QUOTED_STRING_RE = re.compile(r"""["']([^"']+)["']""")


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


# ---------------------------------------------------------------------------
# Step 2 — extract SubsessionKind values to avoid false positives
# ---------------------------------------------------------------------------

_KIND_VALUE_RE = re.compile(r'^\s+(\w+)\s*=\s*"(?P<value>[^"]+)"$')
_CLASS_HEADER_RE = re.compile(r"^class SubsessionKind\(StrEnum\):")

# ---------------------------------------------------------------------------
# Step 3 — find frame.kind comparisons in chat.js
# ---------------------------------------------------------------------------

_FRAME_KIND_RE = re.compile(r'frame\.kind\s*[=!]==?\s*"(?P<kind>[a-z_][a-z_0-9]*)"')


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
    subsession_values = set(
        parse_str_enum(models_py, _CLASS_HEADER_RE, _KIND_VALUE_RE).values()
    )

    # ------------------------------------------------------------------
    # Collect frame.kind comparison strings from chat.js
    # ------------------------------------------------------------------
    js_values = set(iter_matching_strings(chat_js, _FRAME_KIND_RE))

    # Exclude SubsessionKind values — those are checked by
    # check_subsession_kinds.py (which deliberately excludes
    # frame.kind, but frame.kind === "user_chat" is semantically
    # a SubsessionKind comparison, not an activity-kind comparison).
    activity_like = js_values - subsession_values

    violations = run_consistency_checks(
        canonical_values=canonical_values,
        scanned_values=activity_like,
        missing_label=None,
        unrecognised_label=(
            "Unrecognised frame.kind strings in chat.js"
            " (no matching activity kind in ACTIVITY_KINDS):"
        ),
    )

    if violations:
        print(
            "\nRun `python scripts/check_activity_kinds.py` locally to reproduce.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

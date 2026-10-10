#!/usr/bin/env python3
"""CI gate: verify critical components have fallback entries in config.

When central-deploy roster is unavailable or incomplete, the chat relies on
fallback base URLs (component_fallbacks in config/config.json) to reach
components. This check ensures that all critical components have fallback
entries, preventing runtime failures when the roster is temporarily down.

Exit non-zero when:
  1. A critical component is missing from component_fallbacks
  2. A fallback URL is empty or invalid

See: docs/configuration.md for more details on component_fallbacks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# List of components that MUST have fallback entries.
# These are core services that, if unavailable via the roster, would cause
# significant functionality loss. Add new critical components here as they
# are integrated (e.g., robotsix-mail, robotsix-mill, robotsix-calendar).
CRITICAL_COMPONENTS = {
    "calendar": "Calendar service (scheduling, availability)",
    "mail": "Mail service (notifications, alerts)",
    "mill": "Mill service (ticket management, board state)",
}


def main() -> int:
    """Validate component_fallbacks completeness and return 0 (ok) or 1 (violations)."""
    repo_root = Path(__file__).resolve().parent.parent
    config_path = repo_root / "config" / "config.json"

    if not config_path.exists():
        print(
            f"ERROR: config file not found at {config_path.relative_to(repo_root)}",
            file=sys.stderr,
        )
        return 1

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError) as e:
        print(
            f"ERROR: failed to parse config: {e}",
            file=sys.stderr,
        )
        return 1

    fallbacks = config.get("central_deploy", {}).get("component_fallbacks", {})
    violations = False

    # Check that all critical components have fallback entries
    for component_id, description in CRITICAL_COMPONENTS.items():
        if component_id not in fallbacks:
            print(
                f"MISSING: component '{component_id}' ({description})"
                f" has no fallback entry in central_deploy.component_fallbacks",
                file=sys.stderr,
            )
            violations = True
        else:
            url = fallbacks[component_id]
            if not url or not isinstance(url, str):
                print(
                    f"INVALID: component '{component_id}' fallback URL is empty or invalid",
                    file=sys.stderr,
                )
                violations = True
            elif not (url.startswith("http://") or url.startswith("https://")):
                print(
                    f"INVALID: component '{component_id}' fallback URL must be http(s):"
                    f" got '{url}'",
                    file=sys.stderr,
                )
                violations = True

    if violations:
        print(
            "\nTo fix: add missing components to central_deploy.component_fallbacks"
            f" in {config_path.relative_to(repo_root)}\n"
            f"Example: '\"<component_id>\": \"http://<host>:<port>\"'\n"
            f"See docs/configuration.md for details.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Regenerate ``config/config.schema.json`` from the live pydantic Settings model.

With ``--check``, compares the generated schema against the committed file
and prints a unified diff on drift (exit code 0 when in sync, 1 on drift).
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

from robotsix_chat.config import Settings

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "config" / "config.schema.json"


def _generate_json() -> str:
    """Return the canonical JSON representation of the live Settings schema."""
    schema = Settings.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> None:
    """Regenerate or check config.schema.json against the live Settings model."""
    parser = argparse.ArgumentParser(
        description="Regenerate or check config.schema.json."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare generated schema against committed file; "
        "print unified diff and exit non-zero on drift.",
    )
    args = parser.parse_args()

    generated = _generate_json()

    if args.check:
        committed = _SCHEMA_PATH.read_text(encoding="utf-8")
        if generated == committed:
            print(f"{_SCHEMA_PATH} is in sync.")
            return

        diff = difflib.unified_diff(
            committed.splitlines(keepends=True),
            generated.splitlines(keepends=True),
            fromfile=str(_SCHEMA_PATH),
            tofile=f"{_SCHEMA_PATH} (generated)",
        )
        sys.stderr.writelines(diff)
        print(
            f"\nRegenerate:\n  uv run python {Path(__file__).name}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    _SCHEMA_PATH.write_text(generated, encoding="utf-8")
    print(f"Wrote {_SCHEMA_PATH}")


if __name__ == "__main__":
    main()

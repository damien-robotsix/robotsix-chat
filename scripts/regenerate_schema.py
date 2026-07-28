#!/usr/bin/env python3
"""Regenerate ``config/config.schema.json`` from the live pydantic Settings model."""

from __future__ import annotations

import json
from pathlib import Path

from robotsix_chat.config import Settings

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "config" / "config.schema.json"


def main() -> None:
    """Regenerate the committed schema file from the live Settings model."""
    schema = Settings.model_json_schema()
    _SCHEMA_PATH.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Wrote {_SCHEMA_PATH}")


if __name__ == "__main__":
    main()

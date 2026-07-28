#!/usr/bin/env python3
"""Regenerate ``config/config.schema.json`` from the live pydantic Settings model."""

from __future__ import annotations

import json
#!/usr/bin/env python3
"""Auto-correct the SHA256 hash in docs/system_prompt_changelog.md."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from robotsix_chat.config import Settings

_CHANGELOG_PATH = Path(__file__).resolve().parent.parent / "docs" / "system_prompt_changelog.md"

def main() -> None:
    text = _CHANGELOG_PATH.read_text()
    default = Settings.model_fields["agent_instruction"].default
    computed = hashlib.sha256(default.encode()).hexdigest()

    # Find the SHA256 line in the topmost (latest) version entry
    header_pat = re.compile(r"^## v(\d+) ", re.MULTILINE)
    header_match = header_pat.search(text)
    if not header_match:
        raise ValueError("No version entry header found in changelog")

    start = header_match.start()
    next_header = re.compile(r"^## v\d+ ", re.MULTILINE)
    next_match = next_header.search(text, start + 1)
    section = text[start : next_match.start()] if next_match else text[start:]

    sha_pat = re.compile(r"\*\*SHA256:\*\*\s*`([^`]+)`", re.IGNORECASE)
    sha_match = sha_pat.search(section)
    if not sha_match:
        raise ValueError(f"SHA256 not found in version v{header_match.group(1)} entry")

    current_hash = sha_match.group(1)
    if current_hash == computed:
        return  # already correct

    old = f"**SHA256:** `{current_hash}`"
    new = f"**SHA256:** `{computed}`"
    _CHANGELOG_PATH.write_text(text.replace(old, new, 1))
    print(f"Updated SHA256 from {current_hash[:12]}... to {computed[:12]}...")

if __name__ == "__main__":
    main()
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "config" / "config.schema.json"


def main() -> None:
    """Regenerate the committed schema file from the live Settings model."""
    schema = Settings.model_json_schema()
    _SCHEMA_PATH.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Wrote {_SCHEMA_PATH}")


if __name__ == "__main__":
    main()

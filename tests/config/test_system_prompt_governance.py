"""Governance tests for the system prompt (agent_instruction default literal).

Ensures every edit to ``Settings.agent_instruction`` is accompanied by a
corresponding changelog entry, version bump, and SHA256 update — no silent
drift.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from robotsix_chat.config import SYSTEM_PROMPT_VERSION, Settings


def _read_changelog() -> str:
    """Return the full text of ``docs/system_prompt_changelog.md``."""
    changelog_path = Path("docs") / "system_prompt_changelog.md"
    if not changelog_path.exists():
        raise FileNotFoundError(f"Changelog not found at {changelog_path.resolve()}")
    return changelog_path.read_text()


def _parse_latest_version_entry(text: str) -> tuple[int, str]:
    """Parse the *first* (top-most, most recent) version entry from *text*.

    Returns ``(version_number, recorded_sha256)``.  Expects entries of the
    form::

        ## v<N> — <date> — <ticket-id>

        ... body containing **SHA256:** ``<hex>`` ...

    Raises ``ValueError`` if no entry or malformed data is found.
    """
    # Match the first version header: "## v<N> — ..."
    header_pat = re.compile(r"^## v(\d+) ", re.MULTILINE)
    header_match = header_pat.search(text)
    if not header_match:
        raise ValueError("No version entry header found in changelog")
    version = int(header_match.group(1))

    # Find the SHA256 line that follows this header (before the next header
    # or end-of-file).  We search from the header onward.
    start = header_match.start()
    # Next header starts with "## v" (or end of string).
    next_header = re.compile(r"^## v\d+ ", re.MULTILINE)
    next_match = next_header.search(text, start + 1)
    section = text[start : next_match.start()] if next_match else text[start:]

    sha_pat = re.compile(r"\*\*SHA256:\*\*\s*`([0-9a-f]{64})`", re.IGNORECASE)
    sha_match = sha_pat.search(section)
    if not sha_match:
        raise ValueError(f"SHA256 not found in version v{version} entry section")
    return version, sha_match.group(1)


def test_version_stamp_matches_changelog_latest() -> None:
    """``SYSTEM_PROMPT_VERSION`` matches the latest entry in the changelog."""
    changelog = _read_changelog()
    latest_version, _ = _parse_latest_version_entry(changelog)
    assert latest_version == SYSTEM_PROMPT_VERSION, (
        f"SYSTEM_PROMPT_VERSION ({SYSTEM_PROMPT_VERSION}) != latest "
        f"changelog version ({latest_version}).  Bump the constant AND "
        f"add a new changelog entry together."
    )


def test_sha256_matches_live_default() -> None:
    """The recorded SHA256 matches the live ``agent_instruction`` default.

    Uses ``Settings.model_fields["agent_instruction"].default`` (the pydantic
    field default) — **not** a runtime ``Settings()`` instance — so the test
    is immune to ``AGENT_INSTRUCTION`` env-var overrides.
    """
    default = Settings.model_fields["agent_instruction"].default
    computed = hashlib.sha256(default.encode()).hexdigest()

    changelog = _read_changelog()
    _, recorded = _parse_latest_version_entry(changelog)

    assert recorded == computed, (
        f"Recorded SHA256 ({recorded}) != computed SHA256 ({computed}).  "
        f"The agent_instruction default has changed without a corresponding "
        f"changelog update.  Bump SYSTEM_PROMPT_VERSION, add a new entry to "
        f"docs/system_prompt_changelog.md, and record the new hash."
    )


def test_agent_instruction_starts_with_helpful_prefix() -> None:
    """Governance invariant: the default MUST start with the known prefix.

    (Other tests also assert this — this is the governance-level guard.)
    """
    default = Settings.model_fields["agent_instruction"].default
    assert default.startswith("You are a helpful assistant."), (
        "agent_instruction default must start with 'You are a helpful assistant.'"
    )


def _extract_docs_agent_instruction(docs_text: str) -> str:
    r"""Extract the ``agent.instruction`` value from the configuration table.

    The value lives in the third column of the table row, backtick-wrapped,
    with ``\\n`` representing embedded newlines.  Returns the unescaped string.
    """
    start_marker = 'AGENT_INSTRUCTION` | `"'
    start_idx = docs_text.find(start_marker)
    if start_idx < 0:
        raise ValueError(
            "Could not find agent.instruction row start marker in "
            "docs/configuration.md. Has the table format changed?"
        )
    after_start = docs_text[start_idx + len(start_marker):]

    end_marker = '"` | System prompt'
    end_idx = after_start.find(end_marker)
    if end_idx < 0:
        raise ValueError(
            "Could not find agent.instruction row end marker in "
            "docs/configuration.md. Has the table format changed?"
        )

    raw_value = after_start[:end_idx]
    # The table cell uses literal \\n to represent embedded newlines.
    return raw_value.replace("\\n", "\n")


def test_docs_configuration_md_mirrors_agent_instruction_default() -> None:
    """``docs/configuration.md`` ``agent.instruction`` row mirrors the live default.

    Governance item #4 (from docs/system_prompt_changelog.md) requires the
    docs table to stay verbatim in sync with the pydantic field default.
    This test enforces that automatically.
    """
    docs_path = Path("docs") / "configuration.md"
    if not docs_path.exists():
        raise FileNotFoundError(
            f"docs/configuration.md not found at {docs_path.resolve()}"
        )
    docs_text = docs_path.read_text()

    docs_default = _extract_docs_agent_instruction(docs_text)
    code_default = Settings.model_fields["agent_instruction"].default

    assert docs_default == code_default, (
        f"docs/configuration.md agent.instruction row does not match the "
        f"Settings.agent_instruction default. Update the docs table row to "
        f"reflect any changes to the default literal.\n\n"
        f"Docs length: {len(docs_default)}, Code length: {len(code_default)}"
    )

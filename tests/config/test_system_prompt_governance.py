"""Governance tests for the system prompt.

Ensures every edit to ``Settings.agent_instruction`` is accompanied by a
corresponding changelog entry, version bump, and SHA256 update — no silent
drift.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from pathlib import Path

import pytest

from robotsix_chat.config import SYSTEM_PROMPT_VERSION, Settings
from robotsix_chat.config.system_prompt_history import KNOWN_SYSTEM_PROMPT_SHA256S


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


def test_agent_instruction_carries_consolidation_precedence_clause() -> None:
    """The live default carries the reinforced consolidation gate clause.

    Mirrors
    ``test_react_prompt_templates_prioritize_consolidation_over_pending_threads``
    in ``tests/subsessions/test_delivery.py``: a direct assertion on the
    ``agent_instruction`` default (not just the indirect SHA256 governance
    check) that the consolidation rule overrides pending sub-conversation
    threads, forbids re-posing an already-presented decision, and requires
    a clear recommendation and next step.
    """
    default = Settings.model_fields["agent_instruction"].default
    assert "precedence over ANY pending sub-conversation" in default
    assert "do NOT re-pose an earlier question" in default
    assert "end with a clear recommendation and next step" in default
    assert "never re-list individual" in default
    assert "do not re-ask a decision the user has already made" in default


def _extract_docs_agent_instruction(docs_text: str) -> str | None:
    r"""Extract the ``agent_instruction`` value from the configuration table.

    Returns the unescaped string if the docs table inlines the full
    instruction text.  Returns ``None`` when the docs use a placeholder
    (e.g. ``(long default)``) — the caller should skip the parity check
    in that case.

    The value lives in the third column of the table row.  When present,
    it is backtick-wrapped with ``\\n`` representing embedded newlines.
    """
    start_marker = r"`agent_instruction`\s+\|\s+`[^`]+`\s+\|\s+"
    m = re.search(start_marker, docs_text)
    if m is None:
        raise ValueError(
            "Could not find agent_instruction row start marker in "
            "docs/configuration.md. Has the table format changed?"
        )
    after_start = docs_text[m.end() :]

    # If the default column does NOT contain a backtick-quoted string, the
    # docs are using a placeholder — signal the caller to skip comparison.
    if not after_start.startswith('"'):
        # The column starts with something that is NOT a double-quote — it
        # is a placeholder such as ``(long default)``.  Grab it for logging.
        placeholder_end = after_start.index("|")
        placeholder = after_start[:placeholder_end].strip()
        # If the placeholder is the expected sentinel, return None.
        if placeholder == "(long default)":
            return None
        raise ValueError(
            f"Unexpected default-column placeholder {placeholder!r} in "
            "docs/configuration.md agent_instruction row."
        )

    # Original path: the value is a backtick-wrapped double-quoted string.
    end_marker = r'"`\s+\|\s+System prompt'
    m_end = re.search(end_marker, after_start)
    if m_end is None:
        raise ValueError(
            "Could not find agent_instruction row end marker in "
            "docs/configuration.md. Has the table format changed?"
        )

    raw_value = after_start[: m_end.start()]
    # The table cell uses literal \\n to represent embedded newlines.
    return raw_value.replace("\\n", "\n")


def _extract_docs_chat_default_model_level(docs_text: str) -> int:
    """Extract the ``chat_default_model_level`` value from the configuration table."""
    # The table has four columns: key, type, default, description.
    # Match through the Type column to the opening backtick of the Default column.
    start_marker = r"`chat_default_model_level`\s+\|\s+`[^`]+`\s+\|\s+`"
    m = re.search(start_marker, docs_text)
    if m is None:
        raise ValueError(
            "Could not find chat_default_model_level row start marker in "
            "docs/configuration.md. Has the table format changed?"
        )
    after_start = docs_text[m.end() :]
    end_marker = r"`\s+\|\s+The chat agent's default"
    m_end = re.search(end_marker, after_start)
    if m_end is None:
        raise ValueError(
            "Could not find chat_default_model_level row end marker in "
            "docs/configuration.md. Has the table format changed?"
        )
    raw_value = after_start[: m_end.start()]
    return int(raw_value)


def test_docs_configuration_md_mirrors_chat_default_model_level_default() -> None:
    """``chat_default_model_level`` docs row mirrors the live default."""
    docs_path = Path("docs") / "configuration.md"
    if not docs_path.exists():
        raise FileNotFoundError(
            f"docs/configuration.md not found at {docs_path.resolve()}"
        )
    docs_text = docs_path.read_text()

    docs_default = _extract_docs_chat_default_model_level(docs_text)
    code_default = Settings.model_fields["chat_default_model_level"].default

    assert docs_default == code_default, (
        f"docs/configuration.md chat_default_model_level row Default column "
        f"({docs_default!r}) does not match the Settings.chat_default_model_level "
        f"default ({code_default!r}). Update the docs table row to reflect "
        f"the code default."
    )


def _extract_docs_agent_instruction_version(docs_text: str) -> int | None:
    """Extract the version number from the ``agent_instruction`` row description.

    Returns the integer version (e.g. 45) if the description contains
    ``(currently v<N>)``.  Returns ``None`` when the docs description does
    NOT contain a version number (e.g. because it was replaced with a
    symbolic reference like ``(see SYSTEM_PROMPT_VERSION…)``).
    """
    m = re.search(r"\(currently v(\d+)\)", docs_text)
    if m is None:
        return None
    return int(m.group(1))


def test_docs_configuration_md_version_matches_system_prompt_version() -> None:
    """Validate the docs agent_instruction row version against SYSTEM_PROMPT_VERSION.

    The description column includes a ``(currently v<N>)`` parenthetical.
    When that number drifts from ``SYSTEM_PROMPT_VERSION`` in
    ``settings.py``, this test fails, telling the author to update the
    docs row.
    """
    docs_path = Path("docs") / "configuration.md"
    if not docs_path.exists():
        raise FileNotFoundError(
            f"docs/configuration.md not found at {docs_path.resolve()}"
        )
    docs_text = docs_path.read_text()

    documented_version = _extract_docs_agent_instruction_version(docs_text)
    if documented_version is None:
        raise AssertionError(
            "docs/configuration.md agent_instruction row description no "
            "longer contains a '(currently v<N>)' version number.  If "
            "the version was intentionally replaced with a symbolic "
            "reference, remove this test.  Otherwise restore the "
            "'(currently v<N>)' parenthetical and keep it in sync with "
            "SYSTEM_PROMPT_VERSION."
        )

    assert documented_version == SYSTEM_PROMPT_VERSION, (
        f"docs/configuration.md agent_instruction row version "
        f"(currently v{documented_version}) does not match "
        f"SYSTEM_PROMPT_VERSION ({SYSTEM_PROMPT_VERSION}).  Update "
        f"the docs row to '(currently v{SYSTEM_PROMPT_VERSION})'."
    )


def test_docs_configuration_md_mirrors_agent_instruction_default() -> None:
    """``docs/configuration.md`` ``agent_instruction`` row uses ``(long default)``.

    Governance item #4 (from docs/system_prompt_changelog.md) states that the
    full multi-paragraph ``agent_instruction`` default is impractical to embed
    verbatim in a Markdown table cell — the ``(long default)`` placeholder is
    the accepted representation.  This test verifies the placeholder is present
    (or, if ever replaced with an inlined literal, that it matches the code
    default).
    """
    docs_path = Path("docs") / "configuration.md"
    if not docs_path.exists():
        raise FileNotFoundError(
            f"docs/configuration.md not found at {docs_path.resolve()}"
        )
    docs_text = docs_path.read_text()

    docs_default = _extract_docs_agent_instruction(docs_text)
    if docs_default is None:
        # Docs use a placeholder (e.g. "(long default)") — intentionally
        # not inlining the full instruction.  Skip the parity check.
        return

    code_default = Settings.model_fields["agent_instruction"].default

    assert docs_default == code_default, (
        f"docs/configuration.md agent_instruction row does not match the "
        f"Settings.agent_instruction default. Update the docs table row to "
        f"reflect any changes to the default literal.\n\n"
        f"Docs length: {len(docs_default)}, Code length: {len(code_default)}"
    )


def _parse_all_version_headers(text: str) -> list[str]:
    """Return every ``## v<N>`` version string found in *text*, in file order.

    Returns the raw version strings (e.g. ``"65"``, ``"65-b"``).
    """
    return [
        m.group(1) for m in re.finditer(r"^## v(\d+(?:-[b-g])?) ", text, re.MULTILINE)
    ]


def _all_changelog_sha256s(text: str) -> set[str]:
    """Return every ``**SHA256:**`` fingerprint recorded in the changelog."""
    return {
        m.group(1).lower()
        for m in re.finditer(r"\*\*SHA256:\*\*\s*`([0-9a-f]{64})`", text, re.IGNORECASE)
    }


def test_known_sha256_set_mirrors_changelog() -> None:
    """``KNOWN_SYSTEM_PROMPT_SHA256S`` is the exact set of changelog fingerprints.

    The changelog lives under ``docs/`` and is NOT shipped in the runtime
    container image, so the boot-time reconciliation reads the code-side
    mirror instead. This test fails whenever the two drift — e.g. a prompt
    bump added a changelog SHA256 but forgot to mirror it into
    ``system_prompt_history.py`` (or vice versa).
    """
    recorded = _all_changelog_sha256s(_read_changelog())
    assert recorded == KNOWN_SYSTEM_PROMPT_SHA256S, (
        "KNOWN_SYSTEM_PROMPT_SHA256S does not match the SHA256 records in "
        "docs/system_prompt_changelog.md. When bumping SYSTEM_PROMPT_VERSION, "
        "add the new hash to src/robotsix_chat/config/system_prompt_history.py "
        "as well.\n"
        f"Missing from code mirror: {sorted(recorded - KNOWN_SYSTEM_PROMPT_SHA256S)}\n"
        f"Extra in code mirror: {sorted(KNOWN_SYSTEM_PROMPT_SHA256S - recorded)}"
    )


def test_current_default_sha_in_known_set() -> None:
    """The live ``agent_instruction`` default's SHA256 is a recorded default."""
    default = Settings.model_fields["agent_instruction"].default
    sha = hashlib.sha256(default.encode()).hexdigest()
    assert sha in KNOWN_SYSTEM_PROMPT_SHA256S, (
        "The current agent_instruction default is not recorded in "
        "KNOWN_SYSTEM_PROMPT_SHA256S — bump SYSTEM_PROMPT_VERSION, add a "
        "changelog entry, and mirror the hash into system_prompt_history.py."
    )


def test_reconcile_upgrades_stale_former_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored value matching a FORMER default is upgraded to the code default.

    Simulates the 2026-09-05 incident: a config volume froze a prior default
    in place. At boot the reconciler must detect the stale pin (its SHA256 is
    a recorded former default) and drop it so the current code default applies.
    """
    stale = "You are a helpful assistant. STALE FORMER DEFAULT — v0."
    stale_sha = hashlib.sha256(stale.encode()).hexdigest()

    import robotsix_chat.config.settings as settings_mod

    # Pretend ``stale`` was once a governed default.
    monkeypatch.setattr(
        settings_mod,
        "KNOWN_SYSTEM_PROMPT_SHA256S",
        frozenset({stale_sha}),
    )

    settings = Settings.model_validate({"agent_instruction": stale})
    current_default = Settings.model_fields["agent_instruction"].default
    assert settings.agent_instruction == current_default


def test_reconcile_keeps_genuine_customization() -> None:
    """A value matching NO recorded default is kept as an operator customization."""
    custom = "You are a helpful assistant. Bespoke operator override, keep me."
    # Guard: the arbitrary text must not collide with a recorded default.
    custom_sha = hashlib.sha256(custom.encode()).hexdigest()
    assert custom_sha not in KNOWN_SYSTEM_PROMPT_SHA256S

    settings = Settings.model_validate({"agent_instruction": custom})
    assert settings.agent_instruction == custom


def test_reconcile_no_op_when_current_default_stored() -> None:
    """Storing the current default verbatim is preserved (identity no-op)."""
    default = Settings.model_fields["agent_instruction"].default
    settings = Settings.model_validate({"agent_instruction": default})
    assert settings.agent_instruction == default


def test_no_duplicate_version_numbers() -> None:
    """No version number may appear more than once in the changelog.

    The governance policy states: "never reuse a version number."
    """
    changelog = _read_changelog()
    versions = _parse_all_version_headers(changelog)
    seen: dict[str, list[int]] = {}
    for idx, v in enumerate(versions, start=1):
        seen.setdefault(v, []).append(idx)
    duplicates = {v: positions for v, positions in seen.items() if len(positions) > 1}
    assert not duplicates, (
        f"Duplicate version numbers found in changelog: "
        f"{ {v: f'occurrences at positions {pos}' for v, pos in duplicates.items()} }. "
        f"Each version number must be unique — bump the later entries to fresh numbers."
    )


def test_no_unexpected_gaps_in_version_sequence() -> None:
    """The version sequence must not have gaps (except documented skips).

    A gap is a missing integer between the highest and lowest version.
    Known, documented skips (e.g. v23) are excluded from the check.

    Suffixed versions (v65-b, etc.) are excluded from the integer-sequence
    check — they are collocated near their base version and not gaps.
    """
    # Known skips — version numbers that were intentionally not used.
    # Document each with a short rationale.
    known_skips: dict[int, str] = {
        23: "v23 was skipped — documented in changelog",
        70: "v70 superseded by v71 (merge of two v70-branch changes)",
    }

    changelog = _read_changelog()
    version_strings = _parse_all_version_headers(changelog)

    # Extract pure integer versions (no suffix) for gap detection.
    integer_versions: list[int] = []
    for vs in version_strings:
        with contextlib.suppress(ValueError):
            integer_versions.append(int(vs))

    integer_versions = sorted(set(integer_versions), reverse=True)

    if len(integer_versions) < 2:
        return  # nothing to check

    highest = integer_versions[0]
    lowest = integer_versions[-1]

    gaps: list[int] = []
    for expected in range(highest, lowest - 1, -1):
        if expected not in integer_versions and expected not in known_skips:
            gaps.append(expected)

    assert not gaps, (
        f"Gaps found in version sequence: {gaps}. "
        f"Expected every integer from {highest} down to {lowest}. "
        f"If a version was intentionally skipped, add it to KNOWN_SKIPS "
        f"in this test with a rationale."
    )

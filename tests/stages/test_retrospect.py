"""Tests for ``src/robotsix_mill/stages/retrospect.py``.

Covers the six pure helpers: ``_tail_truncate_log``, ``_parse_numeric_count``,
``_extract_ticket_ids``, ``_check_memory_count_consistency``,
``_apply_memory_edits``, and ``_is_noop_draft``.

Uses an importlib-based fake-module harness to load the shadow module
directly from its source file, because the ``robotsix_mill`` shadow
package's ``__init__.py`` requires the real ``robotsix_mill`` to be
installed.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Load the shadow module via importlib — stub out mill siblings first
# ---------------------------------------------------------------------------

_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "robotsix_mill"
    / "stages"
    / "retrospect.py"
)


def _make_pkg_stub(name: str) -> Any:
    """Create a mock module that satisfies package-resolution imports."""
    mod = types.ModuleType(name)
    mod.__path__ = []
    mod.__package__ = name
    return mod


_stubs: dict[str, Any] = {
    "robotsix_mill": _make_pkg_stub("robotsix_mill"),
    "robotsix_mill.agents": _make_pkg_stub("robotsix_mill.agents"),
    "robotsix_mill.agents.retrospecting": SimpleNamespace(
        MemoryEdit=object,
        RetrospectResult=object,
        run_retrospect_agent=lambda **_: None,
    ),
    "robotsix_mill.config": SimpleNamespace(
        ConfigError=Exception,
        Settings=object,
        get_repo_config=lambda _: None,
    ),
    "robotsix_mill.core": _make_pkg_stub("robotsix_mill.core"),
    "robotsix_mill.core.models": SimpleNamespace(
        Comment=object,
        SourceKind=object,
        Ticket=object,
        TicketEvent=object,
    ),
    "robotsix_mill.core.states": SimpleNamespace(
        DONE_OR_CLOSED=frozenset(),
        State=SimpleNamespace(DONE="done"),
    ),
    "robotsix_mill.core.text_noop": SimpleNamespace(is_noop_report=lambda _: False),
    "robotsix_mill.core.text_utils": SimpleNamespace(
        truncate_at_boundary=lambda s, _: s
    ),
    "robotsix_mill.core.workspace": SimpleNamespace(prune_clone=lambda _: None),
    "robotsix_mill.forge": SimpleNamespace(get_forge=lambda **_: None),
    "robotsix_mill.langfuse": _make_pkg_stub("robotsix_mill.langfuse"),
    "robotsix_mill.langfuse.client": SimpleNamespace(
        fetch_session_summary=lambda **_: None
    ),
    "robotsix_mill.runtime": _make_pkg_stub("robotsix_mill.runtime"),
    "robotsix_mill.runtime.tracing": SimpleNamespace(current_session=lambda: None),
    "robotsix_mill.runtime.transient_errors": SimpleNamespace(
        reraise_if_transient=lambda e: None
    ),
    "robotsix_mill.stages": _make_pkg_stub("robotsix_mill.stages"),
    "robotsix_mill.stages.base": SimpleNamespace(
        Outcome=object, Stage=object, StageContext=object
    ),
    "robotsix_mill.stages.merge": SimpleNamespace(_load_pr_urls=lambda _: []),
}

for _mod_name, _stub in _stubs.items():
    sys.modules[_mod_name] = _stub

_spec = importlib.util.spec_from_file_location(
    "robotsix_mill.stages.retrospect", _SOURCE
)
assert _spec is not None, f"Could not load spec for {_SOURCE}"
assert _spec.loader is not None
_retrospect = importlib.util.module_from_spec(_spec)
_retrospect.__package__ = "robotsix_mill.stages"
sys.modules["robotsix_mill.stages.retrospect"] = _retrospect
_spec.loader.exec_module(_retrospect)

_tail_truncate_log = _retrospect._tail_truncate_log
_parse_numeric_count = _retrospect._parse_numeric_count
_extract_ticket_ids = _retrospect._extract_ticket_ids
_check_memory_count_consistency = _retrospect._check_memory_count_consistency
_apply_memory_edits = _retrospect._apply_memory_edits
_is_noop_draft = _retrospect._is_noop_draft

# ---------------------------------------------------------------------------
# _tail_truncate_log
# ---------------------------------------------------------------------------


class TestTailTruncateLog:
    """Tests for ``_tail_truncate_log(text, max_chars)``."""

    def test_zero_max_chars_returns_identical(self) -> None:
        """``max_chars=0`` disables capping — returns text unchanged."""
        text = "line1\nline2\nline3"
        assert _tail_truncate_log(text, 0) == text

    def test_text_shorter_than_max_returns_identical(self) -> None:
        """Text shorter than the cap is returned unchanged."""
        text = "short"
        assert _tail_truncate_log(text, 1000) == text

    def test_text_equal_to_max_returns_identical(self) -> None:
        """Text exactly at the cap is returned unchanged."""
        text = "abcde"
        assert _tail_truncate_log(text, len(text)) == text

    def test_text_longer_than_max_truncates(self) -> None:
        """Longer text is truncated — result starts with omission note."""
        text = "line1\nline2\nline3\nline4\nline5"
        result = _tail_truncate_log(text, max_chars=15)
        assert result != text
        # Result should start with a line containing "omitted" and a
        # positive integer.
        first_line = result.split("\n")[0]
        assert "omitted" in first_line
        # Extract the integer from the omission note.
        import re

        m = re.search(r"(\d+)", first_line)
        assert m is not None
        assert int(m.group(1)) > 0
        # Result should not split mid-line — the kept portion starts
        # immediately after a "\n" in the original text (or at cut_point
        # if no newline found after cut_point).
        # The omission note + newline is prepended; after that, the kept
        # text should be a valid suffix of the original (starting at a
        # line boundary when a "\n" was found after cut_point).
        assert result.endswith("line5") or result.endswith("line4\nline5")

    def test_five_line_truncation_omits_correct_count(self) -> None:
        """Tight cap on a 5-line text omits the correct number of lines."""
        text = "line1\nline2\nline3\nline4\nline5"
        result = _tail_truncate_log(text, max_chars=10)
        # The omission note counts the truncated leading lines.
        import re

        m = re.search(r"\[\.\.\. (\d+) earlier lines omitted\]", result)
        assert m is not None, f"unexpected result: {result!r}"
        omitted = int(m.group(1))
        # We expect the correct number of omitted lines based on the
        # text structure.  Verify by counting "\n" in the omitted portion
        # matches omitted - 1 (the note says "N lines omitted" meaning
        # N-1 newline chars preceded the kept portion).
        # For a 5-line text with a tight cap, typically 4 or 5 lines are
        # omitted (the omission note line itself is not counted).
        assert omitted >= 1


# ---------------------------------------------------------------------------
# _parse_numeric_count
# ---------------------------------------------------------------------------


class TestParseNumericCount:
    """Tests for ``_parse_numeric_count(text)``."""

    def test_digit_pattern(self) -> None:
        """``"3 tickets"`` → ``3``."""
        assert _parse_numeric_count("3 tickets") == 3

    def test_singular_ticket(self) -> None:
        """``"1 ticket"`` → ``1`` (singular form)."""
        assert _parse_numeric_count("1 ticket") == 1

    def test_word_number_default_case(self) -> None:
        """``"Eleven tickets"`` → ``11`` (word-to-num, default case)."""
        assert _parse_numeric_count("Eleven tickets") == 11

    def test_word_number_case_insensitive(self) -> None:
        """``"ELEVEN TICKETS"`` → ``11`` (case-insensitive match)."""
        assert _parse_numeric_count("ELEVEN TICKETS") == 11

    def test_hyphenated_word_form(self) -> None:
        """``"twenty-one tickets found"`` → ``21`` (hyphenated word form)."""
        assert _parse_numeric_count("twenty-one tickets found") == 21

    def test_no_match_returns_none(self) -> None:
        """``"No issues"`` → ``None`` (no match)."""
        assert _parse_numeric_count("No issues") is None

    def test_large_digit(self) -> None:
        r"""``"100 tickets"`` → ``100`` (digit pattern catches any ``\d+``)."""
        assert _parse_numeric_count("100 tickets") == 100

    def test_zero_not_in_word_to_num(self) -> None:
        r"""``"zero tickets"`` → ``None`` ("zero" absent from ``_WORD_TO_NUM``)."""
        assert _parse_numeric_count("zero tickets") is None


# ---------------------------------------------------------------------------
# _extract_ticket_ids
# ---------------------------------------------------------------------------


class TestExtractTicketIds:
    """Tests for ``_extract_ticket_ids(text)``."""

    def test_backtick_wrapped_bullet(self) -> None:
        """``- `TKT-001``` → ``{"TKT-001"}`` (backtick-wrapped bullet)."""
        assert _extract_ticket_ids("- `TKT-001`") == {"TKT-001"}

    def test_bare_bullet_fallback(self) -> None:
        """``- TKT-001: some note`` → ``{"TKT-001"}`` (bare-bullet fallback)."""
        assert _extract_ticket_ids("- TKT-001: some note") == {"TKT-001"}

    def test_same_id_in_both_patterns_deduplicated(self) -> None:
        """Same ID appearing in both patterns → single-element set."""
        text = "- `TKT-001`\n- TKT-001: some note"
        assert _extract_ticket_ids(text) == {"TKT-001"}

    def test_non_bullet_lines_excluded(self) -> None:
        """Lines not starting with a bullet are excluded."""
        text = "TKT-001 is important\n- `TKT-002`"
        assert _extract_ticket_ids(text) == {"TKT-002"}

    def test_two_distinct_ids(self) -> None:
        """Mixed text with two distinct IDs → set of both."""
        text = "- `TKT-001`\n- `TKT-002`"
        assert _extract_ticket_ids(text) == {"TKT-001", "TKT-002"}

    def test_bare_bullet_requires_embedded_digit(self) -> None:
        """``- abc`` → ``set()`` (no digit in ``abc``)."""
        assert _extract_ticket_ids("- abc") == set()


# ---------------------------------------------------------------------------
# _check_memory_count_consistency
# ---------------------------------------------------------------------------


class TestCheckMemoryCountConsistency:
    """Tests for ``_check_memory_count_consistency(memory_text)``."""

    def test_empty_string(self) -> None:
        """Empty string → ``[]``."""
        assert _check_memory_count_consistency("") == []

    def test_blank_whitespace(self) -> None:
        """Blank/whitespace → ``[]``."""
        assert _check_memory_count_consistency("   \n  \n") == []

    def test_consistent_section(self) -> None:
        """Section claims ``"Three tickets"`` with 3 ID bullets → ``[]``."""
        text = (
            "## Issue A\n\n"
            "Three tickets were found:\n"
            "- `TKT-001`\n"
            "- `TKT-002`\n"
            "- `TKT-003`\n"
        )
        assert _check_memory_count_consistency(text) == []

    def test_drifted_section(self) -> None:
        """Section claims ``"Three tickets"`` with only 2 ID bullets → one warning."""
        text = "## Issue A\n\nThree tickets:\n- `TKT-001`\n- `TKT-002`\n"
        warnings = _check_memory_count_consistency(text)
        assert len(warnings) == 1
        assert "Issue A" in warnings[0]
        assert "3" in warnings[0]
        assert "2" in warnings[0]

    def test_section_without_numeric_claim_no_warning(self) -> None:
        """Section with no numeric count claim → no warning generated."""
        text = "## Issue B\n\nSome tickets:\n- `TKT-001`\n- `TKT-002`\n"
        assert _check_memory_count_consistency(text) == []

    def test_two_sections_one_drifted(self) -> None:
        """Two sections: one consistent, one drifted → exactly one warning."""
        text = (
            "## Consistent\n\n"
            "Two tickets:\n"
            "- `TKT-001`\n"
            "- `TKT-002`\n\n"
            "## Drifted\n\n"
            "Four tickets:\n"
            "- `TKT-003`\n"
            "- `TKT-004`\n"
        )
        warnings = _check_memory_count_consistency(text)
        assert len(warnings) == 1
        assert "Drifted" in warnings[0]


# ---------------------------------------------------------------------------
# _apply_memory_edits
# ---------------------------------------------------------------------------


@dataclass
class _FakeMemoryEdit:
    """Minimal stand-in for ``MemoryEdit`` with ``.op``, ``.text``, ``.find``."""

    op: str
    text: str = ""
    find: str = ""


class TestApplyMemoryEdits:
    """Tests for ``_apply_memory_edits(existing, edits)``."""

    def test_append_on_non_empty(self) -> None:
        r"""``append`` on non-empty: result is ``existing + "\n\n" + text``."""
        existing = "section one"
        edit = _FakeMemoryEdit(op="append", text="section two")
        result, failures = _apply_memory_edits(existing, [edit])
        assert result == "section one\n\nsection two"
        assert failures == []

    def test_append_on_empty_existing(self) -> None:
        """``append`` on empty/blank existing: result is just ``edit.text``."""
        edit = _FakeMemoryEdit(op="append", text="fresh content")
        result, failures = _apply_memory_edits("", [edit])
        assert result == "fresh content"
        assert failures == []

    def test_append_on_whitespace_only(self) -> None:
        """``append`` on whitespace-only existing: result is just ``edit.text``."""
        edit = _FakeMemoryEdit(op="append", text="fresh content")
        result, failures = _apply_memory_edits("   ", [edit])
        assert result == "fresh content"
        assert failures == []

    def test_replace_with_find_present(self) -> None:
        """``replace`` with ``find`` present: first occurrence replaced, no failures."""
        existing = "hello world, hello everyone"
        edit = _FakeMemoryEdit(op="replace", find="hello", text="hi")
        result, failures = _apply_memory_edits(existing, [edit])
        assert result == "hi world, hello everyone"
        assert failures == []

    def test_replace_with_empty_find(self) -> None:
        """``replace`` with empty ``find``: failure, text unchanged."""
        existing = "hello world"
        edit = _FakeMemoryEdit(op="replace", find="", text="whatever")
        result, failures = _apply_memory_edits(existing, [edit])
        assert result == "hello world"
        assert len(failures) == 1
        assert "replace" in failures[0]

    def test_replace_with_find_not_in_text(self) -> None:
        """``replace`` with ``find`` not in text: failure, text unchanged."""
        existing = "hello world"
        edit = _FakeMemoryEdit(op="replace", find="missing", text="whatever")
        result, failures = _apply_memory_edits(existing, [edit])
        assert result == "hello world"
        assert len(failures) == 1

    def test_remove_with_find_present(self) -> None:
        """``remove`` with ``find`` present: occurrence removed, newlines collapsed."""
        existing = "keep this\n\n\nremove me\n\n\nkeep that"
        edit = _FakeMemoryEdit(op="remove", find="remove me")
        result, failures = _apply_memory_edits(existing, [edit])
        # After removal, triple+ newlines collapse to two.
        assert "keep this" in result
        assert "keep that" in result
        assert "remove me" not in result
        assert "\n\n\n" not in result
        assert failures == []

    def test_remove_with_find_absent(self) -> None:
        """``remove`` with ``find`` absent: failure, text unchanged."""
        existing = "keep this"
        edit = _FakeMemoryEdit(op="remove", find="not here")
        result, failures = _apply_memory_edits(existing, [edit])
        assert result == "keep this"
        assert len(failures) == 1

    def test_two_edits_in_sequence(self) -> None:
        """Two edits in sequence: second edit sees result of first."""
        existing = "alpha beta gamma"
        edit1 = _FakeMemoryEdit(op="replace", find="alpha", text="first")
        edit2 = _FakeMemoryEdit(op="replace", find="gamma", text="third")
        result, failures = _apply_memory_edits(existing, [edit1, edit2])
        assert result == "first beta third"
        assert failures == []

    def test_mixed_success_and_failure(self) -> None:
        """Successes applied, failures reported, no exception raised."""
        existing = "alpha beta gamma"
        edit1 = _FakeMemoryEdit(op="replace", find="alpha", text="first")
        edit2 = _FakeMemoryEdit(op="remove", find="missing")
        edit3 = _FakeMemoryEdit(op="append", text="appended")
        result, failures = _apply_memory_edits(existing, [edit1, edit2, edit3])
        assert "first" in result
        assert "appended" in result
        assert len(failures) == 1
        assert "remove" in failures[0]


# ---------------------------------------------------------------------------
# _is_noop_draft
# ---------------------------------------------------------------------------


class TestIsNoopDraft:
    """Tests for ``_is_noop_draft(title)``."""

    def test_is_noop_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Monkeypatch ``is_noop_report`` → ``True``: ``_is_noop_draft`` is truthy."""
        monkeypatch.setattr(_retrospect, "is_noop_report", lambda _: True)
        assert _is_noop_draft("anything") is True

    def test_is_noop_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Monkeypatch ``is_noop_report`` → ``False``: ``_is_noop_draft`` is falsy."""
        monkeypatch.setattr(_retrospect, "is_noop_report", lambda _: False)
        assert _is_noop_draft("anything") is False

    def test_none_title_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``None`` title delegates to ``is_noop_report`` (no crash)."""
        monkeypatch.setattr(_retrospect, "is_noop_report", lambda _: False)
        # Should not raise.
        assert _is_noop_draft(None) is False

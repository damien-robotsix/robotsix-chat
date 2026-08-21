"""Governance tests for the canonical prompt-style directive.

Ensures ``docs/prompt-style.md`` exists, is well-formed, and is
actually referenced by prompt assembly — so the reply-style directive
cannot silently disappear from the system prompt.
"""

from __future__ import annotations

from pathlib import Path

from robotsix_chat.chat.server.app import (
    _STYLE_DIRECTIVE_HEADER,
    _inject_skills,
    _load_prompt_style,
)
from robotsix_chat.config import Settings

_STYLE_PATH = Path("docs/prompt-style.md")


def test_style_file_exists() -> None:
    """The canonical style file must exist in the repo."""
    assert _STYLE_PATH.exists(), (
        f"Style file {_STYLE_PATH} does not exist. "
        "Create docs/prompt-style.md with a '## Style directive' section."
    )


def test_style_file_has_directive_header() -> None:
    """The style file must contain the ``## Style directive`` header."""
    assert _STYLE_PATH.exists(), f"Style file {_STYLE_PATH} does not exist."
    content = _STYLE_PATH.read_text()
    assert _STYLE_DIRECTIVE_HEADER in content, (
        f"Style file {_STYLE_PATH} is missing the {_STYLE_DIRECTIVE_HEADER!r} header."
    )


def test_style_directive_is_non_empty() -> None:
    """The directive section must contain non-whitespace content."""
    directive = _load_prompt_style()
    assert directive, (
        f"Style directive in {_STYLE_PATH} is empty — add content "
        f"after the {_STYLE_DIRECTIVE_HEADER!r} header."
    )


def test_style_directive_documents_decision_options_format() -> None:
    """The directive must document the ```suggestions decision-options frame.

    The browser turns a ```suggestions fenced block into clickable reply
    buttons; this governance guard ensures the agent-side format stays
    documented so the agent actually emits it for multiple-choice decisions.
    """
    directive = _load_prompt_style()
    assert directive, "Style directive is empty."
    assert "```suggestions" in directive, (
        f"Style directive in {_STYLE_PATH} must document the "
        "```suggestions fenced block so agents emit structured "
        "decision options in sessions and subsessions."
    )


def test_style_directive_documents_ticket_reference_rule() -> None:
    """The directive must document the full-ID + short-name ticket reference rule.

    Guards the five clauses of the session ticket-reference convention:
    first-reference format, no bare truncations, session ticket map,
    stale short-form resolution, and the failure rationale.
    """
    directive = _load_prompt_style()
    assert directive, "Style directive is empty."
    assert "full ID" in directive and "short name" in directive, (
        f"Style directive in {_STYLE_PATH} must name the ticket reference "
        "rule (full ID + short name)."
    )
    assert "never refer to a ticket by a bare truncated suffix" in directive, (
        f"Style directive in {_STYLE_PATH} must forbid bare truncated ticket suffixes."
    )
    assert "re-surface it whenever more than one ticket is under discussion" in (
        directive
    ), (
        f"Style directive in {_STYLE_PATH} must require the session ticket "
        "map to be re-surfaced for multi-ticket discussions and status "
        "summaries."
    )
    assert "re-derive the full ID from the live source" in directive, (
        f"Style directive in {_STYLE_PATH} must require stale short forms "
        "to be resolved against the live source."
    )


def test_style_directive_has_output_style_section() -> None:
    """The directive must carry an explicit ``## Output style`` section."""
    directive = _load_prompt_style()
    assert directive, "Style directive is empty."
    assert "## Output style" in directive, (
        f"Style directive in {_STYLE_PATH} must contain an explicit "
        "'## Output style' section so the scannable-style rules are "
        "recognizable as a dedicated block in the composed prompt."
    )


def test_style_directive_requires_tldr_first() -> None:
    """The directive must instruct the agent to lead with a one-line TL;DR."""
    directive = _load_prompt_style()
    assert directive, "Style directive is empty."
    assert "one-line TL;DR" in directive, (
        f"Style directive in {_STYLE_PATH} must require a one-line "
        "TL;DR before any detail."
    )


def test_style_directive_requires_bulleted_structure() -> None:
    """The directive must instruct the agent to structure replies as bullets."""
    directive = _load_prompt_style()
    assert directive, "Style directive is empty."
    assert "structure the body as bullet points" in directive, (
        f"Style directive in {_STYLE_PATH} must require a bulleted "
        "body structure (one idea per bullet)."
    )


def test_style_directive_requires_compact_replies() -> None:
    """The directive must carry the compactness / length-cap guidance."""
    directive = _load_prompt_style()
    assert directive, "Style directive is empty."
    assert "prefer the shortest form that fully answers" in directive, (
        f"Style directive in {_STYLE_PATH} must instruct the agent to "
        "keep replies compact and avoid padding."
    )


def test_style_directive_requires_fenced_code_blocks() -> None:
    """The directive must require fenced code blocks for code/commands."""
    directive = _load_prompt_style()
    assert directive, "Style directive is empty."
    assert "use fenced code blocks for code and commands" in directive, (
        f"Style directive in {_STYLE_PATH} must require fenced code "
        "blocks and forbid inlining multi-line code in prose."
    )


def test_inject_skills_includes_style_directive() -> None:
    """``_inject_skills`` appends the style directive to the instruction.

    This is the key governance guard: if prompt assembly stops calling
    ``_load_prompt_style()``, this test fails because the style content
    will be absent from the assembled prompt.
    """
    directive = _load_prompt_style()
    assert directive, (
        f"Style directive is empty — cannot verify injection. "
        f"Ensure {_STYLE_PATH} has content after "
        f"{_STYLE_DIRECTIVE_HEADER!r}."
    )

    settings = Settings()
    result = _inject_skills(settings, "Test instruction.")
    assert directive in result, (
        f"Style directive from {_STYLE_PATH} is not present in the "
        f"assembled prompt.  _inject_skills must call "
        f"_load_prompt_style() and append its result."
    )


def test_inject_skills_includes_style_even_when_bare() -> None:
    """The style directive is appended even in ``bare`` mode.

    ``bare`` mode skips skills but the style directive is a formatting
    directive, not a skill — it should always be included.
    """
    directive = _load_prompt_style()
    assert directive, "Style directive is empty."

    settings = Settings()
    result = _inject_skills(settings, "Test instruction.", bare=True)
    assert directive in result, (
        "Style directive is missing in bare-mode prompt. "
        "_inject_skills must append the style even when bare=True."
    )


# ---------------------------------------------------------------------------
# low_risk_actions injection
# ---------------------------------------------------------------------------


def test_inject_skills_includes_low_risk_actions() -> None:
    """When ``low_risk_actions`` is non-empty, the pre-authorized block is injected."""
    settings = Settings(
        low_risk_actions=["prioritize tickets on the board", "close a subsession"]
    )
    result = _inject_skills(settings, "Test instruction.")

    assert "Pre-authorized low-risk actions:" in result, (
        "Pre-authorized low-risk actions header missing — "
        "_inject_skills must inject the block when low_risk_actions is non-empty."
    )
    assert "prioritize tickets on the board" in result, (
        "First low-risk action not found in assembled prompt."
    )
    assert "close a subsession" in result, (
        "Second low-risk action not found in assembled prompt."
    )


def test_inject_skills_no_low_risk_actions_when_empty() -> None:
    """When ``low_risk_actions`` is empty, the pre-authorized block is NOT injected."""
    settings = Settings()
    result = _inject_skills(settings, "Test instruction.")

    assert "Pre-authorized low-risk actions:" not in result, (
        "Pre-authorized low-risk actions block should not appear "
        "when low_risk_actions is empty."
    )

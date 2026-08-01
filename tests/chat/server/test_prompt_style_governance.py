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

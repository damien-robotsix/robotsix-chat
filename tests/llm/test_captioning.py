"""Tests for the image captioning client — :mod:`robotsix_chat.llm.captioning`.

Covers the branch-coverage security-request-path rule: captioning sends
user-supplied image bytes to the configured ``vision_model`` over the
network, so every branch (unconfigured, setup failure, call failure, success,
truncation) must be exercised.  The llmio provider layer is mocked — these
tests never make a real network call.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robotsix_chat.llm.capabilities import IMAGE_OMITTED_NOTE
from robotsix_chat.llm.captioning import (
    MAX_CAPTION_CHARS,
    caption_image,
    caption_or_omit_note,
    vision_identifier,
    vision_model_name,
)

PNG = b"\x89PNG\r\n\x1a\nfake-png"


# ---------------------------------------------------------------------------
# Identifier translation helpers
# ---------------------------------------------------------------------------


class TestVisionIdentifier:
    """Config ``openrouter/<org>/<model>`` → llmio combined ``openrouter-`` id."""

    def test_openrouter_slash_prefix_becomes_hyphen(self) -> None:
        """A config ``openrouter/`` prefix maps to llmio's hyphen form."""
        assert (
            vision_identifier("openrouter/openai/gpt-4o-mini")
            == "openrouter-openai/gpt-4o-mini"
        )

    def test_already_combined_id_passes_through(self) -> None:
        """An already-hyphenated llmio id is returned unchanged."""
        assert (
            vision_identifier("openrouter-openai/gpt-4o-mini")
            == "openrouter-openai/gpt-4o-mini"
        )


class TestVisionModelName:
    """Bare model path extraction for the pydantic-ai ``model=`` arg."""

    def test_strips_openrouter_slash_prefix(self) -> None:
        """The ``openrouter/`` config prefix is stripped from the model path."""
        assert (
            vision_model_name("openrouter/openai/gpt-4o-mini") == "openai/gpt-4o-mini"
        )

    def test_strips_openrouter_hyphen_prefix(self) -> None:
        """The ``openrouter-`` combined prefix is stripped from the model path."""
        assert (
            vision_model_name("openrouter-openai/gpt-4o-mini") == "openai/gpt-4o-mini"
        )

    def test_bare_path_unchanged(self) -> None:
        """A path with no provider prefix is returned unchanged."""
        assert vision_model_name("openai/gpt-4o-mini") == "openai/gpt-4o-mini"


# ---------------------------------------------------------------------------
# caption_image
# ---------------------------------------------------------------------------


def _mock_vision_call(answer: str, *, raise_error: bool = False) -> MagicMock:
    """Install a fake llmio provider returning *answer* (or raising).

    Returns the fake provider so a test can assert on ``build_agent`` usage.
    """
    if raise_error:
        run = AsyncMock(side_effect=RuntimeError("provider boom"))
    else:
        result = MagicMock()
        result.output = answer
        run = AsyncMock(return_value=result)

    fake_handle = MagicMock()
    fake_handle.run = run
    fake_handle.close = MagicMock()

    fake_provider = MagicMock()
    fake_provider.build_agent = MagicMock(return_value=fake_handle)
    fake_provider._is_transient = MagicMock(return_value=False)

    patch_factory = patch(
        "robotsix_llmio.core.factory.get_provider_for_identifier",
        return_value=fake_provider,
    )
    return patch_factory, fake_provider, fake_handle


@pytest.mark.asyncio
async def test_caption_image_unconfigured_returns_none() -> None:
    """Empty vision_model means unconfigured — no call, None returned."""
    assert await caption_image(PNG, "image/png", vision_model="") is None


@pytest.mark.asyncio
async def test_caption_image_success() -> None:
    """A successful vision call returns the trimmed caption."""
    patch_factory, fake_provider, fake_handle = _mock_vision_call(
        "A red document with a form.\n"
    )
    with patch_factory:
        caption = await caption_image(
            PNG,
            "image/png",
            vision_model="openrouter/openai/gpt-4o-mini",
            vision_api_key="key-123",
        )

    assert caption == "A red document with a form."
    fake_provider.build_agent.assert_called_once()
    fake_handle.close.assert_called_once()


@pytest.mark.asyncio
async def test_caption_image_setup_failure_returns_none() -> None:
    """Provider resolution/build failure is logged and yields None."""
    with patch(
        "robotsix_llmio.core.factory.get_provider_for_identifier",
        side_effect=ValueError("unknown provider"),
    ):
        caption = await caption_image(
            PNG,
            "image/png",
            vision_model="openrouter/openai/gpt-4o-mini",
        )
    assert caption is None


@pytest.mark.asyncio
async def test_caption_image_call_failure_returns_none() -> None:
    """A vision call that raises is logged and yields None (never propagates)."""
    patch_factory, _fake_provider, _fake_handle = _mock_vision_call(
        "",
        raise_error=True,
    )
    with patch_factory:
        caption = await caption_image(
            PNG,
            "image/png",
            vision_model="openrouter/openai/gpt-4o-mini",
        )
    assert caption is None


@pytest.mark.asyncio
async def test_caption_image_empty_answer_returns_none() -> None:
    """A blank caption is treated as a failure → None."""
    patch_factory, _fp, _fh = _mock_vision_call("   ")
    with patch_factory:
        caption = await caption_image(
            PNG,
            "image/png",
            vision_model="openrouter/openai/gpt-4o-mini",
        )
    assert caption is None


@pytest.mark.asyncio
async def test_caption_image_truncates_overlong_caption() -> None:
    """Captions longer than the cap are truncated with an ellipsis."""
    long_answer = "x" * (MAX_CAPTION_CHARS + 50)
    patch_factory, _fp, _fh = _mock_vision_call(long_answer)
    with patch_factory:
        caption = await caption_image(
            PNG,
            "image/png",
            vision_model="openrouter/openai/gpt-4o-mini",
        )
    assert caption is not None
    assert len(caption) == MAX_CAPTION_CHARS + 1  # ellipsis char
    assert caption.endswith("…")
    assert caption[:-1] == "x" * MAX_CAPTION_CHARS


@pytest.mark.asyncio
async def test_caption_image_honours_custom_cap() -> None:
    """max_caption_chars is respected when supplied."""
    patch_factory, _fp, _fh = _mock_vision_call("y" * 30)
    with patch_factory:
        caption = await caption_image(
            PNG,
            "image/png",
            vision_model="openrouter/openai/gpt-4o-mini",
            max_caption_chars=10,
        )
    assert caption is not None
    assert caption == "y" * 10 + "…"


# ---------------------------------------------------------------------------
# caption_or_omit_note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_omit_note_fallback_when_unconfigured() -> None:
    """No vision model → the curated IMAGE_OMITTED_NOTE path is unchanged."""
    result = await caption_or_omit_note(
        "metadata",
        PNG,
        "image/png",
        vision_model="",
    )
    assert result == f"metadata\n{IMAGE_OMITTED_NOTE}"


@pytest.mark.asyncio
async def test_caption_replaces_omit_note_when_configured() -> None:
    """A configured vision model produces a caption in place of the note."""
    patch_factory, _fp, _fh = _mock_vision_call("A filled form.")
    with patch_factory:
        result = await caption_or_omit_note(
            "metadata",
            PNG,
            "image/png",
            vision_model="openrouter/openai/gpt-4o-mini",
            vision_api_key="key-123",
        )
    assert result == "metadata\n[Image caption: A filled form.]"
    assert IMAGE_OMITTED_NOTE not in result


@pytest.mark.asyncio
async def test_omit_note_fallback_when_caption_fails() -> None:
    """A configured model that fails still falls back to the curated note."""
    patch_factory, _fp, _fh = _mock_vision_call("", raise_error=True)
    with patch_factory:
        result = await caption_or_omit_note(
            "metadata",
            PNG,
            "image/png",
            vision_model="openrouter/openai/gpt-4o-mini",
        )
    assert result == f"metadata\n{IMAGE_OMITTED_NOTE}"

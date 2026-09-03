"""Tests for the image-captioning helper :func:`generate_caption`."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robotsix_chat.config import Settings
from robotsix_chat.llm.vision import (
    _MAX_CAPTION_CHARS,
    _to_llmio_identifier,
    generate_caption,
)


def _settings(vision_model: str = "openrouter/openai/gpt-4o-mini") -> Settings:
    """Build a Settings instance with the given vision_model and an API key."""
    return Settings(vision_model=vision_model, llmio_api_key="or-key")  # type: ignore[arg-type]


def _patched_provider(output: str = "a red square") -> tuple[MagicMock, MagicMock]:
    """Return a patched ``get_provider_for_identifier`` and its handle."""
    handle = MagicMock()

    async def fake_run(message: object, **run_kwargs: object) -> MagicMock:
        handle.run_calls.append(message)
        result = MagicMock()
        result.output = output
        return result

    handle.run_calls = []
    handle.run = fake_run
    handle.close = MagicMock()

    provider = MagicMock()
    provider.build_agent.return_value = handle

    factory = MagicMock(return_value=provider)
    return factory, handle


# --------------------------------------------------------------------------- #
#  identifier conversion                                                       #
# --------------------------------------------------------------------------- #


def test_to_llmio_identifier_converts_first_slash() -> None:
    assert (
        _to_llmio_identifier("openrouter/openai/gpt-4o-mini")
        == "openrouter-openai/gpt-4o-mini"
    )


def test_to_llmio_identifier_passthrough_without_slash() -> None:
    # A value with no slash is already in identifier form; returned unchanged.
    assert _to_llmio_identifier("claudeSDK-opus") == "claudeSDK-opus"


# --------------------------------------------------------------------------- #
#  successful caption generation                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_generate_caption_returns_model_output() -> None:
    factory, handle = _patched_provider("A red square on white.")
    with patch("robotsix_chat.llm.vision.get_provider_for_identifier", factory):
        caption = await generate_caption(
            "image/png", b"\x89PNG...", settings=_settings()
        )

    assert caption == "A red square on white."
    handle.close.assert_called_once()  # handle is always closed


@pytest.mark.asyncio
async def test_generate_caption_selects_configured_vision_model() -> None:
    """The configured vision_model drives the provider + model selection."""
    factory, handle = _patched_provider()
    with patch("robotsix_chat.llm.vision.get_provider_for_identifier", factory):
        await generate_caption(
            "image/jpeg",
            b"data",
            settings=_settings("openrouter/anthropic/claude-3-haiku"),
        )

    # Provider resolved from the converted identifier, with the API key.
    identifier = factory.call_args.args[0]
    assert identifier == "openrouter-anthropic/claude-3-haiku"
    assert factory.call_args.kwargs["api_key"] == "or-key"

    # build_agent gets the bare model name (everything after the first hyphen).
    build_kwargs = handle_build_kwargs(factory)
    assert build_kwargs["model"] == "anthropic/claude-3-haiku"


@pytest.mark.asyncio
async def test_generate_caption_sends_image_binary() -> None:
    """The image bytes travel as a pydantic-ai BinaryContent part."""
    from pydantic_ai.messages import BinaryContent

    factory, handle = _patched_provider()
    with patch("robotsix_chat.llm.vision.get_provider_for_identifier", factory):
        await generate_caption("image/gif", b"GIF89a", settings=_settings())

    message = handle.run_calls[0]
    binary = next(p for p in message if isinstance(p, BinaryContent))
    assert binary.data == b"GIF89a"
    assert binary.media_type == "image/gif"


@pytest.mark.asyncio
async def test_generate_caption_truncates_long_output() -> None:
    factory, _ = _patched_provider("x" * (_MAX_CAPTION_CHARS + 500))
    with patch("robotsix_chat.llm.vision.get_provider_for_identifier", factory):
        caption = await generate_caption("image/png", b"data", settings=_settings())

    assert caption.endswith("…[truncated]")
    assert len(caption) <= _MAX_CAPTION_CHARS + len(" …[truncated]")


# --------------------------------------------------------------------------- #
#  error handling — never raises into the caller                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unconfigured_vision_model_returns_empty() -> None:
    factory, _ = _patched_provider()
    with patch("robotsix_chat.llm.vision.get_provider_for_identifier", factory):
        caption = await generate_caption(
            "image/png", b"data", settings=_settings(vision_model="")
        )

    assert caption == ""
    factory.assert_not_called()  # never even builds a provider


@pytest.mark.asyncio
async def test_model_unreachable_returns_empty() -> None:
    """A provider/model error is swallowed and logged, returning ''."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no endpoints found")

    with patch("robotsix_chat.llm.vision.get_provider_for_identifier", boom):
        caption = await generate_caption("image/png", b"data", settings=_settings())

    assert caption == ""


@pytest.mark.asyncio
async def test_api_failure_during_run_returns_empty() -> None:
    """An exception raised while running the model does not propagate."""
    handle = MagicMock()

    async def boom_run(message: object, **kwargs: object) -> Any:
        raise RuntimeError("upstream 500")

    handle.run = boom_run
    handle.close = MagicMock()
    provider = MagicMock()
    provider.build_agent.return_value = handle
    factory = MagicMock(return_value=provider)

    with patch("robotsix_chat.llm.vision.get_provider_for_identifier", factory):
        caption = await generate_caption("image/png", b"bad", settings=_settings())

    assert caption == ""
    handle.close.assert_called_once()  # handle still closed on failure


@pytest.mark.asyncio
async def test_settings_loaded_when_not_supplied() -> None:
    """When no settings are passed, generate_caption loads them at runtime."""
    factory, handle = _patched_provider("loaded caption")
    with (
        patch("robotsix_chat.llm.vision.get_provider_for_identifier", factory),
        patch.object(Settings, "load", return_value=_settings()),
    ):
        caption = await generate_caption("image/png", b"data")

    assert caption == "loaded caption"


def handle_build_kwargs(factory: MagicMock) -> dict[str, Any]:
    """Return the kwargs passed to the mocked provider's ``build_agent``."""
    provider = factory.return_value
    kwargs: dict[str, Any] = provider.build_agent.call_args.kwargs
    return kwargs

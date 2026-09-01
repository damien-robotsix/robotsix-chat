"""Tests for the per-attempt model-capability contextvar."""

from __future__ import annotations

from robotsix_chat.llm.capabilities import (
    model_supports_images,
    reset_model_supports_images,
    set_model_supports_images,
)


def test_defaults_to_supported() -> None:
    assert model_supports_images() is True


def test_set_and_reset_round_trip() -> None:
    token = set_model_supports_images(False)
    assert model_supports_images() is False
    reset_model_supports_images(token)
    assert model_supports_images() is True

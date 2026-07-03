"""Property-based tests for Pydantic config model roundtrip."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from robotsix_chat.config import AuthSettings, Settings


@given(st.builds(AuthSettings))
def test_auth_settings_roundtrip(settings: AuthSettings) -> None:
    """model_dump() -> model_validate() is an identity."""
    dumped = settings.model_dump()
    restored = AuthSettings.model_validate(dumped)
    assert restored.model_dump() == dumped


@given(st.builds(Settings))
def test_settings_roundtrip(settings: Settings) -> None:
    """Full Settings roundtrip via dict serialization."""
    dumped = settings.model_dump()
    restored = Settings.model_validate(dumped)
    assert restored.model_dump() == dumped

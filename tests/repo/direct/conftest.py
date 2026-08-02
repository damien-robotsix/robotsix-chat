"""Shared fixtures for tests/repo/direct/ test modules."""

from __future__ import annotations

from typing import Any

import pytest

from robotsix_chat.config import DirectRepoSettings
from robotsix_chat.repo.direct.client import (
    _INSTALLATION_TOKEN_CACHE,
)


def _prepopulate_installation_token(settings: DirectRepoSettings) -> None:
    """Seed the installation token cache so tests bypass the token exchange."""
    _INSTALLATION_TOKEN_CACHE[settings.github_app_installation_id] = (
        "ghs_prepopulated_token"
    )


def _settings(**kw: Any) -> DirectRepoSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "github_app_id": "12345",
        "github_app_private_key": "fake-key",  # pragma: allowlist secret
        "github_app_installation_id": "67890",
        "board_api_base_url": "http://127.0.0.1:8077",
    }
    base.update(kw)
    return DirectRepoSettings(**base)


@pytest.fixture(autouse=True)
def _mock_github_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock mint_installation_token so the shared library is never imported."""
    import sys
    from types import SimpleNamespace

    def _fake_mint(**kw: object) -> object:
        return SimpleNamespace(token="ghs_test_installation_token")

    fake = SimpleNamespace()
    fake.mint_installation_token = _fake_mint
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)

"""Shared fixtures for tests/repo/direct/ test modules."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from robotsix_chat.config import DirectRepoSettings
from robotsix_chat.repo.direct.client import _INSTALLATION_TOKEN_CACHE


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

    def _fake_mint(**kw: object) -> object:
        return SimpleNamespace(token="ghs_test_installation_token")

    def _fake_build_app_jwt(app_id: str, private_key: str) -> str:
        return "fake-app-jwt"

    def _fake_resolve_installation_id(
        client: object, jwt_token: str, owner: str, repo: str
    ) -> str:
        return "67890"

    fake = SimpleNamespace()
    fake.mint_installation_token = _fake_mint
    fake._auth = SimpleNamespace(
        _build_app_jwt=_fake_build_app_jwt,
        _resolve_installation_id=_fake_resolve_installation_id,
    )
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)
    monkeypatch.setitem(sys.modules, "robotsix_github_auth._auth", fake._auth)

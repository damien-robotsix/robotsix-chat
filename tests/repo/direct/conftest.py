"""Shared fixtures for tests/repo/direct/ test modules."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings


def _prepopulate_installation_token(settings: DirectRepoSettings) -> None:
    """Compatibility no-op.

    The client owns no token cache — robotsix-github-auth's public
    ``mint_installation_token`` handles minting and caching, and the autouse
    ``_mock_github_auth`` fixture fakes it to return a constant token.  So
    tests no longer need to seed a local cache; this helper is retained so
    the call sites across the test modules keep compiling unchanged.
    """


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
    """Mock robotsix-github-auth so the shared library is never imported."""
    import sys

    def _fake_mint(**kw: object) -> object:
        return SimpleNamespace(token="ghs_test_installation_token")

    fake = SimpleNamespace()
    fake.mint_installation_token = _fake_mint
    # The client delegates 401-refresh invalidation to the library's public
    # token-cache API.
    fake.clear_token_cache = lambda: None
    fake.invalidate_token_cache = lambda installation_id: None
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)


def _mock_installation_repos(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "repositories": [
                        {"full_name": "damien-robotsix/repo-a"},
                        {"full_name": "damien-robotsix/robotsix-central-deploy"},
                    ]
                }
            ),
        )
    )


async def _mock_component_request_blocked(
    _component_id: str,
    _method: str,
    _path: str,
    **_kw: Any,
) -> str:
    """Mock ``component_request`` that returns BLOCKED state with ≥3 cycles."""
    return "HTTP 200\n" + json.dumps(
        {
            "state": "blocked",
            "events": [
                {"type": "implement_start"},
                {"type": "implement_complete"},
                {"type": "implement_start"},
                {"type": "implement_complete"},
                {"type": "implement_start"},
                {"type": "implement_complete"},
            ],
        }
    )


async def _mock_component_request_error(
    _component_id: str,
    _method: str,
    _path: str,
    **_kw: Any,
) -> str:
    """Mock ``component_request`` that returns a transient 'Error:' response."""
    return "Error: connection refused"

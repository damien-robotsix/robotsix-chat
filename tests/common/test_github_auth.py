"""Tests for the GitHub App authentication header helper.

Covers all four branches in :func:`_build_github_app_auth_headers`:

- Returns ``None`` when credentials are missing
- Returns a cached token when the installation ID matches
- Mints a new token and populates the cache
- Returns ``None`` on ``RuntimeError`` (logs a warning)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from robotsix_chat.common.github_auth import _build_github_app_auth_headers
from robotsix_chat.config.models import DirectRepoSettings

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(
    *,
    github_app_id: str = "",
    github_app_private_key: str = "",
    github_app_installation_id: str = "",
) -> DirectRepoSettings:
    """Build a ``DirectRepoSettings`` with the given GitHub App fields."""
    return DirectRepoSettings(
        github_app_id=github_app_id,
        github_app_private_key=SecretStr(github_app_private_key),
        github_app_installation_id=github_app_installation_id,
    )


# ---------------------------------------------------------------------------
# missing credentials → None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_none_when_app_id_empty() -> None:
    """Returns ``None`` when ``github_app_id`` is empty."""
    result = await _build_github_app_auth_headers(
        _settings(
            github_app_id="",
            github_app_private_key="k",
            github_app_installation_id="1",
        ),
        "test",
    )
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_private_key_empty() -> None:
    """Returns ``None`` when ``github_app_private_key`` is empty."""
    result = await _build_github_app_auth_headers(
        _settings(
            github_app_id="1",
            github_app_private_key="",
            github_app_installation_id="1",
        ),
        "test",
    )
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_installation_id_empty() -> None:
    """Returns ``None`` when ``github_app_installation_id`` is empty."""
    result = await _build_github_app_auth_headers(
        _settings(
            github_app_id="1",
            github_app_private_key="k",
            github_app_installation_id="",
        ),
        "test",
    )
    assert result is None


# ---------------------------------------------------------------------------
# cached token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_cached_token() -> None:
    """Returns the cached token without calling ``mint_installation_token``."""
    settings = _settings(
        github_app_id="1",
        github_app_private_key="k",
        github_app_installation_id="inst-1",
    )
    cache: dict[str, str] = {"inst-1": "cached-token"}

    with patch("robotsix_github_auth.mint_installation_token") as mock_mint:
        result = await _build_github_app_auth_headers(
            settings, "test", token_cache=cache
        )

    assert result == "cached-token"
    mock_mint.assert_not_called()


# ---------------------------------------------------------------------------
# mint new token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mints_and_caches_new_token() -> None:
    """Calls ``mint_installation_token`` and populates the cache."""
    settings = _settings(
        github_app_id="456",
        github_app_private_key="my-key",
        github_app_installation_id="inst-2",
    )
    cache: dict[str, str] = {}

    mock_token = MagicMock()
    mock_token.token = "fresh-token"

    with patch(
        "robotsix_github_auth.mint_installation_token", return_value=mock_token
    ) as mock_mint:
        result = await _build_github_app_auth_headers(
            settings, "test", token_cache=cache
        )

    assert result == "fresh-token"
    assert cache == {"inst-2": "fresh-token"}
    mock_mint.assert_called_once_with(
        app_id="456",
        private_key="my-key",
        installation_id="inst-2",
    )


# ---------------------------------------------------------------------------
# RuntimeError → None (fallback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_none_on_runtime_error(caplog: pytest.LogCaptureFixture) -> None:
    """Returns ``None`` and logs a warning when minting raises ``RuntimeError``."""
    settings = _settings(
        github_app_id="789",
        github_app_private_key="bad-key",
        github_app_installation_id="inst-3",
    )

    with patch(
        "robotsix_github_auth.mint_installation_token",
        side_effect=RuntimeError("token minting failed"),
    ):
        result = await _build_github_app_auth_headers(settings, "test")

    assert result is None
    assert "test" in caplog.text
    assert "token minting failed" in caplog.text

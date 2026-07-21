"""Tests for the version-check integration.

:func:`build_version_check_tools`, :class:`VersionCheckClient`, and the
:func:`_parse_version` helper, with ``respx`` mocked so there are no real
network calls.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings, VersionCheckSettings
from robotsix_chat.version_check import build_version_check_tools
from robotsix_chat.version_check.client import (
    VersionCheckClient,
    _parse_version,
    compare_versions,
)


def _settings(**kw: Any) -> VersionCheckSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "repo": "org/my-repo",
    }
    base.update(kw)
    return VersionCheckSettings(**base)


def _direct_repo(**kw: Any) -> DirectRepoSettings:
    """Minimal DirectRepoSettings for tests — App not configured by default."""
    return DirectRepoSettings(**kw)


# ---------------------------------------------------------------------------
# _parse_version
# ---------------------------------------------------------------------------


def test_parse_version_simple() -> None:
    """Parsing a plain semver string yields correct int tuple."""
    assert _parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_v_prefix() -> None:
    """Leading v/V is stripped."""
    assert _parse_version("v1.2.3") == (1, 2, 3)
    assert _parse_version("V1.2.3") == (1, 2, 3)


def test_parse_version_prerelease_suffix() -> None:
    """Pre-release suffix is ignored — stops at first non-numeric part."""
    assert _parse_version("1.2.0-rc1") == (1, 2, 0)


def test_parse_version_unparsable() -> None:
    """Unparsable input returns empty tuple."""
    assert _parse_version("not-a-version") == ()
    assert _parse_version("") == ()


def test_parse_version_extra_segments_ignored() -> None:
    """Only the leading numeric portions of each segment matter."""
    assert _parse_version("2.0.0-beta.1") == (2, 0, 0)


# ---------------------------------------------------------------------------
# compare_versions
# ---------------------------------------------------------------------------


def test_compare_versions_current_newer() -> None:
    """Return True when current > latest."""
    assert compare_versions("1.2.0", "1.1.0") is True


def test_compare_versions_equal() -> None:
    """Return True when current == latest."""
    assert compare_versions("1.2.0", "1.2.0") is True


def test_compare_versions_current_older() -> None:
    """Return False when current < latest."""
    assert compare_versions("0.1.0", "0.2.0") is False


def test_compare_versions_non_semantic_fallback() -> None:
    """When parsing fails, fall back to exact string equality."""
    assert compare_versions("abc", "abc") is True
    assert compare_versions("abc", "def") is False


def test_compare_versions_one_unparsable() -> None:
    """When one side is unparsable, fall back to equality."""
    assert compare_versions("1.0.0", "not-ver") is False
    assert compare_versions("not-ver", "1.0.0") is False


# ---------------------------------------------------------------------------
# build_version_check_tools
# ---------------------------------------------------------------------------


def test_build_disabled_returns_empty() -> None:
    """Disabled version_check returns no tools."""
    assert (
        build_version_check_tools(VersionCheckSettings(enabled=False), _direct_repo())
        == []
    )


def test_build_enabled_returns_one_tool() -> None:
    """Enabled version_check returns a single callable named check_for_updates."""
    tools = build_version_check_tools(_settings(), _direct_repo())
    assert len(tools) == 1
    assert tools[0].__name__ == "check_for_updates"


# ---------------------------------------------------------------------------
# VersionCheckClient.latest_version — up-to-date (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_up_to_date(respx_mock: respx.MockRouter) -> None:
    """When current == latest, summary says up-to-date and shows both versions."""
    respx_mock.get("https://api.github.com/repos/org/my-repo/releases/latest").mock(
        return_value=httpx.Response(200, json={"tag_name": "0.1.0"})
    )

    client = VersionCheckClient(_settings(), _direct_repo())
    version, source = await client.latest_version()
    assert version == "0.1.0"
    assert source == "releases/latest"


@pytest.mark.asyncio
async def test_behind(respx_mock: respx.MockRouter) -> None:
    """When current < latest, the client returns the newer version."""
    respx_mock.get("https://api.github.com/repos/org/my-repo/releases/latest").mock(
        return_value=httpx.Response(200, json={"tag_name": "0.2.0"})
    )

    client = VersionCheckClient(_settings(), _direct_repo())
    version, source = await client.latest_version()
    assert version == "0.2.0"


# ---------------------------------------------------------------------------
# VersionCheckClient — tags fallback (404 on releases/latest)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_releases_latest_404_falls_back_to_tags(
    respx_mock: respx.MockRouter,
) -> None:
    """When releases/latest returns 404, the tags endpoint is tried."""
    respx_mock.get("https://api.github.com/repos/org/my-repo/releases/latest").mock(
        return_value=httpx.Response(404, json={})
    )
    tags_route = respx_mock.get("https://api.github.com/repos/org/my-repo/tags").mock(
        return_value=httpx.Response(200, json=[{"name": "v0.1.0"}, {"name": "v0.0.9"}])
    )

    client = VersionCheckClient(_settings(), _direct_repo())
    version, source = await client.latest_version()
    assert version == "v0.1.0"
    assert "tags" in source.lower()
    assert tags_route.called


# ---------------------------------------------------------------------------
# VersionCheckClient — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_returns_none(
    respx_mock: respx.MockRouter,
) -> None:
    """An HTTP error returns (None, reason) — never raises."""
    respx_mock.get("https://api.github.com/repos/org/my-repo/releases/latest").mock(
        return_value=httpx.Response(500, json={})
    )

    client = VersionCheckClient(_settings(), _direct_repo())
    version, source = await client.latest_version()
    assert version is None
    assert "500" in source


@pytest.mark.asyncio
async def test_timeout_returns_none(
    respx_mock: respx.MockRouter,
) -> None:
    """A timeout returns (None, reason) — never raises."""
    respx_mock.get("https://api.github.com/repos/org/my-repo/releases/latest").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    client = VersionCheckClient(_settings(), _direct_repo())
    version, source = await client.latest_version()
    assert version is None
    assert "timed out" in source.lower()


@pytest.mark.asyncio
async def test_unexpected_error_returns_none(
    respx_mock: respx.MockRouter,
) -> None:
    """An unexpected exception returns (None, reason) — never raises."""
    respx_mock.get("https://api.github.com/repos/org/my-repo/releases/latest").mock(
        side_effect=RuntimeError("something crashed")
    )

    client = VersionCheckClient(_settings(), _direct_repo())
    version, source = await client.latest_version()
    assert version is None
    assert "something crashed" in source


@pytest.mark.asyncio
async def test_no_token_header_when_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """When no token is configured, no Authorization header is sent."""
    route = respx_mock.get(
        "https://api.github.com/repos/org/my-repo/releases/latest"
    ).mock(return_value=httpx.Response(200, json={"tag_name": "0.1.0"}))

    client = VersionCheckClient(_settings(), _direct_repo())
    await client.latest_version()
    assert "authorization" not in route.calls.last.request.headers


@pytest.mark.asyncio
async def test_app_token_header_when_configured(
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When App creds are configured, Bearer auth via mint_installation_token."""
    route = respx_mock.get(
        "https://api.github.com/repos/org/my-repo/releases/latest"
    ).mock(return_value=httpx.Response(200, json={"tag_name": "0.1.0"}))

    import sys
    from types import SimpleNamespace

    async def _fake_mint(**kw: object) -> str:
        return "app-token-456"

    fake = SimpleNamespace()
    fake.mint_installation_token = _fake_mint
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)

    dr = _direct_repo(
        github_app_id="12345",
        github_app_private_key="fake-key",  # type: ignore[arg-type]
        github_app_installation_id="67890",
    )
    client = VersionCheckClient(_settings(), dr)
    await client.latest_version()
    assert route.calls.last.request.headers["authorization"] == "Bearer app-token-456"


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_reuses_result_within_ttl(
    respx_mock: respx.MockRouter,
) -> None:
    """Two calls within cache_ttl perform only one HTTP fetch."""
    route = respx_mock.get(
        "https://api.github.com/repos/org/my-repo/releases/latest"
    ).mock(return_value=httpx.Response(200, json={"tag_name": "0.1.0"}))

    client = VersionCheckClient(_settings(cache_ttl=10.0), _direct_repo())
    v1, s1 = await client.latest_version()
    v2, s2 = await client.latest_version()

    assert v1 == "0.1.0"
    assert v2 == "0.1.0"
    assert s2 == "cached"

    # Only a single HTTP request was made.
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_failed_lookup_not_cached(
    respx_mock: respx.MockRouter,
) -> None:
    """A failed lookup is never cached — retries the HTTP call."""
    responses = iter(
        [
            httpx.Response(500, json={}),
            httpx.Response(200, json={"tag_name": "0.2.0"}),
        ]
    )
    route = respx_mock.get(
        "https://api.github.com/repos/org/my-repo/releases/latest"
    ).mock(side_effect=lambda request: next(responses))

    client = VersionCheckClient(_settings(cache_ttl=10.0), _direct_repo())
    v1, s1 = await client.latest_version()
    assert v1 is None

    v2, s2 = await client.latest_version()
    assert v2 == "0.2.0"

    # Two HTTP requests were made — the failure was not cached.
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# Full tool integration (check_for_updates)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_for_updates_up_to_date(
    respx_mock: respx.MockRouter,
) -> None:
    """The tool returns an up-to-date summary when current == latest."""
    respx_mock.get("https://api.github.com/repos/org/my-repo/releases/latest").mock(
        return_value=httpx.Response(200, json={"tag_name": "0.1.0"})
    )

    tools = build_version_check_tools(_settings(), _direct_repo())
    tool = tools[0]
    result = await tool()

    assert "up to date" in result.lower() or "running the latest" in result.lower()
    assert "0.1.0" in result


@pytest.mark.asyncio
async def test_check_for_updates_out_of_date(
    respx_mock: respx.MockRouter,
) -> None:
    """The tool warns when the deployment is behind."""
    respx_mock.get("https://api.github.com/repos/org/my-repo/releases/latest").mock(
        return_value=httpx.Response(200, json={"tag_name": "0.2.0"})
    )

    tools = build_version_check_tools(_settings(), _direct_repo())
    tool = tools[0]
    result = await tool()

    assert "out of date" in result.lower()
    assert "0.2.0" in result
    assert "releases/latest" in result


@pytest.mark.asyncio
async def test_check_for_updates_api_failure(
    respx_mock: respx.MockRouter,
) -> None:
    """When the API fails, the tool returns a graceful 'Could not determine' message."""
    respx_mock.get("https://api.github.com/repos/org/my-repo/releases/latest").mock(
        return_value=httpx.Response(500, json={})
    )

    tools = build_version_check_tools(_settings(), _direct_repo())
    tool = tools[0]
    result = await tool()

    assert "could not determine" in result.lower()
    assert "0.1.0" in result


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_settings_enabled_repo_required() -> None:
    """When enabled and repo is empty, Settings construction raises ValueError."""
    from robotsix_chat.config import Settings

    with pytest.raises(ValueError, match="version_check.repo"):
        Settings(
            version_check={"enabled": True, "repo": ""},  # type: ignore[arg-type]
        )

"""Tests for the version-check integration.

:func:`build_version_check_tools`, :class:`VersionCheckClient`, and the
:func:`_parse_version` helper, with ``httpx`` mocked so there are no real
network calls.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from robotsix_chat.config import VersionCheckSettings
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


# ---------------------------------------------------------------------------
# Shared mock helpers (mirrors the refdocs / board_reader pattern)
# ---------------------------------------------------------------------------


class _MockResponse:
    """Minimal httpx.Response stand-in for testing."""

    def __init__(self, json_data: Any, status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    @property
    def text(self) -> str:
        import json as _json

        return _json.dumps(self._json_data)

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=object(),  # type: ignore[arg-type]
                response=self,  # type: ignore[arg-type]
            )


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_MockResponse] | _MockResponse,
) -> dict[str, Any]:
    """Replace ``httpx.AsyncClient`` with a factory returning *responses*.

    Returns a ``captured`` dict with ``calls`` — a list of ``(url, headers)``
    tuples for each ``get`` call made.
    """
    captured: dict[str, Any] = {"calls": []}
    _responses: list[_MockResponse] = (
        responses if isinstance(responses, list) else [responses]
    )

    # Shared index so consecutive AsyncClient instances advance too.
    idx_counter = {"i": 0}

    class _BoundClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _BoundClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> _MockResponse:
            captured["calls"].append((url, headers))
            i = idx_counter["i"]
            resp = _responses[i]
            # Advance the index but clamp so the last response repeats.
            if i + 1 < len(_responses):
                idx_counter["i"] = i + 1
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", _BoundClient)
    return captured


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


def test_parse_version_unparseable() -> None:
    """Unparseable input returns empty tuple."""
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


def test_compare_versions_one_unparseable() -> None:
    """When one side is unparseable, fall back to equality."""
    assert compare_versions("1.0.0", "not-ver") is False
    assert compare_versions("not-ver", "1.0.0") is False


# ---------------------------------------------------------------------------
# build_version_check_tools
# ---------------------------------------------------------------------------


def test_build_disabled_returns_empty() -> None:
    """Disabled version_check returns no tools."""
    assert build_version_check_tools(VersionCheckSettings(enabled=False)) == []


def test_build_enabled_returns_one_tool() -> None:
    """Enabled version_check returns a single callable named check_for_updates."""
    tools = build_version_check_tools(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "check_for_updates"


# ---------------------------------------------------------------------------
# VersionCheckClient.latest_version — up-to-date (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """When current == latest, summary says up-to-date and shows both versions."""
    resp = _MockResponse({"tag_name": "0.1.0"})
    _install_mock_client(monkeypatch, resp)

    client = VersionCheckClient(_settings())
    version, source = await client.latest_version()
    assert version == "0.1.0"
    assert source == "releases/latest"


@pytest.mark.asyncio
async def test_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """When current < latest, the client returns the newer version."""
    resp = _MockResponse({"tag_name": "0.2.0"})
    _install_mock_client(monkeypatch, resp)

    client = VersionCheckClient(_settings())
    version, source = await client.latest_version()
    assert version == "0.2.0"


# ---------------------------------------------------------------------------
# VersionCheckClient — tags fallback (404 on releases/latest)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_releases_latest_404_falls_back_to_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When releases/latest returns 404, the tags endpoint is tried."""
    not_found = _MockResponse({}, status_code=404)
    tags_resp = _MockResponse([{"name": "v0.1.0"}, {"name": "v0.0.9"}])
    captured = _install_mock_client(monkeypatch, [not_found, tags_resp])

    client = VersionCheckClient(_settings())
    version, source = await client.latest_version()
    assert version == "v0.1.0"
    assert "tags" in source.lower()

    calls = captured["calls"]
    assert len(calls) == 2
    assert "releases/latest" in calls[0][0]
    assert "tags" in calls[1][0]


# ---------------------------------------------------------------------------
# VersionCheckClient — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP error returns (None, reason) — never raises."""
    resp = _MockResponse({}, status_code=500)
    _install_mock_client(monkeypatch, resp)

    client = VersionCheckClient(_settings())
    version, source = await client.latest_version()
    assert version is None
    assert "500" in source


@pytest.mark.asyncio
async def test_timeout_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout returns (None, reason) — never raises."""

    class _TimeoutClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _TimeoutClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> None:
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)

    client = VersionCheckClient(_settings())
    version, source = await client.latest_version()
    assert version is None
    assert "timed out" in source.lower()


@pytest.mark.asyncio
async def test_unexpected_error_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception returns (None, reason) — never raises."""

    class _BrokenClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _BrokenClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> None:
            raise RuntimeError("something crashed")

    monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)

    client = VersionCheckClient(_settings())
    version, source = await client.latest_version()
    assert version is None
    assert "something crashed" in source


@pytest.mark.asyncio
async def test_no_token_header_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no token is configured, no Authorization header is sent."""
    resp = _MockResponse({"tag_name": "0.1.0"})
    captured = _install_mock_client(monkeypatch, resp)

    client = VersionCheckClient(_settings())
    await client.latest_version()
    headers = captured["calls"][0][1]
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_token_header_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a token is configured, Bearer auth is sent."""
    resp = _MockResponse({"tag_name": "0.1.0"})
    captured = _install_mock_client(monkeypatch, resp)

    client = VersionCheckClient(
        _settings(github_token="ghp_test")  # pragma: allowlist secret
    )
    await client.latest_version()
    headers = captured["calls"][0][1]
    assert headers.get("Authorization") == "Bearer ghp_test"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_reuses_result_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two calls within cache_ttl perform only one HTTP fetch."""
    resp = _MockResponse({"tag_name": "0.1.0"})
    captured = _install_mock_client(monkeypatch, resp)

    client = VersionCheckClient(_settings(cache_ttl=10.0))
    v1, s1 = await client.latest_version()
    v2, s2 = await client.latest_version()

    assert v1 == "0.1.0"
    assert v2 == "0.1.0"
    assert s2 == "cached"

    # Only a single HTTP request was made.
    assert len(captured["calls"]) == 1


@pytest.mark.asyncio
async def test_failed_lookup_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed lookup is never cached — retries the HTTP call."""
    resp = _MockResponse({}, status_code=500)
    resp2 = _MockResponse({"tag_name": "0.2.0"})
    captured = _install_mock_client(monkeypatch, [resp, resp2])

    client = VersionCheckClient(_settings(cache_ttl=10.0))
    v1, s1 = await client.latest_version()
    assert v1 is None

    v2, s2 = await client.latest_version()
    assert v2 == "0.2.0"

    # Two HTTP requests were made — the failure was not cached.
    assert len(captured["calls"]) == 2


# ---------------------------------------------------------------------------
# Full tool integration (check_for_updates)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_for_updates_up_to_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool returns an up-to-date summary when current == latest."""
    resp = _MockResponse({"tag_name": "0.1.0"})
    _install_mock_client(monkeypatch, resp)

    tools = build_version_check_tools(_settings())
    tool = tools[0]
    result = await tool()

    assert "up to date" in result.lower() or "running the latest" in result.lower()
    assert "0.1.0" in result


@pytest.mark.asyncio
async def test_check_for_updates_out_of_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool warns when the deployment is behind."""
    resp = _MockResponse({"tag_name": "0.2.0"})
    _install_mock_client(monkeypatch, resp)

    tools = build_version_check_tools(_settings())
    tool = tools[0]
    result = await tool()

    assert "out of date" in result.lower()
    assert "0.2.0" in result
    assert "releases/latest" in result


@pytest.mark.asyncio
async def test_check_for_updates_api_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the API fails, the tool returns a graceful 'Could not determine' message."""
    resp = _MockResponse({}, status_code=500)
    _install_mock_client(monkeypatch, resp)

    tools = build_version_check_tools(_settings())
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

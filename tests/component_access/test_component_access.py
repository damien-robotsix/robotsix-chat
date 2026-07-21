"""Tests for the component_access package.

Uses ``respx`` (httpx transport-layer mocking) so the tests run without a
real network.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx
from pydantic import SecretStr

import robotsix_chat.component_access.roster as _roster
from robotsix_chat.component_access.roster import (
    _cache_valid,
    build_skill_prompt,
    fetch_roster,
    fetch_roster_sync,
)
from robotsix_chat.component_access.tools import (
    _component_request_impl,
    build_component_access_tools,
)
from robotsix_chat.config import CentralDeploySettings


def _settings(**kw: object) -> CentralDeploySettings:
    base: dict[str, object] = {}
    base.update(kw)
    return CentralDeploySettings(**base)  # type: ignore[arg-type]


def _wipe_cache() -> None:
    """Reset the module-level roster cache between tests."""
    _roster._cache = None
    _roster._last_non_empty_cache = None


# ---------------------------------------------------------------------------
# _cache_valid
# ---------------------------------------------------------------------------


def test_cache_valid_none_is_false() -> None:
    """None cache is never valid."""
    _wipe_cache()
    assert _cache_valid(300.0) is False


def test_cache_valid_expired() -> None:
    """Expired cache reports false."""
    _wipe_cache()
    # time.monotonic() starts near 0 at boot, so an absolute 0.0 can read
    # as "fresh" on a newly booted CI runner — anchor relative to now.
    _roster._cache = (time.monotonic() - 301.0, [])  # just past the 300s TTL
    assert _cache_valid(300.0) is False


# ---------------------------------------------------------------------------
# fetch_roster
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_roster_empty_url_returns_empty() -> None:
    """When URL is empty, return [] without any HTTP call."""
    _wipe_cache()
    result = await fetch_roster(CentralDeploySettings())
    assert result == []


@pytest.mark.asyncio
async def test_fetch_roster_cache_hit(
    respx_mock: respx.MockRouter,
) -> None:
    """Fresh cache is returned without a new HTTP call."""
    _wipe_cache()
    entries = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    # Seed the cache with a very recent timestamp.
    import time

    _roster._cache = (time.monotonic(), entries)


@pytest.mark.asyncio
async def test_fetch_roster_success(
    respx_mock: respx.MockRouter,
) -> None:
    """Successful roster fetch returns entries and caches them."""
    _wipe_cache()
    entries = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=entries)
    )

    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = await fetch_roster(settings)
    assert result == entries
    assert _roster._cache is not None
    assert _roster._cache[1] == entries


@pytest.mark.asyncio
async def test_fetch_roster_http_error(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP error returns a single error entry."""
    _wipe_cache()
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(500, text="boom")
    )

    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = await fetch_roster(settings)
    assert len(result) == 1
    assert result[0]["id"] == "_error"
    assert "Roster unavailable" in result[0]["_error"]


@pytest.mark.asyncio
async def test_fetch_roster_network_error(
    respx_mock: respx.MockRouter,
) -> None:
    """Network error returns an error entry."""
    _wipe_cache()
    respx_mock.get("http://deploy:8080/chat/components").mock(
        side_effect=httpx.ConnectError("refused")
    )

    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = await fetch_roster(settings)
    assert len(result) == 1
    assert result[0]["id"] == "_error"
    assert "refused" in result[0]["_error"]


@pytest.mark.asyncio
async def test_fetch_roster_non_list_response(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-list JSON response returns empty list."""
    _wipe_cache()
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json={"not": "a list"})
    )

    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = await fetch_roster(settings)
    assert result == []


@pytest.mark.asyncio
async def test_fetch_roster_empty_list_not_cached(
    respx_mock: respx.MockRouter,
) -> None:
    """Empty roster is not cached — next call re-fetches."""
    _wipe_cache()

    # First: prime a non-empty cache so we have a known state.
    entries = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=entries)
    )
    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = await fetch_roster(settings)
    assert result == entries

    # Second: the upstream returns an empty list.
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=[])
    )
    # Expire the cache so we actually fetch.
    _roster._cache = (time.monotonic() - 301.0, entries)
    cache_before = _roster._cache  # snapshot before empty fetch
    result = await fetch_roster(settings)
    # Should fall back to the last non-empty cache, not the empty list.
    assert result == entries
    # Cache must not have been updated (identity check) — the empty-path
    # deliberately leaves _cache alone so it isn't poisoned by [].
    assert _roster._cache is cache_before


@pytest.mark.asyncio
async def test_fetch_roster_empty_list_no_fallback_returns_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """Empty roster with no prior non-empty cache returns []."""
    _wipe_cache()
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=[])
    )
    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = await fetch_roster(settings)
    assert result == []
    # Cache must not have been set.
    assert _roster._cache is None


@pytest.mark.asyncio
async def test_fetch_roster_with_token(
    respx_mock: respx.MockRouter,
) -> None:
    """The API token is sent as X-API-Key (central-deploy's accepted scheme)."""
    _wipe_cache()
    entries = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    route = respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=entries)
    )

    settings = _settings(
        url="http://deploy:8080",
        api_token=SecretStr("tk-secret"),
        roster_cache_ttl=300.0,
    )
    result = await fetch_roster(settings)
    assert result == entries
    assert route.called
    assert route.calls[0].request.headers["X-API-Key"] == "tk-secret"
    assert "Authorization" not in route.calls[0].request.headers


@pytest.mark.asyncio
async def test_fetch_roster_strips_trailing_slash(
    respx_mock: respx.MockRouter,
) -> None:
    """Trailing slash on URL is stripped so path is not doubled."""
    _wipe_cache()
    entries: list[dict[str, object]] = []
    route = respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=entries)
    )

    settings = _settings(url="http://deploy:8080/", roster_cache_ttl=300.0)
    await fetch_roster(settings)
    assert route.called


# ---------------------------------------------------------------------------
# fetch_roster_sync
# ---------------------------------------------------------------------------


def test_fetch_roster_sync_empty_url() -> None:
    """When URL is empty, return []."""
    _wipe_cache()
    result = fetch_roster_sync(CentralDeploySettings())
    assert result == []


def test_fetch_roster_sync_cache_hit(
    respx_mock: respx.MockRouter,
) -> None:
    """Fresh sync cache is returned without a new HTTP call."""
    _wipe_cache()
    entries = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    import time

    _roster._cache = (time.monotonic(), entries)

    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = fetch_roster_sync(settings)
    assert result == entries


def test_fetch_roster_sync_success(
    respx_mock: respx.MockRouter,
) -> None:
    """Successful sync roster fetch returns entries."""
    _wipe_cache()
    entries = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=entries)
    )

    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = fetch_roster_sync(settings)
    assert result == entries


def test_fetch_roster_sync_http_error(
    respx_mock: respx.MockRouter,
) -> None:
    """Sync HTTP error returns error entry."""
    _wipe_cache()
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(500, text="boom")
    )

    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = fetch_roster_sync(settings)
    assert len(result) == 1
    assert result[0]["id"] == "_error"


def test_fetch_roster_sync_network_error(
    respx_mock: respx.MockRouter,
) -> None:
    """Sync network error returns error entry."""
    _wipe_cache()
    respx_mock.get("http://deploy:8080/chat/components").mock(
        side_effect=httpx.ConnectError("refused")
    )

    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = fetch_roster_sync(settings)
    assert len(result) == 1
    assert result[0]["id"] == "_error"


def test_fetch_roster_sync_non_list_response(
    respx_mock: respx.MockRouter,
) -> None:
    """Sync non-list JSON response returns empty list."""
    _wipe_cache()
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json={"not": "a list"})
    )

    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = fetch_roster_sync(settings)
    assert result == []


def test_fetch_roster_sync_empty_list_not_cached(
    respx_mock: respx.MockRouter,
) -> None:
    """Sync empty roster is not cached — next call re-fetches."""
    _wipe_cache()

    # Prime a non-empty cache.
    entries = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=entries)
    )
    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = fetch_roster_sync(settings)
    assert result == entries

    # Now return empty, with expired cache.
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=[])
    )
    _roster._cache = (time.monotonic() - 301.0, entries)
    result = fetch_roster_sync(settings)
    assert result == entries  # stale fallback


def test_fetch_roster_sync_empty_no_fallback_returns_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """Sync empty roster with no prior cache returns []."""
    _wipe_cache()
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=[])
    )
    settings = _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    result = fetch_roster_sync(settings)
    assert result == []
    assert _roster._cache is None


# ---------------------------------------------------------------------------
# build_skill_prompt
# ---------------------------------------------------------------------------


def test_build_skill_prompt_empty() -> None:
    """Empty roster returns empty string."""
    assert build_skill_prompt([]) == ""


def test_build_skill_prompt_error_only() -> None:
    """Error-only entries are skipped, returns empty string."""
    entries = [{"id": "_error", "base_url": "", "skill": "", "_error": "fail"}]
    assert build_skill_prompt(entries) == ""


def test_build_skill_prompt_single_entry() -> None:
    """Single valid entry produces skill prompt with summary and skill."""
    entries = [
        {
            "id": "mill",
            "base_url": "http://m:8080",
            "skill": "# Mill Skill\n\nDo stuff.",
        }
    ]
    result = build_skill_prompt(entries)
    assert "# Available component skills" in result
    assert "## Component summary" in result
    assert "**mill**" in result
    assert "http://m:8080" in result
    assert "## mill" in result
    assert "# Mill Skill" in result
    assert "Do stuff." in result


def test_build_skill_prompt_multiple_entries() -> None:
    """Multiple entries all appear in summary and detail sections."""
    entries = [
        {"id": "mill", "base_url": "http://m:8080", "skill": "Skill A"},
        {"id": "board", "base_url": "http://b:8080", "skill": "Skill B"},
    ]
    result = build_skill_prompt(entries)
    assert "**mill**" in result
    assert "**board**" in result
    assert "## mill" in result
    assert "## board" in result
    assert "Skill A" in result
    assert "Skill B" in result


def test_build_skill_prompt_mixed_error_and_valid() -> None:
    """Error entries are filtered out, only valid entries appear."""
    entries = [
        {"id": "_error", "base_url": "", "skill": "", "_error": "fail"},
        {"id": "mill", "base_url": "http://m:8080", "skill": "Skill A"},
    ]
    result = build_skill_prompt(entries)
    assert "_error" not in result
    assert "mill" in result


def test_build_skill_prompt_missing_skill_field() -> None:
    """Entries with no skill field are skipped."""
    entries = [{"id": "mill", "base_url": "http://m:8080"}]
    assert build_skill_prompt(entries) == ""


# ---------------------------------------------------------------------------
# _component_request_impl
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_component_request_unknown_id() -> None:
    """Unknown component_id returns a clear error listing known ids."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    result = await _component_request_impl(roster, "unknown", "GET", "/tickets")
    assert "unknown component_id" in result.lower()
    assert "mill" in result


@pytest.mark.asyncio
async def test_component_request_empty_roster() -> None:
    """Empty roster returns an explicit unavailable message, not unknown id."""
    result = await _component_request_impl([], "mill", "GET", "/tickets")
    assert "empty or unavailable" in result.lower()
    assert "unknown component_id" not in result.lower()


@pytest.mark.asyncio
async def test_component_request_error_only_roster() -> None:
    """Roster with only error entries returns unavailable message."""
    roster = [
        {"id": "_error", "base_url": "", "skill": "", "_error": "Roster down"},
        {"id": "_error", "base_url": "", "skill": "", "_error": "Also down"},
    ]
    result = await _component_request_impl(roster, "mill", "GET", "/tickets")
    assert "empty or unavailable" in result.lower()
    assert "unknown component_id" not in result.lower()


@pytest.mark.asyncio
async def test_component_request_error_entry() -> None:
    """When the only entry is an error entry, roster-unavailable message is shown."""
    roster = [{"id": "_error", "base_url": "", "skill": "", "_error": "Roster down"}]
    result = await _component_request_impl(roster, "_error", "GET", "/tickets")
    assert "empty or unavailable" in result.lower()


@pytest.mark.asyncio
async def test_component_request_no_base_url() -> None:
    """Component with no base_url returns an error."""
    roster = [{"id": "mill", "base_url": "", "skill": "..."}]
    result = await _component_request_impl(roster, "mill", "GET", "/tickets")
    assert "no base_url" in result.lower()


@pytest.mark.asyncio
async def test_component_request_absolute_url_path() -> None:
    """Absolute URL in path is refused."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    result = await _component_request_impl(
        roster, "mill", "GET", "http://evil.com/tickets"
    )
    assert "absolute url" in result.lower()


@pytest.mark.asyncio
async def test_component_request_double_slash_path() -> None:
    """//-prefixed path is refused as absolute URL."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    result = await _component_request_impl(roster, "mill", "GET", "//evil.com/tickets")
    assert "absolute url" in result.lower()


@pytest.mark.asyncio
async def test_component_request_unsupported_method() -> None:
    """Unsupported HTTP method returns an error."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    result = await _component_request_impl(roster, "mill", "OPTIONS", "/tickets")
    assert "unsupported http method" in result.lower()


@pytest.mark.asyncio
async def test_component_request_get_success(
    respx_mock: respx.MockRouter,
) -> None:
    """Successful GET returns status code and JSON body."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://m:8080/tickets").mock(
        return_value=httpx.Response(200, json=[{"id": 1}])
    )
    result = await _component_request_impl(roster, "mill", "GET", "/tickets")
    assert "HTTP 200" in result
    assert '"id": 1' in result


@pytest.mark.asyncio
async def test_component_request_post_with_body(
    respx_mock: respx.MockRouter,
) -> None:
    """POST with json_body sends it and returns response."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    route = respx_mock.post("http://m:8080/tickets").mock(
        return_value=httpx.Response(201, json={"id": 42})
    )
    result = await _component_request_impl(
        roster, "mill", "POST", "/tickets", {"title": "hello"}
    )
    assert "HTTP 201" in result
    assert route.called
    assert route.calls[0].request.headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_component_request_put(
    respx_mock: respx.MockRouter,
) -> None:
    """PUT request works."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.put("http://m:8080/tickets/1").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    result = await _component_request_impl(
        roster, "mill", "PUT", "/tickets/1", {"title": "updated"}
    )
    assert "HTTP 200" in result


@pytest.mark.asyncio
async def test_component_request_patch(
    respx_mock: respx.MockRouter,
) -> None:
    """PATCH request works."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.patch("http://m:8080/tickets/1").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    result = await _component_request_impl(
        roster, "mill", "PATCH", "/tickets/1", {"title": "patched"}
    )
    assert "HTTP 200" in result


@pytest.mark.asyncio
async def test_component_request_delete(
    respx_mock: respx.MockRouter,
) -> None:
    """DELETE request works."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.delete("http://m:8080/tickets/1").mock(
        return_value=httpx.Response(204, text="")
    )
    result = await _component_request_impl(roster, "mill", "DELETE", "/tickets/1")
    assert "HTTP 204" in result


@pytest.mark.asyncio
async def test_component_request_non_json_response(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-JSON response is returned as plain text."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://m:8080/health").mock(
        return_value=httpx.Response(200, text="OK")
    )
    result = await _component_request_impl(roster, "mill", "GET", "/health")
    assert "HTTP 200" in result
    assert "OK" in result


@pytest.mark.asyncio
async def test_component_request_http_error(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP errors (4xx/5xx) return the status and body."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://m:8080/bad").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    result = await _component_request_impl(roster, "mill", "GET", "/bad")
    assert "HTTP 404" in result
    assert "not found" in result


@pytest.mark.asyncio
async def test_component_request_network_error(
    respx_mock: respx.MockRouter,
) -> None:
    """Network errors are caught and returned as error text."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://m:8080/tickets").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await _component_request_impl(roster, "mill", "GET", "/tickets")
    assert "Error calling" in result
    assert "connection refused" in result


@pytest.mark.asyncio
async def test_component_request_timeout(
    respx_mock: respx.MockRouter,
) -> None:
    """Timeout errors are caught and returned as error text."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://m:8080/tickets").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    result = await _component_request_impl(roster, "mill", "GET", "/tickets")
    assert "Error calling" in result
    assert "timed out" in result


@pytest.mark.asyncio
async def test_component_request_truncates_long_response(
    respx_mock: respx.MockRouter,
) -> None:
    """Very long response body is truncated for write methods."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    long_text = "x" * 10000
    respx_mock.post("http://m:8080/big").mock(
        return_value=httpx.Response(200, text=long_text)
    )
    result = await _component_request_impl(roster, "mill", "POST", "/big")
    assert "truncated" in result
    assert len(result) < len(long_text) + 100  # should be shorter than original


@pytest.mark.asyncio
async def test_component_request_get_uses_read_response_limit(
    respx_mock: respx.MockRouter,
) -> None:
    """GET responses respect the read_response_max_chars limit."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    long_text = "x" * 10000
    respx_mock.get("http://m:8080/big").mock(
        return_value=httpx.Response(200, text=long_text)
    )
    # Default read_response_max_chars is 8000 (fallback), so GET
    # should also truncate with the default.
    result = await _component_request_impl(roster, "mill", "GET", "/big")
    assert "truncated" in result
    assert len(result) < len(long_text) + 100

    # With a higher limit, the response should not be truncated.
    respx_mock.get("http://m:8080/big").mock(
        return_value=httpx.Response(200, text=long_text)
    )
    result = await _component_request_impl(
        roster,
        "mill",
        "GET",
        "/big",
        read_response_max_chars=200_000,
    )
    assert "truncated" not in result
    assert "x" * 10000 in result


@pytest.mark.asyncio
async def test_component_request_preserves_trailing_slash_base_url(
    respx_mock: respx.MockRouter,
) -> None:
    """Trailing slashes on base_url and leading slashes on path are normalised."""
    roster = [{"id": "mill", "base_url": "http://m:8080/", "skill": "..."}]
    route = respx_mock.get("http://m:8080/tickets").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await _component_request_impl(roster, "mill", "GET", "tickets")
    assert "HTTP 200" in result
    assert route.called


# ---------------------------------------------------------------------------
# build_component_access_tools
# ---------------------------------------------------------------------------


def test_build_tools_empty_url_returns_empty() -> None:
    """When URL is empty, returns no tools."""
    assert build_component_access_tools(CentralDeploySettings()) == []


def test_build_tools_with_url_returns_one_tool() -> None:
    """With a URL configured, returns exactly one tool."""
    tools = build_component_access_tools(_settings(url="http://deploy:8080"))
    assert len(tools) == 1
    assert tools[0].__name__ == "component_request"


@pytest.mark.asyncio
async def test_tool_refreshes_roster_on_call(
    respx_mock: respx.MockRouter,
) -> None:
    """Calling the tool refreshes the roster, then makes the request."""
    _wipe_cache()
    roster_entries = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=roster_entries)
    )
    respx_mock.get("http://m:8080/tickets").mock(
        return_value=httpx.Response(200, json=[{"id": 1}])
    )

    tools = build_component_access_tools(
        _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    )
    tool = tools[0]
    result = await tool("mill", "GET", "/tickets")
    assert "HTTP 200" in result


@pytest.mark.asyncio
async def test_tool_max_response_chars_overrides_default(
    respx_mock: respx.MockRouter,
) -> None:
    """max_response_chars per-call arg overrides the configured limit."""
    _wipe_cache()
    roster_entries = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://deploy:8080/chat/components").mock(
        return_value=httpx.Response(200, json=roster_entries)
    )
    # Response body is 8000 chars — exceeds the per-call limit of 500.
    long_text = "y" * 8000
    respx_mock.get("http://m:8080/tickets/5cd6").mock(
        return_value=httpx.Response(200, text=long_text)
    )

    tools = build_component_access_tools(
        _settings(url="http://deploy:8080", roster_cache_ttl=300.0)
    )
    tool = tools[0]

    # Per-call limit of 500 chars → response should be truncated.
    result = await tool("mill", "GET", "/tickets/5cd6", max_response_chars=500)
    assert "HTTP 200" in result
    assert "truncated at 500" in result
    assert len(result) < 2000  # well under the 8000-char body

    # Without the per-call arg, the configured default (200_000) applies.
    respx_mock.get("http://m:8080/tickets/5cd6").mock(
        return_value=httpx.Response(200, text=long_text)
    )
    result2 = await tool("mill", "GET", "/tickets/5cd6")
    assert "HTTP 200" in result2
    assert "truncated" not in result2
    assert "y" * 8000 in result2


# ---------------------------------------------------------------------------
# Roster auth metadata (virtual components: langfuse basic, deploy header)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_component_request_basic_auth_from_env(
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'basic' auth entry resolves env vars and sends Basic credentials."""
    monkeypatch.setenv("LF_PK", "pk-user")
    monkeypatch.setenv("LF_SK", "sk-pass")
    roster = [
        {
            "id": "langfuse",
            "base_url": "http://lf:3000",
            "skill": "...",
            "auth": {
                "type": "basic",
                "username_env": "LF_PK",
                "password_env": "LF_SK",  # pragma: allowlist secret
            },
        }
    ]
    route = respx_mock.get("http://lf:3000/api/public/traces").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    result = await _component_request_impl(
        roster, "langfuse", "GET", "/api/public/traces"
    )
    assert "HTTP 200" in result
    sent = route.calls.last.request
    import base64 as _b64

    expected = _b64.b64encode(b"pk-user:sk-pass").decode()
    assert sent.headers["Authorization"] == f"Basic {expected}"


@pytest.mark.asyncio
async def test_component_request_header_auth_from_env(
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'header' auth entry resolves the token env var into the header."""
    monkeypatch.setenv("DEPLOY_KEY", "tok-123")
    roster = [
        {
            "id": "deploy",
            "base_url": "http://cd:8100",
            "skill": "...",
            "auth": {
                "type": "header",
                "header_name": "X-API-Key",
                "token_env": "DEPLOY_KEY",
            },
        }
    ]
    route = respx_mock.get("http://cd:8100/services").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await _component_request_impl(roster, "deploy", "GET", "/services")
    assert "HTTP 200" in result
    assert route.calls.last.request.headers["X-API-Key"] == "tok-123"


@pytest.mark.asyncio
async def test_component_request_basic_auth_missing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing Basic-auth env vars yield a provisioning error, no request."""
    monkeypatch.delenv("LF_PK", raising=False)
    monkeypatch.delenv("LF_SK", raising=False)
    roster = [
        {
            "id": "langfuse",
            "base_url": "http://lf:3000",
            "skill": "...",
            "auth": {
                "type": "basic",
                "username_env": "LF_PK",
                "password_env": "LF_SK",  # pragma: allowlist secret
            },
        }
    ]
    result = await _component_request_impl(roster, "langfuse", "GET", "/x")
    assert "Error" in result
    assert "LF_PK" in result
    assert "EnvStore" in result


@pytest.mark.asyncio
async def test_component_request_header_auth_missing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing header-auth token env yields a provisioning error, no request."""
    monkeypatch.delenv("DEPLOY_KEY", raising=False)
    roster = [
        {
            "id": "deploy",
            "base_url": "http://cd:8100",
            "skill": "...",
            "auth": {
                "type": "header",
                "header_name": "X-API-Key",
                "token_env": "DEPLOY_KEY",
            },
        }
    ]
    result = await _component_request_impl(roster, "deploy", "GET", "/services")
    assert "Error" in result
    assert "DEPLOY_KEY" in result


@pytest.mark.asyncio
async def test_component_request_no_auth_unchanged(
    respx_mock: respx.MockRouter,
) -> None:
    """Entries without auth metadata behave exactly as before."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    route = respx_mock.get("http://m:8080/tickets").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await _component_request_impl(roster, "mill", "GET", "/tickets")
    assert "HTTP 200" in result
    assert "Authorization" not in route.calls.last.request.headers


# ---------------------------------------------------------------------------
# Retry-with-backoff tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_on_transient_network_error(
    respx_mock: respx.MockRouter,
) -> None:
    """Transient ConnectError is retried; exhausted attempts include count."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://m:8080/tickets").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await _component_request_impl(roster, "mill", "GET", "/tickets")
    assert "Error calling" in result
    assert "all 3 attempts failed" in result
    assert "connection refused" in result


@pytest.mark.asyncio
async def test_no_retry_on_terminal_4xx_for_get(
    respx_mock: respx.MockRouter,
) -> None:
    """4xx is terminal for idempotent GET — returned immediately without retries."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    route = respx_mock.get("http://m:8080/forbidden").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )
    result = await _component_request_impl(roster, "mill", "GET", "/forbidden")
    assert "HTTP 403" in result
    assert "forbidden" in result
    assert "all 3 attempts" not in result
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_no_retry_on_any_http_response_for_post(
    respx_mock: respx.MockRouter,
) -> None:
    """Any HTTP response is terminal for non-idempotent POST — even 5xx."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    route = respx_mock.post("http://m:8080/tickets").mock(
        return_value=httpx.Response(500, json={"error": "internal"})
    )
    result = await _component_request_impl(
        roster, "mill", "POST", "/tickets", {"title": "test"}
    )
    assert "HTTP 500" in result
    assert "all 3 attempts" not in result
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_retry_on_5xx_for_get(
    respx_mock: respx.MockRouter,
) -> None:
    """5xx is retried for idempotent GET — 503 on every attempt triggers 3 calls."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    route = respx_mock.get("http://m:8080/tickets").mock(
        return_value=httpx.Response(503, json={"error": "overloaded"})
    )
    result = await _component_request_impl(roster, "mill", "GET", "/tickets")
    assert "HTTP 503" in result
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_retry_on_empty_exception_message(
    respx_mock: respx.MockRouter,
) -> None:
    """Exception with empty message is transient — retried until exhausted."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://m:8080/tickets").mock(side_effect=Exception(""))
    result = await _component_request_impl(roster, "mill", "GET", "/tickets")
    assert "Error calling" in result
    assert "all 3 attempts failed" in result


@pytest.mark.asyncio
async def test_non_transient_exception_not_retried(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-transient exception (e.g. ValueError) is returned immediately, no retry."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    route = respx_mock.get("http://m:8080/tickets").mock(
        side_effect=ValueError("bad value")
    )
    result = await _component_request_impl(roster, "mill", "GET", "/tickets")
    assert "Error calling" in result
    assert "bad value" in result
    assert "all 3 attempts" not in result
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_retry_on_nested_transient_error(
    respx_mock: respx.MockRouter,
) -> None:
    """OSError is treated as transient network error and retried."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    respx_mock.get("http://m:8080/tickets").mock(
        side_effect=OSError("network unreachable")
    )
    result = await _component_request_impl(roster, "mill", "GET", "/tickets")
    assert "Error calling" in result
    assert "all 3 attempts failed" in result


@pytest.mark.asyncio
async def test_put_is_idempotent_retries_on_5xx(
    respx_mock: respx.MockRouter,
) -> None:
    """PUT is idempotent — 5xx responses are retried (3 calls total)."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    route = respx_mock.put("http://m:8080/tickets/1").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    result = await _component_request_impl(
        roster, "mill", "PUT", "/tickets/1", {"title": "x"}
    )
    assert "HTTP 500" in result
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_patch_is_non_idempotent_no_retry_on_any_response(
    respx_mock: respx.MockRouter,
) -> None:
    """PATCH is non-idempotent — any HTTP response is terminal (no retry)."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    route = respx_mock.patch("http://m:8080/tickets/1").mock(
        return_value=httpx.Response(503, json={"error": "overloaded"})
    )
    result = await _component_request_impl(
        roster, "mill", "PATCH", "/tickets/1", {"title": "patched"}
    )
    assert "HTTP 503" in result
    assert route.call_count == 1

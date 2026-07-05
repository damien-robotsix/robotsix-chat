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
    """Bearer token is sent in the Authorization header."""
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
    assert route.calls[0].request.headers["Authorization"] == "Bearer tk-secret"


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
    """When the only entry is an error entry, the roster-unavailable message is shown."""
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
    """Very long response body is truncated."""
    roster = [{"id": "mill", "base_url": "http://m:8080", "skill": "..."}]
    long_text = "x" * 10000
    respx_mock.get("http://m:8080/big").mock(
        return_value=httpx.Response(200, text=long_text)
    )
    result = await _component_request_impl(roster, "mill", "GET", "/big")
    assert "truncated" in result
    assert len(result) < len(long_text) + 100  # should be shorter than original


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

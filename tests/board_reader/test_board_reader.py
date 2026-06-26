"""Tests for the board-reader integration.

:func:`build_board_reader_tools` and :class:`BoardReader`, with ``httpx``
mocked so there are no real network calls.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from robotsix_chat.board_reader import build_board_reader_tools
from robotsix_chat.board_reader.client import BoardReader
from robotsix_chat.config import BoardReaderSettings
from tests.common.mock_helpers import (
    MockResponse as _MockResponse,
)
from tests.common.mock_helpers import (
    install_mock_client as _install_mock_client,
)
from tests.common.mock_helpers import (
    install_mock_dual_client as _install_mock_dual_client,
)


def _settings(**kw: Any) -> BoardReaderSettings:
    base: dict[str, Any] = {"enabled": True}
    base.update(kw)
    return BoardReaderSettings(**base)


# ---------------------------------------------------------------------------
# build_board_reader_tools
# ---------------------------------------------------------------------------


def test_build_board_reader_tools_disabled() -> None:
    """Verify that disabled board reader returns no tools."""
    assert build_board_reader_tools(BoardReaderSettings(enabled=False)) == []


def test_build_board_reader_tools_returns_three_tools() -> None:
    """Verify that enabled board reader returns list, read, and create tools."""
    tools = build_board_reader_tools(_settings())
    assert len(tools) == 3
    names = [t.__name__ for t in tools]
    assert "list_board_tickets" in names
    assert "read_board_ticket" in names
    assert "create_board_ticket" in names


# ---------------------------------------------------------------------------
# BoardReader.list_tickets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tickets_calls_get_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that list_tickets calls GET /tickets with correct params."""
    resp = _MockResponse(text='[{"id": "abc", "title": "Fix bug"}]')
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings(api_base_url="http://127.0.0.1:8077"))
    out = await client.list_tickets(repo_id="robotsix-chat")

    assert out == resp.text
    assert captured["url"] == "http://127.0.0.1:8077/tickets"
    assert captured["params"] == {"repo_id": "robotsix-chat"}


@pytest.mark.asyncio
async def test_list_tickets_includes_closed_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that include_closed and state are forwarded as query params."""
    resp = _MockResponse(text="[]")
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings())
    await client.list_tickets(
        repo_id="robotsix-mill",
        include_closed=True,
        state="ready",
    )

    assert captured["params"] == {
        "repo_id": "robotsix-mill",
        "include_closed": "true",
        "state": "ready",
    }


# ---------------------------------------------------------------------------
# BoardReader.get_ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticket_calls_get_tickets_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that get_ticket calls GET /tickets/{id}."""
    resp = _MockResponse(text='{"id": "abc", "title": "Fix bug", "state": "ready"}')
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings(api_base_url="http://localhost:8077"))
    out = await client.get_ticket("abc")

    assert out == resp.text
    assert captured["url"] == "http://localhost:8077/tickets/abc"


# ---------------------------------------------------------------------------
# BoardReader auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_token_sent_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that the Authorization header is set when api_token is given."""
    resp = _MockResponse(text="[]")
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings(api_token="secret-token"))
    await client.list_tickets(repo_id="robotsix-chat")

    assert captured["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer secret-token",
    }


@pytest.mark.asyncio
async def test_no_auth_header_when_token_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that no Authorization header is sent when api_token is empty."""
    resp = _MockResponse(text="[]")
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings(api_token=""))
    await client.list_tickets(repo_id="robotsix-chat")

    assert "Authorization" not in captured["headers"]


# ---------------------------------------------------------------------------
# BoardReader error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_returns_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that HTTP errors become a text message, never raised."""
    resp = _MockResponse(text="not found", status_code=404)
    _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings())
    out = await client.get_ticket("nonexistent")

    assert "404" in out
    assert "not found" in out


@pytest.mark.asyncio
async def test_timeout_returns_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that timeouts become a text message, never raised."""

    class _TimeoutClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _TimeoutClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            params: dict[str, str] | None = None,
        ) -> None:
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)

    client = BoardReader(_settings(timeout=5.0))
    out = await client.list_tickets(repo_id="x")

    assert "timed out" in out
    assert "5.0s" in out


@pytest.mark.asyncio
async def test_unexpected_error_returns_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that unexpected errors become a text message, never raised."""

    class _BrokenClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _BrokenClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            params: dict[str, str] | None = None,
        ) -> None:
            raise RuntimeError("something crashed")

    monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)

    client = BoardReader(_settings())
    out = await client.get_ticket("abc")

    assert "failed" in out.lower()
    assert "something crashed" in out


# ---------------------------------------------------------------------------
# BoardReader.create_ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ticket_calls_post_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that create_ticket calls POST /tickets with correct payload."""
    resp = _MockResponse(
        text='{"id": "abc", "title": "New ticket", "state": "draft"}',
        status_code=201,
    )
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings(api_base_url="http://127.0.0.1:8077"))
    out = await client.create_ticket(
        title="New ticket",
        description="A test ticket",
        repo_id="robotsix-chat",
    )

    assert out == resp.text
    assert captured["url"] == "http://127.0.0.1:8077/tickets"
    assert captured["json"] == {
        "title": "New ticket",
        "description": "A test ticket",
        "repo_id": "robotsix-chat",
    }


@pytest.mark.asyncio
async def test_create_ticket_with_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that kind is included in payload when provided."""
    resp = _MockResponse(text='{"id": "xyz", "kind": "bug"}', status_code=201)
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings())
    await client.create_ticket(
        title="Bug report",
        description="Something broke",
        repo_id="robotsix-mill",
        kind="bug",
    )

    assert captured["json"] == {
        "title": "Bug report",
        "description": "Something broke",
        "repo_id": "robotsix-mill",
        "kind": "bug",
    }


@pytest.mark.asyncio
async def test_create_ticket_omits_kind_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that kind is omitted from payload when empty string."""
    resp = _MockResponse(text='{"id": "abc"}', status_code=201)
    captured = _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings())
    await client.create_ticket(
        title="T",
        description="D",
        repo_id="r",
        kind="",
    )

    assert "kind" not in captured["json"]


@pytest.mark.asyncio
async def test_create_ticket_http_error_returns_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that create_ticket HTTP errors become text, never raised."""
    resp = _MockResponse(text="conflict", status_code=409)
    _install_mock_client(monkeypatch, resp)

    client = BoardReader(_settings())
    out = await client.create_ticket(
        title="Dup",
        description="Duplicate",
        repo_id="x",
    )

    assert "409" in out
    assert "conflict" in out


# ---------------------------------------------------------------------------
# BoardReader cache tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tickets_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second call to list_tickets with same params must hit cache (0 extra HTTP)."""
    get_counter: list[int] = [0]
    resp = _MockResponse(text='[{"id": "abc"}]')
    _install_mock_client(monkeypatch, resp, counter=get_counter)

    client = BoardReader(_settings(cache_ttl=60.0))
    out1 = await client.list_tickets(repo_id="robotsix-chat")
    out2 = await client.list_tickets(repo_id="robotsix-chat")

    assert out1 == out2 == '[{"id": "abc"}]'
    assert get_counter[0] == 1  # only the first call hit HTTP


@pytest.mark.asyncio
async def test_list_tickets_cache_miss_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After cache_ttl seconds, a fresh HTTP call must be made."""
    get_counter: list[int] = [0]
    resp = _MockResponse(text="[]")
    _install_mock_client(monkeypatch, resp, counter=get_counter)

    # Control monotonic time so we can advance past cache_ttl
    fake_time = [0.0]

    class _FakeMonotonic:
        def __call__(self) -> float:
            return fake_time[0]

    monkeypatch.setattr(time, "monotonic", _FakeMonotonic())

    client = BoardReader(_settings(cache_ttl=5.0))

    # First call fills cache at t=0
    await client.list_tickets(repo_id="robotsix-chat")
    assert get_counter[0] == 1

    # Advance past cache_ttl
    fake_time[0] = 10.0

    await client.list_tickets(repo_id="robotsix-chat")
    assert get_counter[0] == 2  # cache expired → second HTTP call


@pytest.mark.asyncio
async def test_list_tickets_errors_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error responses must not be stored; each call hits HTTP."""
    get_counter: list[int] = [0]
    # 500 error → response text starts with "Board API error"
    resp = _MockResponse(
        text="Board API error 500 for GET /tickets: boom", status_code=500
    )
    _install_mock_client(monkeypatch, resp, counter=get_counter)

    client = BoardReader(_settings(cache_ttl=60.0))
    out1 = await client.list_tickets(repo_id="x")
    out2 = await client.list_tickets(repo_id="x")

    assert "500" in out1
    assert "500" in out2
    assert get_counter[0] == 2  # errors not cached → both calls hit HTTP


@pytest.mark.asyncio
async def test_create_ticket_invalidates_list_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_ticket must clear _list_cache, forcing a list refetch."""
    get_counter: list[int] = [0]
    post_counter: list[int] = [0]

    list_resp = _MockResponse(text='[{"id": "abc"}]')
    create_resp = _MockResponse(text='{"id": "xyz"}', status_code=201)

    captured: dict[str, Any] = {}

    class _BothClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _BothClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str] | None = None,
        ) -> _MockResponse:
            captured["url"] = url
            captured["headers"] = headers
            captured.setdefault("params_list", []).append(params)
            get_counter[0] += 1
            return list_resp

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, str] | None = None,
        ) -> _MockResponse:
            captured["post_url"] = url
            captured["post_headers"] = headers
            captured["post_json"] = json
            post_counter[0] += 1
            return create_resp

    monkeypatch.setattr(httpx, "AsyncClient", _BothClient)

    client = BoardReader(_settings(cache_ttl=60.0))

    # 1) Populate list cache
    await client.list_tickets(repo_id="robotsix-chat")
    assert get_counter[0] == 1

    # 2) Create a ticket → invalidates list cache
    await client.create_ticket(title="T", description="D", repo_id="robotsix-chat")
    assert post_counter[0] == 1

    # 3) List again → must refetch (cache was cleared by create)
    await client.list_tickets(repo_id="robotsix-chat")
    assert get_counter[0] == 2  # second list HTTP call
    assert post_counter[0] == 1  # still exactly one POST


@pytest.mark.asyncio
async def test_get_ticket_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second get_ticket for the same id must hit cache (0 extra HTTP)."""
    get_counter: list[int] = [0]
    resp = _MockResponse(text='{"id": "abc", "title": "Fix bug"}')
    _install_mock_client(monkeypatch, resp, counter=get_counter)

    client = BoardReader(_settings(cache_ttl=60.0))
    out1 = await client.get_ticket("abc")
    out2 = await client.get_ticket("abc")

    assert out1 == out2 == '{"id": "abc", "title": "Fix bug"}'
    assert get_counter[0] == 1  # only the first call hit HTTP


# ---------------------------------------------------------------------------
# dedup helpers — unit tests
# ---------------------------------------------------------------------------


from robotsix_chat.board_reader.dedup import (  # noqa: E402 — deliberately after mock helpers
    SIMILARITY_THRESHOLD,
    find_duplicate_candidates,
    normalize_title,
    title_similarity,
)


def test_normalize_title_lowercase() -> None:
    """Lowercase the title."""
    assert normalize_title("Add Image Support") == "add image support"


def test_normalize_title_strip_punctuation() -> None:
    """Strip punctuation characters."""
    result = normalize_title("Hello, world! How's it going?")
    assert result == "hello world how s it going"


def test_normalize_title_collapse_whitespace() -> None:
    """Collapse multiple whitespace into single spaces."""
    assert normalize_title("  too   many   spaces  ") == "too many spaces"


def test_normalize_title_empty_string() -> None:
    """Empty string returns empty string."""
    assert normalize_title("") == ""


def test_normalize_title_only_punctuation() -> None:
    """All-punctuation string returns empty string."""
    assert normalize_title("!!! ??? ...") == ""


def test_title_similarity_identical() -> None:
    """Identical titles have similarity 1.0."""
    assert title_similarity("Add image support", "Add image support") == 1.0


def test_title_similarity_punctuation_insensitive() -> None:
    """Punctuation differences do not affect similarity."""
    assert title_similarity("Add image support", "Add image support!!!") == 1.0


def test_title_similarity_case_insensitive() -> None:
    """Case differences do not affect similarity."""
    assert title_similarity("ADD IMAGE SUPPORT", "add image support") == 1.0


def test_title_similarity_no_overlap() -> None:
    """Completely different titles have similarity 0.0."""
    assert title_similarity("Fix login bug", "Add image support") == 0.0


def test_title_similarity_partial_overlap() -> None:
    """Partially overlapping token sets yield a fractional score."""
    # "image support" vs "support images"
    # tokens: {image, support} vs {support, images}
    # intersection=1, union=3 → 0.333...
    sim = title_similarity("image support", "support images")
    assert 0.3 < sim < 0.4


def test_title_similarity_empty_a() -> None:
    """Similarity is 0.0 when first title is empty."""
    assert title_similarity("", "something") == 0.0


def test_title_similarity_empty_b() -> None:
    """Similarity is 0.0 when second title is empty."""
    assert title_similarity("something", "") == 0.0


def test_title_similarity_both_punctuation_only() -> None:
    """Similarity is 0.0 when both titles are pure punctuation."""
    assert title_similarity("!!!", "???") == 0.0


def test_find_dup_equal_title_match() -> None:
    """Exact title match returns the candidate."""
    tickets = [
        {"id": "abc", "title": "Add image support", "state": "ready"},
        {"id": "xyz", "title": "Fix login bug", "state": "draft"},
    ]
    result = find_duplicate_candidates("Add image support", tickets)
    assert len(result) == 1
    assert result[0]["id"] == "abc"


def test_find_dup_normalized_equal_match() -> None:
    """Normalization-equal titles (punctuation difference) still match."""
    tickets = [
        {"id": "abc", "title": "Add image support!!!", "state": "ready"},
    ]
    result = find_duplicate_candidates("add image support", tickets)
    assert len(result) == 1
    assert result[0]["id"] == "abc"


def test_find_dup_substring_match() -> None:
    """When new title is a normalized substring of an existing title."""
    tickets = [
        {"id": "abc", "title": "Add image support to the chat", "state": "ready"},
    ]
    result = find_duplicate_candidates("image support", tickets)
    assert len(result) == 1
    assert result[0]["id"] == "abc"


def test_find_dup_reverse_substring_match() -> None:
    """When existing title is a normalized substring of the new title."""
    tickets = [
        {"id": "abc", "title": "image support", "state": "ready"},
    ]
    result = find_duplicate_candidates("Add image support to the chat", tickets)
    assert len(result) == 1
    assert result[0]["id"] == "abc"


def test_find_dup_above_threshold() -> None:
    """Token overlap at or above SIMILARITY_THRESHOLD returns a candidate."""
    # "image support for chat" vs "add image support in chat"
    # normalized: "image support for chat" vs "add image support in chat"
    # tokens: {image,support,for,chat} vs {add,image,support,in,chat}
    # intersection=3, union=6 → 0.5  → at threshold (>=) should match
    tickets = [
        {"id": "abc", "title": "add image support in chat", "state": "ready"},
    ]
    result = find_duplicate_candidates("image support for chat", tickets)
    assert len(result) == 1
    assert result[0]["id"] == "abc"


def test_find_dup_below_threshold() -> None:
    """Token overlap below threshold returns no candidates."""
    tickets = [
        {"id": "abc", "title": "fix login bug", "state": "ready"},
    ]
    result = find_duplicate_candidates("image support", tickets)
    assert len(result) == 0


def test_find_dup_custom_threshold() -> None:
    """Custom threshold changes what is flagged."""
    tickets = [
        {"id": "abc", "title": "image support", "state": "ready"},
    ]
    # "image support" vs "support images": sim ~0.333
    result_strict = find_duplicate_candidates("support images", tickets, threshold=0.5)
    assert len(result_strict) == 0
    result_loose = find_duplicate_candidates("support images", tickets, threshold=0.3)
    assert len(result_loose) == 1


def test_find_dup_skips_missing_title() -> None:
    """Tickets with missing or empty title are skipped."""
    tickets = [
        {"id": "abc", "state": "ready"},
        {"id": "xyz", "title": "", "state": "draft"},
    ]
    result = find_duplicate_candidates("image support", tickets)
    assert len(result) == 0


def test_find_dup_skips_non_string_title() -> None:
    """Tickets with non-string title are skipped."""
    tickets = [
        {"id": "abc", "title": 123, "state": "ready"},
    ]
    result = find_duplicate_candidates("image support", tickets)
    assert len(result) == 0


def test_find_dup_sorted_by_descending_similarity() -> None:
    """Candidates are sorted descending by similarity score."""
    tickets = [
        {"id": "a", "title": "something else entirely", "state": "draft"},
        {"id": "b", "title": "image support", "state": "ready"},
        {"id": "c", "title": "add image support to chat", "state": "draft"},
    ]
    result = find_duplicate_candidates("image support", tickets)
    # "image support" exact match (score 1.0)
    # "add image support to chat" substring match (score 1.0)
    # Both score 1.0, stable sort → b first, then c
    assert len(result) == 2
    assert result[0]["id"] == "b"
    assert result[1]["id"] == "c"


def test_find_dup_empty_new_title() -> None:
    """Empty new title returns no candidates."""
    tickets = [{"id": "abc", "title": "image support", "state": "ready"}]
    result = find_duplicate_candidates("", tickets)
    assert len(result) == 0


def test_find_dup_empty_tickets() -> None:
    """Empty ticket list returns no candidates."""
    result = find_duplicate_candidates("image support", [])
    assert len(result) == 0


def test_similarity_threshold_constant() -> None:
    """SIMILARITY_THRESHOLD is 0.5 float."""
    assert isinstance(SIMILARITY_THRESHOLD, float)
    assert SIMILARITY_THRESHOLD == 0.5


# ---------------------------------------------------------------------------
# create_board_ticket — duplicate guard (tool-level)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ticket_blocks_on_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Near-duplicate open ticket blocks POST and returns a warning."""
    get_resp = _MockResponse(
        text='[{"id": "existing-1", "title": "Add image support", "state": "ready"}]'
    )
    post_resp = _MockResponse(text="SHOULD NOT BE CALLED", status_code=201)

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]
    captured = _install_mock_dual_client(monkeypatch, get_resp, post_resp)

    out = await create_fn(
        title="image support",
        description="We need image support",
        repo_id="robotsix-chat",
    )

    # Must NOT have POSTed.
    assert "url" not in captured["post"]
    # Warning must be present.
    assert "⚠️" in out
    assert "existing-1" in out
    assert "read_board_ticket" in out
    assert "force=True" in out


@pytest.mark.asyncio
async def test_create_ticket_proceeds_when_no_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With force=False and no similar open tickets, the tool POSTs normally."""
    get_resp = _MockResponse(
        text='[{"id": "abc", "title": "Fix login bug", "state": "ready"}]'
    )
    post_resp = _MockResponse(
        text='{"id": "new-1", "title": "Support images", "state": "draft"}',
        status_code=201,
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]
    captured = _install_mock_dual_client(monkeypatch, get_resp, post_resp)

    out = await create_fn(
        title="Support images",
        description="We need image support",
        repo_id="robotsix-chat",
    )

    # POST must have been called.
    assert captured["post"]["url"] == "http://127.0.0.1:8077/tickets"
    assert out == post_resp.text


@pytest.mark.asyncio
async def test_create_ticket_force_bypasses_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With force=True, the dedup check is skipped and the tool POSTs immediately."""
    get_resp = _MockResponse(
        text='[{"id": "existing-1", "title": "Add image support"}]'
    )
    post_resp = _MockResponse(
        text='{"id": "new-2", "title": "Support images", "state": "draft"}',
        status_code=201,
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]
    captured = _install_mock_dual_client(monkeypatch, get_resp, post_resp)

    out = await create_fn(
        title="Support images",
        description="We need image support",
        repo_id="robotsix-chat",
        force=True,
    )

    # GET should NOT have been called (we never listed tickets).
    assert "url" not in captured["get"]
    # POST must have been called.
    assert captured["post"]["url"] == "http://127.0.0.1:8077/tickets"
    assert out == post_resp.text


@pytest.mark.asyncio
async def test_create_ticket_fail_open_on_non_json_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Board-read error (diagnostic string) → fail-open POST."""
    get_resp = _MockResponse(text="Board API request timed out after 5.0s: ...")
    post_resp = _MockResponse(
        text='{"id": "new-3", "title": "Support images", "state": "draft"}',
        status_code=201,
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]
    captured = _install_mock_dual_client(monkeypatch, get_resp, post_resp)

    out = await create_fn(
        title="Support images",
        description="We need image support",
        repo_id="robotsix-chat",
    )

    # GET was called, POST also was called (fail-open).
    assert captured["get"]["url"] == "http://127.0.0.1:8077/tickets"
    assert captured["post"]["url"] == "http://127.0.0.1:8077/tickets"
    assert out == post_resp.text


@pytest.mark.asyncio
async def test_create_ticket_fail_open_on_json_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Board-read returns a JSON object (not list) → fail-open POST."""
    get_resp = _MockResponse(text='{"error": "something went wrong"}')
    post_resp = _MockResponse(
        text='{"id": "new-4", "title": "Fix bug", "state": "draft"}',
        status_code=201,
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]
    captured = _install_mock_dual_client(monkeypatch, get_resp, post_resp)

    out = await create_fn(
        title="Fix bug",
        description="Something broke",
        repo_id="robotsix-chat",
    )

    assert captured["post"]["url"] == "http://127.0.0.1:8077/tickets"
    assert out == post_resp.text


@pytest.mark.asyncio
async def test_create_ticket_duplicate_listing_includes_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate warning includes state for each candidate."""
    get_resp = _MockResponse(
        text='[{"id": "dup-1", "title": "Add image support", "state": "in_progress"}]'
    )
    post_resp = _MockResponse(text="SHOULD NOT BE CALLED", status_code=201)

    tools = build_board_reader_tools(_settings())
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]
    _install_mock_dual_client(monkeypatch, get_resp, post_resp)

    out = await create_fn(
        title="Add image support",
        description="Need images",
        repo_id="robotsix-chat",
    )

    assert "in_progress" in out
    assert "dup-1" in out

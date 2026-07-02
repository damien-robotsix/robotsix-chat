"""Tests for the board-reader integration.

:func:`build_board_reader_tools` and :class:`BoardReader`, with ``respx``
mocked so there are no real network calls.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.board_reader import board_was_read, build_board_reader_tools
from robotsix_chat.board_reader.client import BoardReader
from robotsix_chat.config import BoardReaderSettings


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
# board_was_read context var — hallucination guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_board_was_read_set_by_list_tickets(
    respx_mock: respx.MockRouter,
) -> None:
    """Calling list_board_tickets sets board_was_read to True."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, text='[{"id": "abc"}]')
    )

    tools = build_board_reader_tools(_settings())
    list_fn = [t for t in tools if t.__name__ == "list_board_tickets"][0]

    # Reset before the call so we observe the effect of THIS invocation.
    board_was_read.set(False)
    await list_fn(repo_id="robotsix-chat")

    assert board_was_read.get() is True


@pytest.mark.asyncio
async def test_board_was_read_set_by_read_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Calling read_board_ticket sets board_was_read to True."""
    respx_mock.get("http://127.0.0.1:8077/tickets/abc").mock(
        return_value=httpx.Response(200, text='{"id": "abc", "title": "Fix bug"}')
    )

    tools = build_board_reader_tools(_settings())
    read_fn = [t for t in tools if t.__name__ == "read_board_ticket"][0]

    board_was_read.set(False)
    await read_fn(ticket_id="abc")

    assert board_was_read.get() is True


@pytest.mark.asyncio
async def test_board_was_read_set_by_create_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """Calling create_board_ticket sets board_was_read to True."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, text="[]")
    )
    respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201, text='{"id": "new-1", "title": "A task", "state": "draft"}'
        )
    )

    tools = build_board_reader_tools(_settings())
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]

    board_was_read.set(False)
    await create_fn(title="A task", description="Desc", repo_id="robotsix-chat")

    assert board_was_read.get() is True


# ---------------------------------------------------------------------------
# BoardReader.list_tickets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tickets_calls_get_tickets(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that list_tickets calls GET /tickets with correct params."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, text='[{"id": "abc", "title": "Fix bug"}]')
    )

    client = BoardReader(_settings(api_base_url="http://127.0.0.1:8077"))
    out = await client.list_tickets(repo_id="robotsix-chat")

    assert out == '[{"id": "abc", "title": "Fix bug"}]'
    assert route.called
    assert dict(route.calls.last.request.url.params) == {"repo_id": "robotsix-chat"}


@pytest.mark.asyncio
async def test_list_tickets_includes_closed_and_state(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that include_closed and state are forwarded as query params."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, text="[]")
    )

    client = BoardReader(_settings())
    await client.list_tickets(
        repo_id="robotsix-mill",
        include_closed=True,
        state="ready",
    )

    assert dict(route.calls.last.request.url.params) == {
        "repo_id": "robotsix-mill",
        "include_closed": "true",
        "state": "ready",
    }


# ---------------------------------------------------------------------------
# BoardReader.get_ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticket_calls_get_tickets_by_id(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that get_ticket calls GET /tickets/{id}."""
    route = respx_mock.get("http://localhost:8077/tickets/abc").mock(
        return_value=httpx.Response(
            200, text='{"id": "abc", "title": "Fix bug", "state": "ready"}'
        )
    )

    client = BoardReader(_settings(api_base_url="http://localhost:8077"))
    out = await client.get_ticket("abc")

    assert out == '{"id": "abc", "title": "Fix bug", "state": "ready"}'
    assert route.called


# ---------------------------------------------------------------------------
# BoardReader auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_token_sent_when_configured(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that the Authorization header is set when api_token is given."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, text="[]")
    )

    client = BoardReader(_settings(api_token="secret-token"))
    await client.list_tickets(repo_id="robotsix-chat")

    assert route.called
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-token"
    assert route.calls.last.request.headers["accept"] == "application/json"


@pytest.mark.asyncio
async def test_no_auth_header_when_token_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that no Authorization header is sent when api_token is empty."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, text="[]")
    )

    client = BoardReader(_settings(api_token=""))
    await client.list_tickets(repo_id="robotsix-chat")

    assert "authorization" not in route.calls.last.request.headers


# ---------------------------------------------------------------------------
# BoardReader error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_returns_diagnostic(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that HTTP errors become a text message, never raised."""
    respx_mock.get("http://127.0.0.1:8077/tickets/nonexistent").mock(
        return_value=httpx.Response(404, text="not found")
    )

    client = BoardReader(_settings())
    out = await client.get_ticket("nonexistent")

    assert "404" in out
    assert "not found" in out


@pytest.mark.asyncio
async def test_timeout_returns_diagnostic(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that timeouts become a text message, never raised."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    client = BoardReader(_settings(timeout=5.0))
    out = await client.list_tickets(repo_id="x")

    assert "timed out" in out
    assert "5.0s" in out


@pytest.mark.asyncio
async def test_unexpected_error_returns_diagnostic(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that unexpected errors become a text message, never raised."""
    respx_mock.get("http://127.0.0.1:8077/tickets/abc").mock(
        side_effect=RuntimeError("something crashed")
    )

    client = BoardReader(_settings())
    out = await client.get_ticket("abc")

    assert "failed" in out.lower()
    assert "something crashed" in out


# ---------------------------------------------------------------------------
# BoardReader.create_ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ticket_calls_post_tickets(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that create_ticket calls POST /tickets with correct payload."""
    route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201, text='{"id": "abc", "title": "New ticket", "state": "draft"}'
        )
    )

    client = BoardReader(_settings(api_base_url="http://127.0.0.1:8077"))
    out = await client.create_ticket(
        title="New ticket",
        description="A test ticket",
        repo_id="robotsix-chat",
    )

    assert out == '{"id": "abc", "title": "New ticket", "state": "draft"}'
    assert route.called
    # Check the JSON payload
    import json as _json

    assert _json.loads(route.calls.last.request.content) == {
        "title": "New ticket",
        "description": "A test ticket",
        "repo_id": "robotsix-chat",
    }


@pytest.mark.asyncio
async def test_create_ticket_with_kind(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that kind is included in payload when provided."""
    route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(201, text='{"id": "xyz", "kind": "bug"}')
    )

    client = BoardReader(_settings())
    await client.create_ticket(
        title="Bug report",
        description="Something broke",
        repo_id="robotsix-mill",
        kind="bug",
    )

    import json as _json

    assert _json.loads(route.calls.last.request.content) == {
        "title": "Bug report",
        "description": "Something broke",
        "repo_id": "robotsix-mill",
        "kind": "bug",
    }


@pytest.mark.asyncio
async def test_create_ticket_omits_kind_when_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that kind is omitted from payload when empty string."""
    route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(201, text='{"id": "abc"}')
    )

    client = BoardReader(_settings())
    await client.create_ticket(
        title="T",
        description="D",
        repo_id="r",
        kind="",
    )

    import json as _json

    assert "kind" not in _json.loads(route.calls.last.request.content)


@pytest.mark.asyncio
async def test_create_ticket_http_error_returns_diagnostic(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that create_ticket HTTP errors become text, never raised."""
    respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(409, text="conflict")
    )

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
    respx_mock: respx.MockRouter,
) -> None:
    """Second call to list_tickets with same params must hit cache (0 extra HTTP)."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, text='[{"id": "abc"}]')
    )

    client = BoardReader(_settings(cache_ttl=60.0))
    out1 = await client.list_tickets(repo_id="robotsix-chat")
    out2 = await client.list_tickets(repo_id="robotsix-chat")

    assert out1 == out2 == '[{"id": "abc"}]'
    assert route.call_count == 1  # only the first call hit HTTP


@pytest.mark.asyncio
async def test_list_tickets_cache_miss_after_expiry(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After cache_ttl seconds, a fresh HTTP call must be made."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, text="[]")
    )

    # Control monotonic time so we can advance past cache_ttl
    fake_time = [0.0]

    class _FakeMonotonic:
        def __call__(self) -> float:
            return fake_time[0]

    monkeypatch.setattr(time, "monotonic", _FakeMonotonic())

    client = BoardReader(_settings(cache_ttl=5.0))

    # First call fills cache at t=0
    await client.list_tickets(repo_id="robotsix-chat")
    assert route.call_count == 1

    # Advance past cache_ttl
    fake_time[0] = 10.0

    await client.list_tickets(repo_id="robotsix-chat")
    assert route.call_count == 2  # cache expired → second HTTP call


@pytest.mark.asyncio
async def test_list_tickets_errors_not_cached(
    respx_mock: respx.MockRouter,
) -> None:
    """Error responses must not be stored; each call hits HTTP."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            500, text="Board API error 500 for GET /tickets: boom"
        )
    )

    client = BoardReader(_settings(cache_ttl=60.0))
    out1 = await client.list_tickets(repo_id="x")
    out2 = await client.list_tickets(repo_id="x")

    assert "500" in out1
    assert "500" in out2
    assert route.call_count == 2  # errors not cached → both calls hit HTTP


@pytest.mark.asyncio
async def test_create_ticket_invalidates_list_cache(
    respx_mock: respx.MockRouter,
) -> None:
    """create_ticket must clear _list_cache, forcing a list refetch."""
    get_route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, text='[{"id": "abc"}]')
    )
    post_route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(201, text='{"id": "xyz"}')
    )

    client = BoardReader(_settings(cache_ttl=60.0))

    # 1) Populate list cache
    await client.list_tickets(repo_id="robotsix-chat")
    assert get_route.call_count == 1

    # 2) Create a ticket → invalidates list cache
    await client.create_ticket(title="T", description="D", repo_id="robotsix-chat")
    assert post_route.call_count == 1

    # 3) List again → must refetch (cache was cleared by create)
    await client.list_tickets(repo_id="robotsix-chat")
    assert get_route.call_count == 2  # second list HTTP call
    assert post_route.call_count == 1  # still exactly one POST


@pytest.mark.asyncio
async def test_get_ticket_cache_hit(
    respx_mock: respx.MockRouter,
) -> None:
    """Second get_ticket for the same id must hit cache (0 extra HTTP)."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets/abc").mock(
        return_value=httpx.Response(200, text='{"id": "abc", "title": "Fix bug"}')
    )

    client = BoardReader(_settings(cache_ttl=60.0))
    out1 = await client.get_ticket("abc")
    out2 = await client.get_ticket("abc")

    assert out1 == out2 == '{"id": "abc", "title": "Fix bug"}'
    assert route.call_count == 1  # only the first call hit HTTP


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
    respx_mock: respx.MockRouter,
) -> None:
    """Near-duplicate open ticket blocks POST and returns a warning."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            text=(
                '[{"id": "existing-1", "title": "Add image support", "state": "ready"}]'
            ),
        )
    )
    post_route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(201, text="SHOULD NOT BE CALLED")
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]

    out = await create_fn(
        title="image support",
        description="We need image support",
        repo_id="robotsix-chat",
    )

    # Must NOT have POSTed.
    assert not post_route.called
    # Warning must be present.
    assert "⚠️" in out
    assert "existing-1" in out
    assert "read_board_ticket" in out
    assert "force=True" in out


@pytest.mark.asyncio
async def test_create_ticket_proceeds_when_no_duplicate(
    respx_mock: respx.MockRouter,
) -> None:
    """With force=False and no similar open tickets, the tool POSTs normally."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            text='[{"id": "abc", "title": "Fix login bug", "state": "ready"}]',
        )
    )
    post_route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201,
            text='{"id": "new-1", "title": "Support images", "state": "draft"}',
        )
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]

    out = await create_fn(
        title="Support images",
        description="We need image support",
        repo_id="robotsix-chat",
    )

    # POST must have been called.
    assert post_route.called
    assert str(post_route.calls.last.request.url) == "http://127.0.0.1:8077/tickets"
    assert out == post_route.calls.last.response.text


@pytest.mark.asyncio
async def test_create_ticket_force_bypasses_dedup(
    respx_mock: respx.MockRouter,
) -> None:
    """With force=True, the dedup check is skipped and the tool POSTs immediately."""
    get_route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            200, text='[{"id": "existing-1", "title": "Add image support"}]'
        )
    )
    post_route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201,
            text='{"id": "new-2", "title": "Support images", "state": "draft"}',
        )
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]

    out = await create_fn(
        title="Support images",
        description="We need image support",
        repo_id="robotsix-chat",
        force=True,
    )

    # GET should NOT have been called (we never listed tickets).
    assert not get_route.called
    # POST must have been called.
    assert post_route.called
    assert str(post_route.calls.last.request.url) == "http://127.0.0.1:8077/tickets"
    assert out == post_route.calls.last.response.text


@pytest.mark.asyncio
async def test_create_ticket_fail_open_on_non_json_list(
    respx_mock: respx.MockRouter,
) -> None:
    """Board-read error (diagnostic string) → fail-open POST."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            200, text="Board API request timed out after 5.0s: ..."
        )
    )
    post_route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201,
            text='{"id": "new-3", "title": "Support images", "state": "draft"}',
        )
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]

    out = await create_fn(
        title="Support images",
        description="We need image support",
        repo_id="robotsix-chat",
    )

    # GET was called, POST also was called (fail-open).
    assert post_route.called
    assert out == post_route.calls.last.response.text


@pytest.mark.asyncio
async def test_create_ticket_fail_open_on_json_object(
    respx_mock: respx.MockRouter,
) -> None:
    """Board-read returns a JSON object (not list) → fail-open POST."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, text='{"error": "something went wrong"}')
    )
    post_route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201,
            text='{"id": "new-4", "title": "Fix bug", "state": "draft"}',
        )
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]

    out = await create_fn(
        title="Fix bug",
        description="Something broke",
        repo_id="robotsix-chat",
    )

    assert post_route.called
    assert str(post_route.calls.last.request.url) == "http://127.0.0.1:8077/tickets"
    assert out == post_route.calls.last.response.text


@pytest.mark.asyncio
async def test_create_ticket_duplicate_listing_includes_state(
    respx_mock: respx.MockRouter,
) -> None:
    """The duplicate warning includes state for each candidate."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            text=(
                '[{"id": "dup-1", "title": "Add image support",'
                ' "state": "in_progress"}]'
            ),
        )
    )
    respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(201, text="SHOULD NOT BE CALLED")
    )

    tools = build_board_reader_tools(_settings())
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]

    out = await create_fn(
        title="Add image support",
        description="Need images",
        repo_id="robotsix-chat",
    )

    assert "in_progress" in out
    assert "dup-1" in out

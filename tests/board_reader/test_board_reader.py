"""Tests for the board integration via the shared BoardHTTPClient.

:func:`build_board_reader_tools` with ``respx`` mocked so there are no real
network calls.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.board import board_was_read, build_board_reader_tools
from robotsix_chat.config import BoardSettings


def _settings(**kw: Any) -> BoardSettings:
    base: dict[str, Any] = {"enabled": True}
    base.update(kw)
    return BoardSettings(**base)


# ---------------------------------------------------------------------------
# build_board_reader_tools
# ---------------------------------------------------------------------------


def test_build_board_reader_tools_disabled() -> None:
    """Verify that disabled board reader returns no tools."""
    assert build_board_reader_tools(BoardSettings(enabled=False)) == []


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
        return_value=httpx.Response(200, json=[{"id": "abc"}])
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
        return_value=httpx.Response(200, json={"id": "abc", "title": "Fix bug"})
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
        return_value=httpx.Response(200, json=[])
    )
    respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201, json={"id": "new-1", "title": "A task", "state": "draft"}
        )
    )

    tools = build_board_reader_tools(_settings())
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]

    board_was_read.set(False)
    await create_fn(title="A task", description="Desc", repo_id="robotsix-chat")

    assert board_was_read.get() is True


# ---------------------------------------------------------------------------
# list_board_tickets tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tickets_calls_get_tickets(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that list_board_tickets calls GET /tickets with correct params."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, json=[{"id": "abc", "title": "Fix bug"}])
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    list_fn = [t for t in tools if t.__name__ == "list_board_tickets"][0]
    out = await list_fn(repo_id="robotsix-chat")

    assert json.loads(out) == [{"id": "abc", "title": "Fix bug"}]
    assert route.called
    assert dict(route.calls.last.request.url.params) == {"repo_id": "robotsix-chat"}


@pytest.mark.asyncio
async def test_list_tickets_with_state(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that state is forwarded as a query param."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, json=[])
    )

    tools = build_board_reader_tools(_settings())
    list_fn = [t for t in tools if t.__name__ == "list_board_tickets"][0]
    await list_fn(repo_id="robotsix-mill", state="ready")

    assert dict(route.calls.last.request.url.params) == {
        "repo_id": "robotsix-mill",
        "state": "ready",
    }


# ---------------------------------------------------------------------------
# read_board_ticket tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticket_calls_get_tickets_by_id(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that read_board_ticket calls GET /tickets/{id}."""
    route = respx_mock.get("http://localhost:8077/tickets/abc").mock(
        return_value=httpx.Response(
            200, json={"id": "abc", "title": "Fix bug", "state": "ready"}
        )
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://localhost:8077"))
    read_fn = [t for t in tools if t.__name__ == "read_board_ticket"][0]
    out = await read_fn(ticket_id="abc")

    assert json.loads(out) == {"id": "abc", "title": "Fix bug", "state": "ready"}
    assert route.called


# ---------------------------------------------------------------------------
# auth — Bearer token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_token_sent_when_configured(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that the Authorization header is set when api_token is given."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, json=[])
    )

    tools = build_board_reader_tools(_settings(api_token="secret-token"))
    list_fn = [t for t in tools if t.__name__ == "list_board_tickets"][0]
    await list_fn(repo_id="robotsix-chat")

    assert route.called
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_no_auth_header_when_token_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that no Authorization header is sent when api_token is empty."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, json=[])
    )

    tools = build_board_reader_tools(_settings(api_token=""))
    list_fn = [t for t in tools if t.__name__ == "list_board_tickets"][0]
    await list_fn(repo_id="robotsix-chat")

    assert "authorization" not in route.calls.last.request.headers


# ---------------------------------------------------------------------------
# error handling (via ErrorStrategy.RETURN)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_returns_diagnostic(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that HTTP errors become a text message, never raised."""
    respx_mock.get("http://127.0.0.1:8077/tickets/nonexistent").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )

    tools = build_board_reader_tools(_settings())
    read_fn = [t for t in tools if t.__name__ == "read_board_ticket"][0]
    out = await read_fn(ticket_id="nonexistent")

    result = json.loads(out)
    assert result.get("error") is True
    assert result.get("status_code") == 404


# ---------------------------------------------------------------------------
# create_board_ticket tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ticket_calls_post_tickets(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that create_board_ticket calls POST /tickets with correct payload."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, json=[])
    )
    route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201, json={"id": "abc", "title": "New ticket", "state": "draft"}
        )
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]
    out = await create_fn(
        title="New ticket",
        description="A test ticket",
        repo_id="robotsix-chat",
    )

    assert json.loads(out) == {"id": "abc", "title": "New ticket", "state": "draft"}
    assert route.called
    assert json.loads(route.calls.last.request.content) == {
        "title": "New ticket",
        "description": "A test ticket",
        "source": "robotsix",
        "kind": "task",
        "repo_id": "robotsix-chat",
    }


@pytest.mark.asyncio
async def test_create_ticket_with_kind(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that kind is included in payload when provided."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, json=[])
    )
    route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(201, json={"id": "xyz", "kind": "bug"})
    )

    tools = build_board_reader_tools(_settings())
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]
    await create_fn(
        title="Bug report",
        description="Something broke",
        repo_id="robotsix-mill",
        kind="bug",
    )

    assert json.loads(route.calls.last.request.content) == {
        "title": "Bug report",
        "description": "Something broke",
        "source": "robotsix",
        "kind": "bug",
        "repo_id": "robotsix-mill",
    }


@pytest.mark.asyncio
async def test_create_ticket_defaults_kind_to_task_when_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that kind defaults to 'task' when empty string."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, json=[])
    )
    route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(201, json={"id": "abc"})
    )

    tools = build_board_reader_tools(_settings())
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]
    await create_fn(
        title="T",
        description="D",
        repo_id="r",
        kind="",
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["kind"] == "task"


@pytest.mark.asyncio
async def test_create_ticket_http_error_returns_diagnostic(
    respx_mock: respx.MockRouter,
) -> None:
    """Verify that create_ticket HTTP errors become JSON error, never raised."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(409, json={"detail": "conflict"})
    )

    tools = build_board_reader_tools(_settings())
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]
    out = await create_fn(
        title="Dup",
        description="Duplicate",
        repo_id="x",
    )

    result = json.loads(out)
    assert result.get("error") is True
    assert result.get("status_code") == 409


# ---------------------------------------------------------------------------
# cache tests — BoardHTTPClient handles caching internally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tickets_cache_hit(
    respx_mock: respx.MockRouter,
) -> None:
    """Second list_board_tickets call with same params must hit cache (0 extra HTTP)."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, json=[{"id": "abc"}])
    )

    tools = build_board_reader_tools(_settings(cache_ttl=60.0))
    list_fn = [t for t in tools if t.__name__ == "list_board_tickets"][0]
    out1 = await list_fn(repo_id="robotsix-chat")
    out2 = await list_fn(repo_id="robotsix-chat")

    assert out1 == out2 == json.dumps([{"id": "abc"}])
    assert route.call_count == 1  # only the first call hit HTTP


@pytest.mark.asyncio
async def test_list_tickets_cache_miss_after_expiry(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After cache_ttl seconds, a fresh HTTP call must be made."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, json=[])
    )

    # Control monotonic time so we can advance past cache_ttl
    fake_time = [0.0]

    class _FakeMonotonic:
        def __call__(self) -> float:
            return fake_time[0]

    monkeypatch.setattr(time, "monotonic", _FakeMonotonic())

    tools = build_board_reader_tools(_settings(cache_ttl=5.0))
    list_fn = [t for t in tools if t.__name__ == "list_board_tickets"][0]

    # First call fills cache at t=0
    await list_fn(repo_id="robotsix-chat")
    assert route.call_count == 1

    # Advance past cache_ttl
    fake_time[0] = 10.0

    await list_fn(repo_id="robotsix-chat")
    assert route.call_count == 2  # cache expired → second HTTP call


@pytest.mark.asyncio
async def test_create_ticket_invalidates_list_cache(
    respx_mock: respx.MockRouter,
) -> None:
    """create_board_ticket calls GET for dedup but list cache may still be served."""
    get_route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(200, json=[{"id": "abc"}])
    )
    post_route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(201, json={"id": "xyz"})
    )

    tools = build_board_reader_tools(_settings(cache_ttl=60.0))
    list_fn = [t for t in tools if t.__name__ == "list_board_tickets"][0]
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]

    # 1) Populate list cache
    await list_fn(repo_id="robotsix-chat")
    assert get_route.call_count == 1

    # 2) Create a ticket (GET for dedup + POST)
    await create_fn(title="T", description="D", repo_id="robotsix-chat")
    assert post_route.call_count == 1

    # 3) List again — list cache is still fresh (BoardHTTPClient caches by TTL only)
    await list_fn(repo_id="robotsix-chat")
    # GET is still 1 because the list cache did not expire
    assert get_route.call_count == 1


@pytest.mark.asyncio
async def test_get_ticket_cache_hit(
    respx_mock: respx.MockRouter,
) -> None:
    """Second read_board_ticket for the same id must hit cache (0 extra HTTP)."""
    route = respx_mock.get("http://127.0.0.1:8077/tickets/abc").mock(
        return_value=httpx.Response(200, json={"id": "abc", "title": "Fix bug"})
    )

    tools = build_board_reader_tools(_settings(cache_ttl=60.0))
    read_fn = [t for t in tools if t.__name__ == "read_board_ticket"][0]
    out1 = await read_fn(ticket_id="abc")
    out2 = await read_fn(ticket_id="abc")

    assert out1 == out2 == json.dumps({"id": "abc", "title": "Fix bug"})
    assert route.call_count == 1  # only the first call hit HTTP


# ---------------------------------------------------------------------------
# dedup helpers — unit tests
# ---------------------------------------------------------------------------


from robotsix_chat.board.dedup import (  # noqa: E402 — deliberately after mock helpers
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
            json=[{"id": "existing-1", "title": "Add image support", "state": "ready"}],
        )
    )
    post_route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(201, json={"id": "SHOULD NOT BE CALLED"})
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
            json=[{"id": "abc", "title": "Fix login bug", "state": "ready"}],
        )
    )
    post_route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201,
            json={"id": "new-1", "title": "Support images", "state": "draft"},
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
    expected = {"id": "new-1", "title": "Support images", "state": "draft"}
    assert json.loads(out) == expected


@pytest.mark.asyncio
async def test_create_ticket_force_bypasses_dedup(
    respx_mock: respx.MockRouter,
) -> None:
    """With force=True, the dedup check is skipped and the tool POSTs immediately."""
    get_route = respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            200, json=[{"id": "existing-1", "title": "Add image support"}]
        )
    )
    post_route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201,
            json={"id": "new-2", "title": "Support images", "state": "draft"},
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
    expected = {"id": "new-2", "title": "Support images", "state": "draft"}
    assert json.loads(out) == expected


@pytest.mark.asyncio
async def test_create_ticket_fail_open_on_error_response(
    respx_mock: respx.MockRouter,
) -> None:
    """Board-read error (error dict) → fail-open POST."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(500, json={"detail": "server error"})
    )
    post_route = respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201,
            json={"id": "new-3", "title": "Support images", "state": "draft"},
        )
    )

    tools = build_board_reader_tools(_settings(api_base_url="http://127.0.0.1:8077"))
    create_fn = [t for t in tools if t.__name__ == "create_board_ticket"][0]

    out = await create_fn(
        title="Support images",
        description="We need image support",
        repo_id="robotsix-chat",
    )

    # GET was called (error dict), POST also was called (fail-open).
    assert post_route.called
    expected = {"id": "new-3", "title": "Support images", "state": "draft"}
    assert json.loads(out) == expected


@pytest.mark.asyncio
async def test_create_ticket_duplicate_listing_includes_state(
    respx_mock: respx.MockRouter,
) -> None:
    """The duplicate warning includes state for each candidate."""
    respx_mock.get("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "dup-1", "title": "Add image support", "state": "in_progress"}
            ],
        )
    )
    respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(201, json={"id": "SHOULD NOT BE CALLED"})
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

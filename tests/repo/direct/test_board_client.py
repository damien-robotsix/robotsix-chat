"""Dedicated unit tests for :class:`BoardClient`.

Mock ``safe_http_request`` and the HTTP layer (via ``respx``) so there
are no real network calls.  Coverage targets: ``get_ticket_state``,
``resume_blocked_ticket``, ``get_ticket_data``, and
``count_implement_cycles``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings
from robotsix_chat.repo.direct.board_client import BoardClient

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


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


def _client(**kw: Any) -> BoardClient:
    return BoardClient(_settings(**kw))


# ---------------------------------------------------------------------------
# get_ticket_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticket_state_returns_state(respx_mock: respx.MockRouter) -> None:
    """Valid JSON with a ``state`` field returns the state string."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"id": "t-1", "state": "BLOCKED"})
        )
    )
    client = _client()
    state = await client.get_ticket_state("t-1")
    assert state == "BLOCKED"


@pytest.mark.asyncio
async def test_get_ticket_state_missing_field_returns_none(
    respx_mock: respx.MockRouter,
) -> None:
    """JSON without a ``state`` field returns None."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(200, text=json.dumps({"id": "t-1"}))
    )
    client = _client()
    state = await client.get_ticket_state("t-1")
    assert state is None


@pytest.mark.asyncio
async def test_get_ticket_state_non_json_response(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-JSON response body returns None."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(200, text="<html>Not JSON</html>")
    )
    client = _client()
    state = await client.get_ticket_state("t-1")
    assert state is None


@pytest.mark.asyncio
async def test_get_ticket_state_404_returns_none(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP 404 returns None."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(404, text="Not found")
    )
    client = _client()
    state = await client.get_ticket_state("t-1")
    assert state is None


@pytest.mark.asyncio
async def test_get_ticket_state_500_returns_none(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP 500 returns None (error logged but not raised)."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    client = _client()
    state = await client.get_ticket_state("t-1")
    assert state is None


# ---------------------------------------------------------------------------
# resume_blocked_ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_blocked_ticket_2xx_returns_true(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP 2xx → returns True."""
    respx_mock.post("http://127.0.0.1:8077/tickets/t-1/resume-blocked").mock(
        return_value=httpx.Response(200, text=json.dumps({"ok": True}))
    )
    client = _client()
    result = await client.resume_blocked_ticket("t-1", "Needs more cycles")
    assert result is True


@pytest.mark.asyncio
async def test_resume_blocked_ticket_400_returns_false(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP >=400 → returns False."""
    respx_mock.post("http://127.0.0.1:8077/tickets/t-1/resume-blocked").mock(
        return_value=httpx.Response(400, text=json.dumps({"error": "bad request"}))
    )
    client = _client()
    result = await client.resume_blocked_ticket("t-1", "Needs more cycles")
    assert result is False


@pytest.mark.asyncio
async def test_resume_blocked_ticket_500_returns_false(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP 500 → returns False."""
    respx_mock.post("http://127.0.0.1:8077/tickets/t-1/resume-blocked").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    client = _client()
    result = await client.resume_blocked_ticket("t-1", "Needs more cycles")
    assert result is False


@pytest.mark.asyncio
async def test_resume_blocked_ticket_connection_error_returns_false(
    respx_mock: respx.MockRouter,
) -> None:
    """Connection error (no route mocked → respx raises) → returns False."""
    # No route mocked — respx raises a connection error by default.
    client = _client()
    result = await client.resume_blocked_ticket("t-1", "Needs more cycles")
    assert result is False


# ---------------------------------------------------------------------------
# get_ticket_data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticket_data_returns_full_dict(
    respx_mock: respx.MockRouter,
) -> None:
    """Valid JSON returns the full parsed dict."""
    payload = {
        "id": "t-1",
        "state": "BLOCKED",
        "events": [{"type": "implement_start"}, {"type": "implement_complete"}],
    }
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(200, text=json.dumps(payload))
    )
    client = _client()
    data = await client.get_ticket_data("t-1")
    assert data == payload


@pytest.mark.asyncio
async def test_get_ticket_data_non_json_returns_none(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-JSON response returns None."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(200, text="not json")
    )
    client = _client()
    data = await client.get_ticket_data("t-1")
    assert data is None


@pytest.mark.asyncio
async def test_get_ticket_data_404_returns_none(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP 404 returns None."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(404, text="Not found")
    )
    client = _client()
    data = await client.get_ticket_data("t-1")
    assert data is None


# ---------------------------------------------------------------------------
# count_implement_cycles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_implement_cycles_from_events(
    respx_mock: respx.MockRouter,
) -> None:
    """Counts implement-type events from the events array."""
    payload = {
        "id": "t-1",
        "events": [
            {"type": "implement_start"},
            {"type": "implement_complete"},
            {"type": "review"},
        ],
    }
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(200, text=json.dumps(payload))
    )
    client = _client()
    cycles = await client.count_implement_cycles("t-1")
    assert cycles == 2  # two implement events, one review (not counted)


@pytest.mark.asyncio
async def test_count_implement_cycles_fallback_history(
    respx_mock: respx.MockRouter,
) -> None:
    """Falls back to history when no events array is present."""
    payload = {
        "id": "t-1",
        "history": [
            {"state": "implement_complete"},
            {"state": "implement_complete"},
            {"state": "review"},
        ],
    }
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(200, text=json.dumps(payload))
    )
    client = _client()
    cycles = await client.count_implement_cycles("t-1")
    assert cycles == 2  # two implement_complete, one review (not counted)


@pytest.mark.asyncio
async def test_count_implement_cycles_fallback_direct_field(
    respx_mock: respx.MockRouter,
) -> None:
    """Falls back to ``cycle_count`` field when no events or history present."""
    payload = {"id": "t-1", "cycle_count": 5}
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(200, text=json.dumps(payload))
    )
    client = _client()
    cycles = await client.count_implement_cycles("t-1")
    assert cycles == 5


@pytest.mark.asyncio
async def test_count_implement_cycles_no_data_returns_zero(
    respx_mock: respx.MockRouter,
) -> None:
    """Bare dict with no events/history/cycle_count returns 0."""
    payload: dict[str, Any] = {"id": "t-1"}
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(200, text=json.dumps(payload))
    )
    client = _client()
    cycles = await client.count_implement_cycles("t-1")
    assert cycles == 0


@pytest.mark.asyncio
async def test_count_implement_cycles_api_failure_returns_none(
    respx_mock: respx.MockRouter,
) -> None:
    """API failure (404) → get_ticket_data returns None → count returns None."""
    respx_mock.get("http://127.0.0.1:8077/tickets/t-1").mock(
        return_value=httpx.Response(404, text="Not found")
    )
    client = _client()
    cycles = await client.count_implement_cycles("t-1")
    assert cycles is None


# ---------------------------------------------------------------------------
# create_ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ticket_returns_id_on_201(
    respx_mock: respx.MockRouter,
) -> None:
    """Successful ticket creation returns the ticket id."""
    respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201,
            text=json.dumps(
                {"id": "new-ticket-1", "title": "Test ticket", "state": "draft"}
            ),
        )
    )
    client = _client()
    ticket_id = await client.create_ticket(
        title="Test ticket",
        description="A test follow-up",
    )
    assert ticket_id == "new-ticket-1"


@pytest.mark.asyncio
async def test_create_ticket_returns_none_on_400(
    respx_mock: respx.MockRouter,
) -> None:
    """HTTP 400 returns None."""
    respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(400, text="Bad request")
    )
    client = _client()
    ticket_id = await client.create_ticket(title="Bad ticket")
    assert ticket_id is None


@pytest.mark.asyncio
async def test_create_ticket_returns_none_on_non_json(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-JSON 201 response returns None."""
    respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(201, text="<html>OK</html>")
    )
    client = _client()
    ticket_id = await client.create_ticket(title="HTML response")
    assert ticket_id is None


@pytest.mark.asyncio
async def test_create_ticket_returns_none_when_missing_id(
    respx_mock: respx.MockRouter,
) -> None:
    """Valid JSON without an 'id' field returns None."""
    respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201,
            text=json.dumps({"title": "no id field"}),
        )
    )
    client = _client()
    ticket_id = await client.create_ticket(title="Missing id")
    assert ticket_id is None

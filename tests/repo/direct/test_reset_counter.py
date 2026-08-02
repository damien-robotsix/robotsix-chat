"""Tests for reset_implement_spawn_counter, get_ticket_data, count_implement_cycles."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.direct.client import (
    DirectRepoClient,
)

from .conftest import _settings

# ============================================================================
# reset_implement_spawn_counter
# ============================================================================


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_success(
    respx_mock: respx.MockRouter,
) -> None:
    """Successful POST resume-blocked → tool returns success message."""
    respx_mock.post("http://127.0.0.1:8077/tickets/t-reset/resume-blocked").mock(
        return_value=httpx.Response(200)
    )

    tools = build_direct_repo_tools(_settings())
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-reset")
    assert "reset" in out
    assert "t-reset" in out
    assert "Error" not in out


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_failure(
    respx_mock: respx.MockRouter,
) -> None:
    """Board API error → tool returns error message."""
    respx_mock.post("http://127.0.0.1:8077/tickets/t-bad/resume-blocked").mock(
        return_value=httpx.Response(500)
    )

    tools = build_direct_repo_tools(_settings())
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-bad")
    assert "Error" in out
    assert "t-bad" in out


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_roster_first_success() -> None:
    """When component_request is available, use roster path first."""

    async def _mock_cr(
        component: str, method: str, path: str, json_body: Any = None
    ) -> str:
        return "HTTP 200 OK"

    tools = build_direct_repo_tools(_settings(), component_request=_mock_cr)
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-roster-reset")
    assert "reset" in out
    assert "t-roster-reset" in out
    assert "roster path" in out


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_roster_fails_falls_back_to_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """When roster path fails, fall back to direct board API."""

    async def _mock_cr(
        component: str, method: str, path: str, json_body: Any = None
    ) -> str:
        return "Error: connection refused"

    respx_mock.post("http://127.0.0.1:8077/tickets/t-fb/resume-blocked").mock(
        return_value=httpx.Response(200)
    )

    tools = build_direct_repo_tools(_settings(), component_request=_mock_cr)
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-fb")
    assert "reset" in out
    assert "t-fb" in out
    assert "Error" not in out


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_roster_non_2xx_falls_back(
    respx_mock: respx.MockRouter,
) -> None:
    """When roster returns non-2xx, fall back to direct."""

    async def _mock_cr(
        component: str, method: str, path: str, json_body: Any = None
    ) -> str:
        return "HTTP 502 Bad Gateway"

    respx_mock.post("http://127.0.0.1:8077/tickets/t-502/resume-blocked").mock(
        return_value=httpx.Response(200)
    )

    tools = build_direct_repo_tools(_settings(), component_request=_mock_cr)
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-502")
    assert "reset" in out
    assert "t-502" in out
    assert "Error" not in out


@pytest.mark.asyncio
async def test_reset_implement_spawn_counter_no_component_request_uses_direct(
    respx_mock: respx.MockRouter,
) -> None:
    """Without component_request, uses direct path only (regression)."""
    respx_mock.post("http://127.0.0.1:8077/tickets/t-direct/resume-blocked").mock(
        return_value=httpx.Response(200)
    )

    tools = build_direct_repo_tools(_settings())  # component_request=None
    reset_fn = [t for t in tools if t.__name__ == "reset_implement_spawn_counter"][0]

    out = await reset_fn(ticket_id="t-direct")
    assert "reset" in out
    assert "t-direct" in out
    assert "Error" not in out


# ---------------------------------------------------------------------------
# get_ticket_data / count_implement_cycles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticket_data_returns_full_json(
    respx_mock: respx.MockRouter,
) -> None:
    """get_ticket_data returns the full ticket JSON."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-full").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-full",
                    "state": "blocked",
                    "title": "Fix stuff",
                    "events": [{"type": "implement_start"}],
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    data = await client.get_ticket_data("t-full")
    assert data is not None
    assert data["id"] == "t-full"
    assert data["state"] == "blocked"


@pytest.mark.asyncio
async def test_get_ticket_data_returns_none_on_error(
    respx_mock: respx.MockRouter,
) -> None:
    """get_ticket_data returns None when the board API errors."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-err2").mock(
        return_value=httpx.Response(500, text="boom")
    )

    client = DirectRepoClient(settings)
    data = await client.get_ticket_data("t-err2")
    assert data is None


@pytest.mark.asyncio
async def test_count_implement_cycles_from_events(
    respx_mock: respx.MockRouter,
) -> None:
    """count_implement_cycles counts events with 'implement' in type/action."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-cycles").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-cycles",
                    "state": "blocked",
                    "events": [
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "implement_start"},
                        {"type": "implement_complete"},
                        {"type": "review"},
                        {"type": "implement_start"},
                    ],
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    cycles = await client.count_implement_cycles("t-cycles")
    assert cycles == 5  # 3 starts + 2 completes


@pytest.mark.asyncio
async def test_count_implement_cycles_fallback_history(
    respx_mock: respx.MockRouter,
) -> None:
    """count_implement_cycles falls back to history when no events array."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-hist").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-hist",
                    "state": "blocked",
                    "history": [
                        {"state": "ready"},
                        {"state": "implement_complete"},
                        {"state": "in_progress"},
                        {"state": "implement_complete"},
                    ],
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    cycles = await client.count_implement_cycles("t-hist")
    assert cycles == 2


@pytest.mark.asyncio
async def test_count_implement_cycles_fallback_direct_field(
    respx_mock: respx.MockRouter,
) -> None:
    """count_implement_cycles falls back to cycle_count field."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-count").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "id": "t-count",
                    "state": "blocked",
                    "cycle_count": 5,
                }
            ),
        )
    )

    client = DirectRepoClient(settings)
    cycles = await client.count_implement_cycles("t-count")
    assert cycles == 5


@pytest.mark.asyncio
async def test_count_implement_cycles_no_data_returns_zero(
    respx_mock: respx.MockRouter,
) -> None:
    """count_implement_cycles returns 0 when no events/history/cycle_count."""
    settings = _settings()

    respx_mock.get("http://127.0.0.1:8077/tickets/t-nodata").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"id": "t-nodata", "state": "blocked"}),
        )
    )

    client = DirectRepoClient(settings)
    cycles = await client.count_implement_cycles("t-nodata")
    assert cycles == 0

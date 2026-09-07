"""``read_diagnostic_events`` must query the mill API, not a local file.

Until 2026-09-07 the tool read ``settings.mill_events_path`` — a path in
the MILL's data volume that chat never mounts — so it always answered
"No mill diagnostic events file found", and its schema lacked the
``board_id`` / ``category`` filters the real ``GET /diagnostic-events``
route takes (agents passing them got ``Input validation error``).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from robotsix_chat.config import DiagnosticsSettings, DirectRepoSettings
from robotsix_chat.diagnostics import build_diagnostics_tools


def _tool(tmp_path, direct_repo: DirectRepoSettings | None):
    settings = DiagnosticsSettings(
        store_path=str(tmp_path / "d.json"),
        proposals_path=str(tmp_path / "p.json"),
        effectiveness_path=str(tmp_path / "e.json"),
    )
    tools = build_diagnostics_tools(settings, direct_repo=direct_repo)
    return next(t for t in tools if t.__name__ == "read_diagnostic_events")


_DIRECT = DirectRepoSettings(
    board_api_base_url="http://board:8077/", board_api_token="tok", timeout=5.0
)

_EVENT = {
    "category": "CI_FIX_RESOLVED",
    "ticket_id": "20260905T073219Z-fix-aliens-river-de51",
    "repo_id": "hexarchy",
    "reason": "CI green after fix",
    "normalized_key": "fix aliens river",
    "timestamp": "2026-09-05T08:00:00+00:00",
}


@pytest.mark.asyncio
async def test_read_events_queries_mill_api_with_real_filters(
    respx_mock: respx.MockRouter, tmp_path
) -> None:
    route = respx_mock.get("http://board:8077/diagnostic-events").mock(
        return_value=httpx.Response(200, json={"events": [_EVENT]})
    )
    tool = _tool(tmp_path, _DIRECT)

    out = await tool(
        board_id="hexarchy",
        category="CI_FIX_RESOLVED",
        since="2026-09-04T00:00:00Z",
        limit=50,
    )

    assert route.called
    req = route.calls.last.request
    assert req.url.params["board_id"] == "hexarchy"
    assert req.url.params["category"] == "CI_FIX_RESOLVED"
    assert req.url.params["since"] == "2026-09-04T00:00:00Z"
    assert req.url.params["limit"] == "50"
    assert req.headers["Authorization"] == "Bearer tok"
    assert "CI_FIX_RESOLVED" in out
    assert "repo_id=hexarchy" in out
    assert "ticket_id=20260905T073219Z-fix-aliens-river-de51" in out
    assert "CI green after fix" in out


@pytest.mark.asyncio
async def test_read_events_event_type_alias_and_empty(
    respx_mock: respx.MockRouter, tmp_path
) -> None:
    route = respx_mock.get("http://board:8077/diagnostic-events").mock(
        return_value=httpx.Response(200, json={"events": []})
    )
    tool = _tool(tmp_path, _DIRECT)

    out = await tool(event_type="CI_FAILURE")

    assert route.calls.last.request.url.params["category"] == "CI_FAILURE"
    assert out.startswith("No matching diagnostic events.")


@pytest.mark.asyncio
async def test_read_events_without_board_url_explains(tmp_path) -> None:
    tool = _tool(tmp_path, None)
    out = await tool()
    assert "board_api_base_url" in out


@pytest.mark.asyncio
async def test_read_events_mill_error_is_reported(
    respx_mock: respx.MockRouter, tmp_path
) -> None:
    respx_mock.get("http://board:8077/diagnostic-events").mock(
        return_value=httpx.Response(500, text="boom")
    )
    tool = _tool(tmp_path, _DIRECT)
    out = await tool()
    assert out.startswith("Could not read diagnostic events from")


@pytest.mark.asyncio
async def test_read_events_until_filter_and_truncation_note(
    respx_mock: respx.MockRouter, tmp_path
) -> None:
    """``until`` is applied client-side and a full page carries the note.

    The mill route has no upper bound parameter.
    """
    early = {**_EVENT, "timestamp": "2026-09-01T00:00:00+00:00"}
    late = {**_EVENT, "timestamp": "2026-09-06T00:00:00+00:00"}
    respx_mock.get("http://board:8077/diagnostic-events").mock(
        return_value=httpx.Response(200, json={"events": [early, late]})
    )
    tool = _tool(tmp_path, _DIRECT)

    out = await tool(until="2026-09-02T00:00:00+00:00", limit=1)

    assert "2026-09-01T00:00:00" in out
    assert "2026-09-06T00:00:00" not in out
    assert "(result truncated to 1 events)" in out

"""Shared fixtures and factory helpers for the ticket_poll tool tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from robotsix_chat.config import DirectRepoSettings, PeriodicSettings, Settings


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "board_api_base_url": "http://board:8077",
        "board_api_token": "",
        "timeout": 10.0,
    }
    base.update(kw)
    return Settings(direct_repo=DirectRepoSettings(**base))


def _component_request_success(
    ticket_id: str,
    state: str = "DONE",
) -> Callable[..., Any]:
    """Return an async mock that returns a successful roster-style response."""

    async def _req(
        component: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> str:
        return "HTTP 200 OK\n" + json.dumps({"state": state, "ticket_id": ticket_id})

    return _req


def _component_request_error(
    error_msg: str = "Error: connection refused",
) -> Callable[..., Any]:
    """Return an async mock that returns a roster-style error."""

    async def _req(
        component: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> str:
        return error_msg

    return _req


def _component_request_ticket_list(
    tickets: Any,
) -> Callable[..., Any]:
    """Return an async mock that returns a roster-style ticket list response."""

    async def _req(
        component: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> str:
        return "HTTP 200 OK\n" + json.dumps(tickets)

    return _req


def _component_request_http_error(
    status_code: int = 502,
    body: str = '{"error": "internal"}',
) -> Callable[..., Any]:
    """Return an async mock that returns a roster-style HTTP error response."""

    async def _req(
        component: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> str:
        return f"HTTP {status_code} Bad Gateway\n{body}"

    return _req


def _stale_settings(
    ready_staleness_minutes: int = 10,
    priority_ready_staleness_minutes: int = 60,
    **kw: Any,
) -> Settings:
    """Return Settings with periodic.ready_staleness_minutes configured."""
    base: dict[str, Any] = {
        "board_api_base_url": "http://board:8077",
        "board_api_token": "",
        "timeout": 10.0,
    }
    base.update(kw)
    return Settings(
        direct_repo=DirectRepoSettings(**base),
        periodic=PeriodicSettings(
            ready_staleness_minutes=ready_staleness_minutes,
            priority_ready_staleness_minutes=priority_ready_staleness_minutes,
        ),
    )


# The 69be-shaped history: the real mill delivery path for
# 20260830T091612Z-add-chat-scoped-volume-file-write-endpoi-69be, whose PR
# (central-deploy #821) merged and whose retrospect then closed the ticket.
_69BE_PR = "https://github.com/damien-robotsix/robotsix-central-deploy/pull/821"


_69BE_HISTORY = [
    {"state": s}
    for s in (
        "draft",
        "ready",
        "code_review",
        "documenting",
        "deliverable",
        "implement_complete",
        "waiting_auto_merge",
        "done",
        "closed",
    )
]


_69BE_ID = "20260830T091612Z-add-chat-scoped-volume-file-write-endpoi-69be"


def _transition_tool(monkeypatch, captured):
    """Build transition_ticket over a fake roster component_request."""
    import robotsix_chat.ticket_poll.ticket_poll_write as tp

    async def fake_component_request(component, method, path, json_body=None, **kw):
        captured.append((component, method, path, json_body))
        return f'HTTP 200 {{"state": "{(json_body or {}).get("state")}"}}'

    async def fake_resolve(board_url, token, timeout, ids):
        return {i: i for i in ids}

    monkeypatch.setattr(tp, "_resolve_ticket_ids", fake_resolve)
    settings = _settings()
    tools = tp.build_transition_ticket_tool(
        settings, component_request=fake_component_request
    )
    assert len(tools) == 1
    return tools[0]

"""Tests for the component client tools and ``ComponentAgentClient``.

Uses ``MockResponse`` and ``install_mock_client`` from
``tests.common.mock_helpers`` to stand in for httpx so the tests
run without a real network and never touch the broker.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from robotsix_chat.component_client import build_component_tools
from robotsix_chat.component_client.client import ComponentAgentClient
from robotsix_chat.config import ComponentClientSettings, ComponentTarget, Settings

from ..common.mock_helpers import MockResponse, install_mock_client


def _settings(**kw: Any) -> ComponentClientSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "components": [ComponentTarget(base_url="http://comp-1:8090", label="Chat")],
    }
    base.update(kw)
    # Allow dict-style components for brevity in tests
    if isinstance(base.get("components"), list):
        resolved: list[ComponentTarget] = []
        for item in base["components"]:
            if isinstance(item, ComponentTarget):
                resolved.append(item)
            elif isinstance(item, dict):
                resolved.append(ComponentTarget(**item))
            else:
                resolved.append(item)
        base["components"] = resolved
    return ComponentClientSettings(**base)


# ---------------------------------------------------------------------------
# build_component_tools
# ---------------------------------------------------------------------------


def test_disabled_returns_empty() -> None:
    """Disabled settings → no tools."""
    assert build_component_tools(ComponentClientSettings()) == []


def test_enabled_returns_four_tools() -> None:
    """All four tools returned when enabled."""
    tools = build_component_tools(_settings())
    assert len(tools) == 4
    names = [t.__name__ for t in tools]
    assert names == [
        "list_component_agents",
        "get_component_telemetry",
        "get_component_config",
        "set_component_config",
    ]


# ---------------------------------------------------------------------------
# list_component_agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_component_agents_returns_configured() -> None:
    """list_component_agents enumerates configured agents and supported kinds."""
    tools = build_component_tools(
        _settings(
            components=[
                {"base_url": "http://comp-1:8090", "label": "Chat"},
                {"base_url": "http://comp-2:8090", "label": ""},
            ]
        )
    )
    list_fn = tools[0]
    result = await list_fn()
    assert "http://comp-1:8090" in result
    assert "Chat" in result
    assert "http://comp-2:8090" in result
    assert "monitor" in result
    assert "config-get" in result
    assert "config-set" in result


@pytest.mark.asyncio
async def test_list_component_agents_empty() -> None:
    """list_component_agents with no components returns a helpful message."""
    tools = build_component_tools(_settings(components=[]))
    list_fn = tools[0]
    result = await list_fn()
    assert "No component agents" in result


# ---------------------------------------------------------------------------
# get_component_telemetry (monitor)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_component_telemetry_sends_monitor_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_component_telemetry POSTs to /api/component-agent/monitor."""
    captured = install_mock_client(
        monkeypatch,
        MockResponse({"check_loops": {}, "conversations": {}}),
    )
    tools = build_component_tools(_settings())
    telemetry_fn = tools[1]
    result = await telemetry_fn("http://comp-1:8090")
    assert "check_loops" in result
    assert captured["method"] == "POST"
    assert captured["url"] == "http://comp-1:8090/api/component-agent/monitor"
    assert captured["json"] == {"kind": "monitor", "payload": {}}


@pytest.mark.asyncio
async def test_get_component_telemetry_unknown_base_url() -> None:
    """Unknown base_url returns a clear error naming known URLs."""
    tools = build_component_tools(
        _settings(components=[{"base_url": "http://comp-1:8090"}])
    )
    telemetry_fn = tools[1]
    result = await telemetry_fn("http://unknown:8090")
    assert "Unknown component agent" in result
    assert "http://comp-1:8090" in result


# ---------------------------------------------------------------------------
# get_component_config (config-get)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_component_config_sends_config_get_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_component_config POSTs to /api/component-agent/config with config-get."""
    captured = install_mock_client(
        monkeypatch,
        MockResponse({"config": {}, "settable": {}}),
    )
    tools = build_component_tools(_settings())
    config_fn = tools[2]
    result = await config_fn("http://comp-1:8090")
    assert "config" in result
    assert captured["method"] == "POST"
    assert captured["url"] == "http://comp-1:8090/api/component-agent/config"
    assert captured["json"] == {"kind": "config-get", "payload": {}}


@pytest.mark.asyncio
async def test_get_component_config_unknown_base_url() -> None:
    """Unknown base_url returns a clear error."""
    tools = build_component_tools(
        _settings(components=[{"base_url": "http://comp-1:8090"}])
    )
    config_fn = tools[2]
    result = await config_fn("http://unknown:8090")
    assert "Unknown component agent" in result


# ---------------------------------------------------------------------------
# set_component_config (config-set)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_component_config_sends_config_set_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set_component_config POSTs updates under payload["updates"]."""
    captured = install_mock_client(
        monkeypatch,
        MockResponse({"applied": {"server.port": 8080}}),
    )
    tools = build_component_tools(_settings())
    set_fn = tools[3]
    result = await set_fn("http://comp-1:8090", {"server.port": 8080})
    assert "applied" in result
    assert captured["method"] == "POST"
    assert captured["url"] == "http://comp-1:8090/api/component-agent/config"
    assert captured["json"] == {
        "kind": "config-set",
        "payload": {"updates": {"server.port": 8080}},
    }


@pytest.mark.asyncio
async def test_set_component_config_unknown_base_url() -> None:
    """Unknown base_url returns a clear error."""
    tools = build_component_tools(
        _settings(components=[{"base_url": "http://comp-1:8090"}])
    )
    set_fn = tools[3]
    result = await set_fn("http://unknown:8090", {"x": 1})
    assert "Unknown component agent" in result


@pytest.mark.asyncio
async def test_set_component_config_failure_surfaced_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP error from config-set is surfaced as a clear error string."""
    install_mock_client(
        monkeypatch,
        MockResponse({"code": "INVALID", "message": "nope"}, status_code=400),
    )
    tools = build_component_tools(_settings())
    set_fn = tools[3]
    result = await set_fn("http://comp-1:8090", {"bad.key": 1})
    assert "nope" in result


# ---------------------------------------------------------------------------
# ComponentAgentClient — direct HTTP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_monitor_posts_correct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """monitor() POSTs to the correct endpoint with the right payload."""
    captured = install_mock_client(
        monkeypatch,
        MockResponse({"ok": True}),
    )
    client = ComponentAgentClient(_settings(timeout=5.0))
    result = await client.monitor("http://agent:8090")
    assert "ok" in result
    assert captured["url"] == "http://agent:8090/api/component-agent/monitor"
    assert captured["json"] == {"kind": "monitor", "payload": {}}
    assert captured["method"] == "POST"


@pytest.mark.asyncio
async def test_client_config_get_posts_correct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_get() POSTs to the config endpoint with config-get kind."""
    captured = install_mock_client(
        monkeypatch,
        MockResponse({"config": {}, "settable": {}}),
    )
    client = ComponentAgentClient(_settings(timeout=10.0))
    result = await client.config_get("http://agent:8090")
    assert "config" in result
    assert captured["url"] == "http://agent:8090/api/component-agent/config"
    assert captured["json"] == {"kind": "config-get", "payload": {}}


@pytest.mark.asyncio
async def test_client_config_set_posts_correct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_set() POSTs updates to the config endpoint."""
    captured = install_mock_client(
        monkeypatch,
        MockResponse({"applied": {"x": 1}}),
    )
    client = ComponentAgentClient(_settings())
    result = await client.config_set("http://agent:8090", {"x": 1})
    assert "applied" in result
    assert captured["url"] == "http://agent:8090/api/component-agent/config"
    assert captured["json"] == {
        "kind": "config-set",
        "payload": {"updates": {"x": 1}},
    }


@pytest.mark.asyncio
async def test_client_strips_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing slash on base_url is stripped so the URL is not doubled."""
    captured = install_mock_client(
        monkeypatch,
        MockResponse({"ok": True}),
    )
    client = ComponentAgentClient(_settings())
    await client.monitor("http://agent:8090/")
    assert captured["url"] == "http://agent:8090/api/component-agent/monitor"


@pytest.mark.asyncio
async def test_client_http_error_returned_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 500 errors are caught and returned as error text, never raised."""
    install_mock_client(
        monkeypatch,
        MockResponse("internal error", status_code=500),
    )
    client = ComponentAgentClient(_settings())
    result = await client.monitor("http://agent:8090")
    assert "error" in result.lower()
    assert "http://agent:8090" in result


@pytest.mark.asyncio
async def test_client_timeout_returned_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout errors are caught and returned as text, never raised."""
    import httpx

    # Make the mock client's post method raise a TimeoutException
    captured: dict[str, Any] = {}

    class _TimeoutClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _TimeoutClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

        async def post(self, *args: Any, **kwargs: Any) -> None:
            raise httpx.TimeoutException("timed out")

        async def get(self, *args: Any, **kwargs: Any) -> None:
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)
    client = ComponentAgentClient(_settings())
    result = await client.monitor("http://agent:8090")
    assert "timed out" in result.lower()


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


def test_settings_disabled_requires_nothing() -> None:
    """Disabled component_client requires no special fields."""
    Settings(component_client=ComponentClientSettings())


def test_settings_enabled_with_empty_components_ok() -> None:
    """Enabled component_client with empty components is allowed (just no agents reachable)."""
    settings = Settings(component_client=ComponentClientSettings(enabled=True))
    assert settings.component_client.enabled is True
    assert settings.component_client.components == []


def test_settings_enabled_with_components_passes() -> None:
    """Enabled component_client with at least one component succeeds."""
    Settings(
        component_client=ComponentClientSettings(
            enabled=True,
            components=[ComponentTarget(base_url="http://comp-1:8090")],
        )
    )

"""Tests for the component client tools and ``ComponentAgentClient``.

robotsix-agent-comm is faked via ``sys.modules`` so these run without the
``broker`` extra installed and never touch a real broker.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from robotsix_chat.component_client import build_component_tools
from robotsix_chat.component_client.client import ComponentAgentClient
from robotsix_chat.config import ComponentClientSettings, ComponentTarget, Settings

from ..conftest import _FakeError, _install_fake_agent_comm, _Reply


def _settings(**kw: Any) -> ComponentClientSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "broker_token": "tok",
        "components": [ComponentTarget(agent_id="comp-1", label="Chat")],
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


def test_missing_broker_extra_returns_empty_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing robotsix_agent_comm → no tools + warning."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with caplog.at_level(logging.WARNING):
        tools = build_component_tools(_settings())
    assert tools == []
    assert "component_client.enabled is true but the 'broker' extra" in caplog.text


def test_enabled_with_broker_extra_returns_four_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All four tools returned when broker extra is present."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch)
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
async def test_list_component_agents_returns_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_component_agents enumerates configured agents and supported kinds."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch)
    tools = build_component_tools(
        _settings(
            components=[
                {"agent_id": "comp-1", "label": "Chat"},
                {"agent_id": "comp-2", "label": ""},
            ]
        )
    )
    list_fn = tools[0]
    result = await list_fn()
    assert "comp-1" in result
    assert "Chat" in result
    assert "comp-2" in result
    assert "monitor" in result
    assert "config-get" in result
    assert "config-set" in result


@pytest.mark.asyncio
async def test_list_component_agents_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_component_agents with no components returns a helpful message."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch)
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
    """get_component_telemetry sends {"kind": "monitor", "payload": {}}."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    captured = _install_fake_agent_comm(
        monkeypatch, reply=_Reply({"check_loops": {}, "conversations": {}})
    )
    tools = build_component_tools(_settings())
    telemetry_fn = tools[1]
    result = await telemetry_fn("comp-1")
    assert "check_loops" in result
    assert captured["payload"] == {"kind": "monitor", "payload": {}}
    assert captured["recipient"] == "comp-1"
    assert captured["broker_token"] == "tok"  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_get_component_telemetry_unknown_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown agent_id returns a clear error naming known ids."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch)
    tools = build_component_tools(_settings(components=[{"agent_id": "comp-1"}]))
    telemetry_fn = tools[1]
    result = await telemetry_fn("comp-unknown")
    assert "Unknown component agent" in result
    assert "comp-1" in result


# ---------------------------------------------------------------------------
# get_component_config (config-get)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_component_config_sends_config_get_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_component_config sends {"kind": "config-get", "payload": {}}."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    captured = _install_fake_agent_comm(
        monkeypatch, reply=_Reply({"config": {}, "settable": {}})
    )
    tools = build_component_tools(_settings())
    config_fn = tools[2]
    result = await config_fn("comp-1")
    assert "config" in result
    assert captured["payload"] == {"kind": "config-get", "payload": {}}
    assert captured["recipient"] == "comp-1"


@pytest.mark.asyncio
async def test_get_component_config_unknown_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown agent_id returns a clear error."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch)
    tools = build_component_tools(_settings(components=[{"agent_id": "comp-1"}]))
    config_fn = tools[2]
    result = await config_fn("comp-unknown")
    assert "Unknown component agent" in result


# ---------------------------------------------------------------------------
# set_component_config (config-set)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_component_config_sends_config_set_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set_component_config sends updates under payload["updates"]."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    captured = _install_fake_agent_comm(
        monkeypatch, reply=_Reply({"applied": {"server.port": 8080}})
    )
    tools = build_component_tools(_settings())
    set_fn = tools[3]
    result = await set_fn("comp-1", {"server.port": 8080})
    assert "applied" in result
    assert captured["payload"] == {
        "kind": "config-set",
        "payload": {"updates": {"server.port": 8080}},
    }
    assert captured["recipient"] == "comp-1"


@pytest.mark.asyncio
async def test_set_component_config_unknown_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown agent_id returns a clear error."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch)
    tools = build_component_tools(_settings(components=[{"agent_id": "comp-1"}]))
    set_fn = tools[3]
    result = await set_fn("comp-unknown", {"x": 1})
    assert "Unknown component agent" in result


@pytest.mark.asyncio
async def test_set_component_config_failure_surfaced_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config-set failure (Error reply) is surfaced as a clear error string."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    err = _FakeError({"code": "INVALID", "message": "nope"})
    _install_fake_agent_comm(monkeypatch, reply=err)
    tools = build_component_tools(_settings())
    set_fn = tools[3]
    result = await set_fn("comp-1", {"bad.key": 1})
    assert "nope" in result


# ---------------------------------------------------------------------------
# ComponentAgentClient — requester construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requester_constructed_with_correct_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each BrokeredRequester is constructed with the correct broker settings."""
    captured = _install_fake_agent_comm(monkeypatch)
    s = _settings(
        agent_id="chat-agent",
        broker_host="host.example.com",
        broker_port=8443,
        broker_scheme="http",
        broker_token="secret-token",
        timeout=120.0,
    )
    client = ComponentAgentClient(s)
    await client.monitor("target-1")
    assert captured["agent_id"] == "chat-agent"
    assert captured["recipient"] == "target-1"
    assert captured["broker_host"] == "host.example.com"
    assert captured["broker_port"] == 8443
    assert captured["broker_scheme"] == "http"
    assert captured["broker_token"] == "secret-token"  # pragma: allowlist secret
    assert captured["timeout"] == 120.0


@pytest.mark.asyncio
async def test_requesters_cached_per_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requester is reused for the same target, distinct for different targets."""
    captured = _install_fake_agent_comm(monkeypatch)
    s = _settings(
        components=[
            {"agent_id": "comp-1"},
            {"agent_id": "comp-2"},
        ]
    )
    client = ComponentAgentClient(s)
    # First call creates a requester.
    await client.monitor("comp-1")
    assert captured["recipient"] == "comp-1"
    first_requester = client._requesters["comp-1"]
    # Second call to same target reuses.
    await client.config_get("comp-1")
    assert client._requesters["comp-1"] is first_requester
    # Different target creates a new one.
    await client.monitor("comp-2")
    assert "comp-2" in client._requesters
    assert client._requesters["comp-2"] is not first_requester


# ---------------------------------------------------------------------------
# ComponentAgentClient — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_broker_unavailable_returned_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BrokerUnavailableError (connection refused) returns text, never raises."""
    _install_fake_agent_comm(monkeypatch, raise_exc=RuntimeError("connection refused"))
    client = ComponentAgentClient(_settings())
    result = await client.monitor("comp-1")
    assert "unreachable" in result.lower()
    assert "comp-1" in result


@pytest.mark.asyncio
async def test_client_other_exception_returned_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other exceptions are caught and returned as error text."""
    _install_fake_agent_comm(monkeypatch, raise_exc=RuntimeError("something broke"))
    client = ComponentAgentClient(_settings())
    result = await client.config_get("comp-1")
    assert "failed" in result.lower()
    assert "comp-1" in result
    assert "something broke" in result


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


def test_settings_disabled_requires_nothing() -> None:
    """Disabled component_client does not require broker_token or broker_host."""
    Settings(component_client=ComponentClientSettings())


def test_settings_enabled_requires_broker_token() -> None:
    """Enabled component_client with empty broker_token raises."""
    with pytest.raises(ValueError, match="component_client.broker_token"):
        Settings(
            component_client=ComponentClientSettings(enabled=True, broker_token="")
        )


def test_settings_enabled_requires_broker_host() -> None:
    """Enabled component_client with empty broker_host raises."""
    with pytest.raises(ValueError, match="component_client.broker_host"):
        Settings(
            component_client=ComponentClientSettings(
                enabled=True, broker_token="tok", broker_host=""
            )
        )


def test_settings_enabled_with_token_and_host_passes() -> None:
    """Enabled component_client with broker_token and broker_host succeeds."""
    Settings(
        component_client=ComponentClientSettings(
            enabled=True,
            broker_token="tok",
            broker_host="broker.example.com",
        )
    )

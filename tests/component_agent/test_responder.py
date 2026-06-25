"""Tests for the component-agent broker responder."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from robotsix_agent_comm.protocol import Metadata
from robotsix_agent_comm.protocol.messages import Request

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import EventBus
from robotsix_chat.chat.loops import CheckLoopRegistry
from robotsix_chat.component_agent.responder import (
    ComponentAgentResponder,
    ComponentAgentResponderError,
)
from robotsix_chat.config import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(kind: str, payload: dict[str, Any] | None = None) -> Request:
    """Build a minimal fake request for handler dispatch."""
    return Request(
        metadata=Metadata.create(sender="test-caller"),
        body={"kind": kind, "payload": payload or {}},
    )


@pytest.fixture
def responder_deps():
    """Build the live runtime objects the responder reads from."""
    settings = Settings(
        component_agent={
            "enabled": True,
            "broker_host": "broker.example.com",
            "broker_port": 443,
            "broker_scheme": "https",
            "broker_token": "test-token",
            "agent_id": "robotsix-chat-component",
            "timeout": 30.0,
        }
    )
    event_bus = EventBus()
    check_loop_registry = CheckLoopRegistry(store_path=None)  # no disk persistence
    conversation_store = ConversationStore()
    return {
        "settings": settings,
        "event_bus": event_bus,
        "check_loop_registry": check_loop_registry,
        "conversation_store": conversation_store,
    }


@pytest.fixture
def fake_broker(monkeypatch):
    """Install the fake agent-comm tree (BrokeredAgent) and return captured state."""
    from tests.conftest import _install_fake_agent_comm

    captured = _install_fake_agent_comm(monkeypatch)
    return captured


# ---------------------------------------------------------------------------
# Registration + lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_responder_start_registers_handler(
    responder_deps, fake_broker, monkeypatch
):
    """Starting the responder constructs a BrokeredAgent with the on_request handler."""
    responder = ComponentAgentResponder(
        settings=responder_deps["settings"],
        check_loop_registry=responder_deps["check_loop_registry"],
        conversation_store=responder_deps["conversation_store"],
        event_bus=responder_deps["event_bus"],
    )
    await responder.start()

    # The fake agent is stored as self._agent.
    agent = responder._agent
    assert agent.agent_id == "robotsix-chat-component"
    assert agent._on_request is not None

    await responder.stop()


@pytest.mark.asyncio
async def test_responder_stop_cancels_serve(responder_deps, fake_broker, monkeypatch):
    """stop() cancels the serve task and calls agent.stop()."""
    responder = ComponentAgentResponder(
        settings=responder_deps["settings"],
        check_loop_registry=responder_deps["check_loop_registry"],
        conversation_store=responder_deps["conversation_store"],
        event_bus=responder_deps["event_bus"],
    )
    await responder.start()
    assert responder._serve_task is not None
    assert not responder._serve_task.done()

    await responder.stop()
    assert responder._serve_task is None
    assert not responder._agent._running


@pytest.mark.asyncio
async def test_responder_missing_broker_extra_raises(responder_deps, monkeypatch):
    """When the broker extra is absent, start() raises ComponentAgentResponderError."""
    # Mock find_spec to return None (simulating absent package).
    import importlib.util

    _orig = importlib.util.find_spec

    def _mock_find_spec(name, package=None):
        if name == "robotsix_agent_comm":
            return None
        return _orig(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _mock_find_spec)

    responder = ComponentAgentResponder(
        settings=responder_deps["settings"],
        check_loop_registry=responder_deps["check_loop_registry"],
        conversation_store=responder_deps["conversation_store"],
        event_bus=responder_deps["event_bus"],
    )
    with pytest.raises(ComponentAgentResponderError, match="broker extra"):
        await responder.start()


# ---------------------------------------------------------------------------
# monitor handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_returns_live_telemetry(responder_deps, fake_broker):
    """Monitor returns genuine counts reflecting seeded state."""
    loop_registry = responder_deps["check_loop_registry"]
    conv_store = responder_deps["conversation_store"]
    event_bus = responder_deps["event_bus"]

    # Seed a conversation.
    sid = conv_store.new_session_id()
    conv_store.begin(sid)
    conv_store.record(sid, "owner1", "hello", "hi there")
    conv_store.begin(sid)
    conv_store.record(sid, "owner1", "how are you?", "good")

    # Seed event subscribers.
    event_bus.subscribe("client-a")
    event_bus.subscribe("client-b")

    # Seed a check loop (manually, without spawning a real task).
    async def _noop():
        pass

    task = asyncio.create_task(_noop())
    loop_registry.register(
        "client-x",
        "check every minute",
        interval_seconds=60.0,
        max_iterations=5,
        coro=task,
        reason="test loop",
    )
    task.cancel()

    # Build responder and start it.
    responder = ComponentAgentResponder(
        settings=responder_deps["settings"],
        check_loop_registry=loop_registry,
        conversation_store=conv_store,
        event_bus=event_bus,
    )
    await responder.start()

    # Dispatch a monitor request.
    req = _make_request("monitor")
    reply = responder._agent._on_request(req)

    # The reply should be a Response.
    from robotsix_agent_comm.protocol.messages import Response

    assert isinstance(reply, Response)
    result = reply.body

    # Check conversation stats.
    assert result["conversations"]["sessions"] >= 1
    assert result["conversations"]["total_turns"] >= 2

    # Check event bus.
    assert result["event_bus"]["subscribers"] >= 2

    # Check check-loops.
    assert result["check_loops"]["total"] >= 1
    loops = result["check_loops"]["loops"]
    assert any(loo["reason"] == "test loop" for loo in loops)

    # Settings snapshot is redacted.
    assert "settings" in result
    assert result["settings"]["mill.broker_token"] == "***"
    assert result["settings"]["component_agent.broker_token"] == "***"

    await responder.stop()


# ---------------------------------------------------------------------------
# config-get handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_get_returns_snapshot_and_settable_keys(
    responder_deps, fake_broker
):
    """config-get returns redacted snapshot + settable metadata."""
    responder = ComponentAgentResponder(
        settings=responder_deps["settings"],
        check_loop_registry=responder_deps["check_loop_registry"],
        conversation_store=responder_deps["conversation_store"],
        event_bus=responder_deps["event_bus"],
    )
    await responder.start()

    req = _make_request("config-get")
    reply = responder._agent._on_request(req)

    from robotsix_agent_comm.protocol.messages import Response

    assert isinstance(reply, Response)
    result = reply.body

    assert "config" in result
    assert "settable" in result
    assert result["config"]["server.log_level"] == "INFO"
    assert result["config"]["mill.enabled"] is False
    assert result["config"]["component_agent.enabled"] is True
    assert "server.log_level" in result["settable"]
    assert result["settable"]["server.log_level"]["type"] == "str"

    await responder.stop()


# ---------------------------------------------------------------------------
# config-set handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_set_applies_valid_update(responder_deps, fake_broker):
    """config-set applies a valid update and returns the audit record."""
    responder = ComponentAgentResponder(
        settings=responder_deps["settings"],
        check_loop_registry=responder_deps["check_loop_registry"],
        conversation_store=responder_deps["conversation_store"],
        event_bus=responder_deps["event_bus"],
    )
    await responder.start()

    original = responder_deps["settings"].log_level
    req = _make_request("config-set", {"updates": {"server.log_level": "DEBUG"}})
    reply = responder._agent._on_request(req)

    from robotsix_agent_comm.protocol.messages import Response

    assert isinstance(reply, Response)
    assert reply.body["applied"]["server.log_level"] == (original, "DEBUG")
    assert responder_deps["settings"].log_level == "DEBUG"

    await responder.stop()


@pytest.mark.asyncio
async def test_config_set_rejects_unknown_key(responder_deps, fake_broker):
    """config-set rejects an unknown key without mutating settings."""
    responder = ComponentAgentResponder(
        settings=responder_deps["settings"],
        check_loop_registry=responder_deps["check_loop_registry"],
        conversation_store=responder_deps["conversation_store"],
        event_bus=responder_deps["event_bus"],
    )
    await responder.start()

    original_level = responder_deps["settings"].log_level
    req = _make_request("config-set", {"updates": {"no.such.key": 42}})
    reply = responder._agent._on_request(req)

    from robotsix_agent_comm.protocol.messages import Error

    assert isinstance(reply, Error)
    assert reply.body["code"] == "UNKNOWN_KEY"
    assert responder_deps["settings"].log_level == original_level

    await responder.stop()


@pytest.mark.asyncio
async def test_config_set_rejects_type_mismatch(responder_deps, fake_broker):
    """config-set rejects a type mismatch without mutating settings."""
    responder = ComponentAgentResponder(
        settings=responder_deps["settings"],
        check_loop_registry=responder_deps["check_loop_registry"],
        conversation_store=responder_deps["conversation_store"],
        event_bus=responder_deps["event_bus"],
    )
    await responder.start()

    original_enabled = responder_deps["settings"].component_agent.enabled
    req = _make_request("config-set", {"updates": {"component_agent.enabled": "yes"}})
    reply = responder._agent._on_request(req)

    from robotsix_agent_comm.protocol.messages import Error

    assert isinstance(reply, Error)
    assert reply.body["code"] == "TYPE_MISMATCH"
    assert responder_deps["settings"].component_agent.enabled == original_enabled

    await responder.stop()


@pytest.mark.asyncio
async def test_config_set_rejects_cross_field_invalid(responder_deps, fake_broker):
    """config-set rejects an update that would violate cross-field invariants."""
    responder = ComponentAgentResponder(
        settings=responder_deps["settings"],
        check_loop_registry=responder_deps["check_loop_registry"],
        conversation_store=responder_deps["conversation_store"],
        event_bus=responder_deps["event_bus"],
    )
    await responder.start()

    original_mill_enabled = responder_deps["settings"].mill.enabled
    req = _make_request("config-set", {"updates": {"mill.enabled": True}})
    reply = responder._agent._on_request(req)

    from robotsix_agent_comm.protocol.messages import Error

    assert isinstance(reply, Error)
    assert reply.body["code"] == "CROSS_FIELD_INVALID"
    assert responder_deps["settings"].mill.enabled == original_mill_enabled

    await responder.stop()


# ---------------------------------------------------------------------------
# Unknown kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_kind_returns_error(responder_deps, fake_broker):
    """An unknown request kind returns an Error."""
    responder = ComponentAgentResponder(
        settings=responder_deps["settings"],
        check_loop_registry=responder_deps["check_loop_registry"],
        conversation_store=responder_deps["conversation_store"],
        event_bus=responder_deps["event_bus"],
    )
    await responder.start()

    req = _make_request("nonexistent-kind")
    reply = responder._agent._on_request(req)

    from robotsix_agent_comm.protocol.messages import Error

    assert isinstance(reply, Error)
    assert reply.body["code"] == "UNKNOWN_KIND"

    await responder.stop()


# ---------------------------------------------------------------------------
# Disabled responder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_responder_not_constructed_when_disabled(
    responder_deps, fake_broker, monkeypatch
):
    """When component_agent.enabled=False, the server guard prevents construction.

    The responder itself works with any settings (enabled is a server-level gate).
    """
    settings = Settings()  # default — component_agent.enabled=False
    responder = ComponentAgentResponder(
        settings=settings,
        check_loop_registry=responder_deps["check_loop_registry"],
        conversation_store=responder_deps["conversation_store"],
        event_bus=responder_deps["event_bus"],
    )
    await responder.start()
    assert responder._agent.agent_id == "robotsix-chat-component"
    await responder.stop()


# ---------------------------------------------------------------------------
# Read-only accessor regression
# ---------------------------------------------------------------------------


def test_conversation_store_stats_is_read_only():
    """stats() does not change subsequent recent_activity() ordering."""
    store = ConversationStore()
    sid1 = store.new_session_id()
    store.begin(sid1)
    store.record(sid1, "o1", "msg1", "reply1")
    store.begin(sid1)
    store.record(sid1, "o1", "msg2", "reply2")

    before = store.recent_activity(limit=10)
    stats1 = store.stats()
    stats2 = store.stats()
    after = store.recent_activity(limit=10)

    assert stats1 == stats2
    assert before == after
    assert stats1["sessions"] >= 1
    assert stats1["total_turns"] >= 2


def test_event_bus_subscriber_count_is_read_only():
    """subscriber_count() does not mutate _subscribers."""
    bus = EventBus()
    bus.subscribe("a")
    bus.subscribe("b")
    bus.subscribe("b")

    assert bus.subscriber_count() == 3
    assert bus.subscriber_count("a") == 1
    assert bus.subscriber_count("b") == 2
    assert bus.subscriber_count("nonexistent") == 0
    assert bus.subscriber_count() == 3
    assert bus.subscriber_count("a") == 1


@pytest.mark.asyncio
async def test_check_loop_registry_snapshot_is_read_only():
    """snapshot() returns a list of loops without mutating registry."""
    registry = CheckLoopRegistry(store_path=None)

    async def _noop():
        pass

    task = asyncio.create_task(_noop())
    registry.register("c1", "p1", interval_seconds=30.0, max_iterations=None, coro=task)
    task.cancel()

    snap1 = registry.snapshot()
    snap2 = registry.snapshot()
    assert snap1 == snap2
    assert len(snap1) == 1
    assert snap1[0].session_id == "c1"
    # Calling snapshot doesn't change count_running or list_for_session.
    assert registry.count_running() == 1
    assert len(registry.list_for_session("c1")) == 1

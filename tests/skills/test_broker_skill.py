"""Tests for BrokerSkill tool generation and tool calls."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

import pytest

from robotsix_chat.skills.broker_skill import BrokerSkill
from robotsix_chat.skills.spec import (
    BrokerConfig,
    CapabilityDef,
    ParameterDef,
    SkillManifest,
)
from tests.common.agent_comm_fakes import _install_fake_agent_comm


def _make_manifest(
    skill_id: str = "test",
    enabled: bool = True,
    target_agent_id: str = "target-agent",
    capabilities: list[CapabilityDef] | None = None,
) -> SkillManifest:
    """Build a minimal enabled manifest for testing."""
    if capabilities is None:
        capabilities = [CapabilityDef(name="ping", description="Ping the agent.")]
    return SkillManifest(
        skill_id=skill_id,
        display_name="Test Skill",
        enabled=enabled,
        broker=BrokerConfig(
            target_agent_id=target_agent_id,
            agent_id="robotsix-chat",
            host="broker.example.com",
            port=443,
            scheme="https",
            token="test-token",
        ),
        capabilities=capabilities,
    )


# ---------------------------------------------------------------------------
# BrokerSkill construction
# ---------------------------------------------------------------------------


def test_stores_manifest() -> None:
    """The manifest is stored and the requester starts as None."""
    manifest = _make_manifest()
    skill = BrokerSkill(manifest)
    assert skill.skill_id == "test"
    assert skill._requester is None


def test_disabled_manifest_still_instantiates() -> None:
    """Even disabled manifests can be wrapped (tools will be empty)."""
    manifest = _make_manifest(enabled=False)
    skill = BrokerSkill(manifest)
    assert skill.skill_id == "test"


# ---------------------------------------------------------------------------
# get_tools — without broker extra
# ---------------------------------------------------------------------------


def test_get_tools_returns_empty_when_extra_missing() -> None:
    """No tools when ``robotsix_agent_comm`` is not installed."""
    manifest = _make_manifest()
    skill = BrokerSkill(manifest)
    with patch(
        "robotsix_chat.skills.broker_skill._broker_extra_installed",
        return_value=False,
    ):
        tools = skill.get_tools()
    assert tools == []


def test_get_tools_returns_empty_when_no_broker_config() -> None:
    """No tools when the manifest has no broker config."""
    manifest = SkillManifest(
        skill_id="nobroker",
        enabled=True,
        capabilities=[CapabilityDef(name="x", description="desc")],
    )
    skill = BrokerSkill(manifest)
    with patch(
        "robotsix_chat.skills.broker_skill._broker_extra_installed",
        return_value=True,
    ):
        tools = skill.get_tools()
    assert tools == []


# ---------------------------------------------------------------------------
# get_tools — with broker extra
# ---------------------------------------------------------------------------


def test_creates_one_tool_per_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each capability produces one async tool callable."""
    _install_fake_agent_comm(monkeypatch, reply="ok")
    manifest = _make_manifest(
        capabilities=[
            CapabilityDef(name="ping", description="Ping."),
            CapabilityDef(name="status", description="Get status."),
        ]
    )
    skill = BrokerSkill(manifest)
    tools = skill.get_tools()
    assert len(tools) == 2
    assert tools[0].__name__ == "test_ping"
    assert tools[1].__name__ == "test_status"


def test_tool_has_docstring(monkeypatch: pytest.MonkeyPatch) -> None:
    """The capability description becomes the tool's docstring."""
    _install_fake_agent_comm(monkeypatch, reply="ok")
    manifest = _make_manifest(
        capabilities=[
            CapabilityDef(name="ping", description="Ping the test agent."),
        ]
    )
    skill = BrokerSkill(manifest)
    tools = skill.get_tools()
    assert tools[0].__doc__ == "Ping the test agent."


def test_tool_has_signature_with_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required params have no default; optional params have defaults."""
    _install_fake_agent_comm(monkeypatch, reply="ok")
    manifest = _make_manifest(
        capabilities=[
            CapabilityDef(
                name="set_config",
                description="Set config.",
                parameters={
                    "key": ParameterDef(type="string", description="Config key"),
                    "value": ParameterDef(
                        type="string",
                        description="New value",
                        required=False,
                        default="",
                    ),
                },
            ),
        ]
    )
    skill = BrokerSkill(manifest)
    tools = skill.get_tools()

    sig = inspect.signature(tools[0])
    params = list(sig.parameters.values())
    assert len(params) == 2
    assert params[0].name == "key"
    assert params[0].default is inspect.Parameter.empty
    assert params[1].name == "value"
    assert params[1].default == ""


def test_tool_annotations_carry_real_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__annotations__ must reflect each parameter's real type, not str.

    pydantic-ai builds the tool's JSON schema from __annotations__ (via
    get_type_hints), so a hardcoded str collapses every parameter's type.
    """
    _install_fake_agent_comm(monkeypatch, reply="ok")
    manifest = _make_manifest(
        capabilities=[
            CapabilityDef(
                name="typed",
                description="Typed params.",
                parameters={
                    "count": ParameterDef(type="integer", description="n"),
                    "flag": ParameterDef(type="boolean", description="b"),
                    "items": ParameterDef(type="array", description="xs"),
                    "label": ParameterDef(type="string", description="s"),
                },
            ),
        ]
    )
    skill = BrokerSkill(manifest)
    tools = skill.get_tools()

    annotations = tools[0].__annotations__
    assert annotations["count"] is int
    assert annotations["flag"] is bool
    assert annotations["items"] is list
    assert annotations["label"] is str


# ---------------------------------------------------------------------------
# Tool call behaviour
# ---------------------------------------------------------------------------


def test_tool_sends_kind_and_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool sends ``kind`` + keyword arguments in the broker payload."""
    captured = _install_fake_agent_comm(monkeypatch, reply="ok")
    manifest = _make_manifest(
        capabilities=[
            CapabilityDef(
                name="query",
                description="Query the agent.",
                kind="search",
                parameters={
                    "request": ParameterDef(type="string", description="Query text"),
                },
            ),
        ]
    )
    skill = BrokerSkill(manifest)
    tools = skill.get_tools()

    result = asyncio.run(tools[0](request="hello"))
    assert result == "ok"
    payload = captured.get("payload")
    assert payload is not None
    assert payload["kind"] == "search"
    assert payload["request"] == "hello"


def test_tool_handles_broker_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broker-unavailable errors return a friendly message, never raise."""
    _install_fake_agent_comm(
        monkeypatch,
        raise_exc=RuntimeError("connection refused by broker"),
    )
    manifest = _make_manifest(
        capabilities=[CapabilityDef(name="ping", description="Ping.")],
    )
    skill = BrokerSkill(manifest)
    tools = skill.get_tools()

    result = asyncio.run(tools[0]())
    assert "temporarily unreachable" in result


def test_tool_handles_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic errors are caught and returned as text, never raised."""
    _install_fake_agent_comm(monkeypatch, raise_exc=RuntimeError("boom"))
    manifest = _make_manifest(
        capabilities=[CapabilityDef(name="ping", description="Ping.")],
    )
    skill = BrokerSkill(manifest)
    tools = skill.get_tools()

    result = asyncio.run(tools[0]())
    assert "could not be completed" in result
    assert "boom" in result


def test_lazy_inits_requester_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requester is created on first call and reused afterwards."""
    _install_fake_agent_comm(monkeypatch, reply="ok")
    manifest = _make_manifest(
        capabilities=[
            CapabilityDef(name="a", description="A."),
            CapabilityDef(name="b", description="B."),
        ]
    )
    skill = BrokerSkill(manifest)
    tools = skill.get_tools()

    asyncio.run(tools[0]())
    requester = skill._requester
    assert requester is not None

    asyncio.run(tools[1]())
    assert skill._requester is requester

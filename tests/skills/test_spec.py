"""Tests for skills data models (spec.py)."""

from __future__ import annotations

from robotsix_chat.skills.spec import (
    BrokerConfig,
    CapabilityDef,
    ParameterDef,
    SkillManifest,
)

# ---------------------------------------------------------------------------
# ParameterDef
# ---------------------------------------------------------------------------


def test_parameter_def_defaults() -> None:
    """Default values are sensible."""
    p = ParameterDef()
    assert p.type_ == "string"
    assert p.description == ""
    assert p.required is True
    assert p.default is None


def test_parameter_def_alias_type() -> None:
    """The ``type`` alias maps to ``type_``."""
    p = ParameterDef(type="integer", description="count")
    assert p.type_ == "integer"
    assert p.description == "count"


def test_parameter_def_optional_with_default() -> None:
    """Optional parameters carry a default value."""
    p = ParameterDef(type="boolean", required=False, default=False)
    assert p.required is False
    assert p.default is False


# ---------------------------------------------------------------------------
# CapabilityDef
# ---------------------------------------------------------------------------


def test_capability_def_minimal() -> None:
    """Minimal construction works."""
    c = CapabilityDef(name="query", description="Query the agent.")
    assert c.name == "query"
    assert c.description == "Query the agent."
    assert c.kind == ""
    assert c.parameters == {}


def test_effective_kind_defaults_to_name() -> None:
    """When ``kind`` is empty, ``effective_kind`` falls back to ``name``."""
    c = CapabilityDef(name="query", description="desc")
    assert c.effective_kind == "query"


def test_effective_kind_explicit() -> None:
    """Explicit ``kind`` takes precedence."""
    c = CapabilityDef(name="query", description="desc", kind="search")
    assert c.effective_kind == "search"


def test_capability_def_with_parameters() -> None:
    """Parameters are stored and accessible."""
    c = CapabilityDef(
        name="set_config",
        description="Set config.",
        parameters={
            "key": ParameterDef(type="string", description="Config key"),
            "value": ParameterDef(type="string", description="New value"),
        },
    )
    assert len(c.parameters) == 2
    assert c.parameters["key"].type_ == "string"
    assert c.parameters["value"].required is True


# ---------------------------------------------------------------------------
# BrokerConfig
# ---------------------------------------------------------------------------


def test_broker_config_minimal() -> None:
    """Minimal construction has sensible defaults."""
    b = BrokerConfig(target_agent_id="target-1")
    assert b.target_agent_id == "target-1"
    assert b.agent_id == "robotsix-chat"
    assert b.host == "ai-broker.robotsix.net"
    assert b.port == 443
    assert b.scheme == "https"
    assert b.token.get_secret_value() == ""
    assert b.timeout == 240.0
    assert b.request_key == "message"


# ---------------------------------------------------------------------------
# SkillManifest
# ---------------------------------------------------------------------------


def test_skill_manifest_minimal() -> None:
    """Minimal construction works with sensible defaults."""
    m = SkillManifest(skill_id="test")
    assert m.skill_id == "test"
    assert m.display_name == ""
    assert m.enabled is False
    assert m.broker is None
    assert m.capabilities == []


def test_skill_manifest_full_from_dict() -> None:
    """Full pydantic validation from a dict works."""
    data = {
        "skill_id": "mill",
        "display_name": "Board Manager",
        "enabled": True,
        "broker": {
            "target_agent_id": "board-manager",
            "agent_id": "robotsix-chat",
            "host": "broker.example.com",
            "port": 443,
            "scheme": "https",
            "token": "secret",
            "timeout": 600.0,
            "request_key": "message",
        },
        "capabilities": [
            {
                "name": "consult",
                "description": "Send a request to the board manager.",
                "kind": "consult",
                "parameters": {
                    "request": {
                        "type": "string",
                        "description": "Natural-language request.",
                        "required": True,
                    }
                },
            }
        ],
    }
    m = SkillManifest.model_validate(data)
    assert m.skill_id == "mill"
    assert m.enabled is True
    assert m.broker is not None
    assert m.broker.target_agent_id == "board-manager"
    assert m.broker.token.get_secret_value() == "secret"
    assert len(m.capabilities) == 1
    assert m.capabilities[0].name == "consult"

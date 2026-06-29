"""Data models for the skill/capability system.

Defines the declarative manifest format (:class:`SkillManifest`) that
describes a broker's capabilities — what functions it exposes, what
parameters they take, and what scoping rules apply.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "BrokerConfig",
    "CapabilityDef",
    "ParameterDef",
    "SkillManifest",
]


class ParameterDef(BaseModel):
    """A single parameter in a capability's signature.

    Attributes:
        type_: JSON-schema type name (``"string"``, ``"integer"``,
            ``"boolean"``, ``"array"``).
        description: Human-readable description surfaced to the LLM.
        required: Whether the parameter must be supplied.
        default: Default value when the parameter is omitted.

    """

    type_: str = Field(default="string", alias="type")
    description: str = ""
    required: bool = True
    default: Any = None


class CapabilityDef(BaseModel):
    """One capability (LLM-callable tool) a broker exposes.

    Each capability maps to a specific request ``kind`` sent to the broker.
    The tool name is ``{skill_id}_{name}`` to avoid collisions.

    Attributes:
        name: Tool function name suffix (prefixed with ``skill_id_``).
        description: LLM-visible tool description (one-line docstring).
        kind: Broker request ``kind`` discriminator.  Defaults to *name* when
            empty, so the responder can dispatch on it.
        parameters: Named parameters the tool accepts, mapped to payload keys
            sent to the broker.

    """

    name: str
    description: str
    kind: str = ""
    parameters: dict[str, ParameterDef] = Field(default_factory=dict)

    @property
    def effective_kind(self) -> str:
        """Return the broker request kind, defaulting to *name*."""
        return self.kind or self.name


class BrokerConfig(BaseModel):
    """Broker connection and routing configuration for a skill.

    Attributes:
        target_agent_id: The broker-registered ID of the recipient agent.
        agent_id: This agent's ID on the broker (default ``robotsix-chat``).
        host: Broker hostname.
        port: Broker port.
        scheme: ``https`` (TLS) or ``http``.
        token: This agent's bearer token.  May contain ``${ENV_VAR}``
            references resolved at load time.
        timeout: Per-request timeout in seconds.
        request_key: Payload key used for the natural-language request text
            (default ``"message"``; some agents expect ``"instruction"``).

    """

    target_agent_id: str
    agent_id: str = "robotsix-chat"
    host: str = "ai-broker.robotsix.net"
    port: int = 443
    scheme: str = "https"
    token: str = ""
    timeout: float = 240.0
    request_key: str = "message"


class SkillManifest(BaseModel):
    """Declarative spec for a skill — a broker's capabilities.

    Each YAML file under ``config/skills/`` is one manifest.

    Attributes:
        skill_id: Unique short identifier (e.g. ``"mill"``, ``"calendar"``).
        display_name: Human-readable label for logging/debugging.
        enabled: Master switch — disabled manifests produce no tools.
        broker: Broker connection config.  Required for broker-based skills.
        capabilities: The capabilities (tools) this broker exposes.

    """

    skill_id: str
    display_name: str = ""
    enabled: bool = False
    broker: BrokerConfig | None = None
    capabilities: list[CapabilityDef] = Field(default_factory=list)

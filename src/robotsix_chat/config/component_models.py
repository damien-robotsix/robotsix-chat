"""Component Settings Models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeSettings(BaseModel):
    """Local, writable knowledge base for agent-authored operational notes.

    A deliberate, explicit, agent-curated store of durable lessons and findings
    — plain local JSON, no embeddings, no external service, always-on.  The
    agent writes notes via five tools
    (``add_knowledge_note``, ``append_to_knowledge_note``,
    ``update_knowledge_note``, ``list_knowledge_notes``,
    ``read_knowledge_note``)
    and can re-read and revise them by id across sessions.

    This store is **complementary to**, not a duplicate of, the long-term
    memory component (``memory/``), which automatically recalls past
    conversations by similarity; this knowledge base holds notes the agent
    deliberately authors and addresses by id.

    Attributes:
        enabled: Master switch.  Default ``True`` — this is a purely local,
            no-credential, no-external-dependency primitive.
        path: Path to the JSON persistence file.  Default
            ``/data/knowledge.json``.

    """

    enabled: bool = True
    path: str = "/data/knowledge.json"
    model_config = ConfigDict(extra="forbid")


class SelfReviewSettings(BaseModel):
    """Self-review tool — a read-only digest of live conversation activity.

    When enabled, the agent gains a ``read_recent_activity`` tool that
    reads the in-process :class:`~robotsix_chat.chat.conversation.ConversationStore`
    (short-lived per-client conversation turns) and returns a human-readable
    multi-session digest.  This is a deliberate, explicit, cross-client
    snapshot — complementary to, but independent of, the long-term
    episodic memory subsystem (``src/robotsix_chat/memory/``).

    Default-disabled so behaviour is unchanged unless explicitly turned on.

    Attributes:
        enabled: Master switch. When ``True``, the ``read_recent_activity``
            tool is attached to the agent.
        recent_activity_limit: Maximum number of conversations returned by
            the tool (clamps the caller's ``limit`` argument).

    """

    enabled: bool = False
    recent_activity_limit: int = 20
    model_config = ConfigDict(extra="forbid")


class ComponentTarget(BaseModel):
    """A single component agent that the chat may inspect or configure.

    Attributes:
        base_url: Base URL of the component agent (e.g.
            ``"http://comp-1:8090"``).
        label: Optional human-readable label shown in discovery output.

    """

    base_url: str
    label: str = ""
    model_config = ConfigDict(extra="forbid")


class ComponentClientSettings(BaseModel):
    """Component agent client settings — inspect and configure remote agents.

    When enabled, the chat agent gains four tools: ``list_component_agents``,
    ``get_component_telemetry``, ``get_component_config``, and
    ``set_component_config`` so it can enumerate configured component agents,
    read live telemetry, and read/update configuration on demand via direct
    HTTP.

    Attributes:
        enabled: Master switch.
        timeout: Per-request HTTP timeout (seconds).
        components: Allowlist of component agents the chat may contact.
            Each entry has a ``base_url`` and an optional ``label``.

    """

    enabled: bool = False
    timeout: float = 240.0
    components: list[ComponentTarget] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")

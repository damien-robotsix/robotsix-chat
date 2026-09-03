"""OpenRouter Settings Models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class OpenRouterSettings(BaseModel):
    """Canonical OpenRouter credential block (component standard).

    Holds OpenRouter API keys keyed by the **alias** each LLM-generating
    subsystem is billed under.  The alias matches the subsystem's Langfuse
    project name in the top-level ``langfuse.projects`` map, so cost-monitor
    can join OpenRouter provider spend to Langfuse traces via the shared
    alias.

    The main chat agent runs on the Claude SDK and needs no OpenRouter key,
    so this component declares only the ``robotsix-chat-cognee`` alias — the
    :mod:`robotsix_chat.config`.

    Attributes:
        keys: OpenRouter alias (Langfuse project name) → API key.

    """

    keys: dict[str, SecretStr] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    def key(self, alias: str) -> SecretStr:
        """Return the API key for *alias*, or an empty secret when absent."""
        return self.keys.get(alias, SecretStr(""))

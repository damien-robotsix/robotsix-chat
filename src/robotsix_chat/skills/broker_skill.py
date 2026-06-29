"""Generic broker-based skill — one :class:`BrokerSkill` per manifest.

Wraps a :class:`~robotsix_chat.skills.spec.SkillManifest` and produces one
LLM-callable async tool per declared capability.  Each tool sends a
structured request to the broker's target agent and returns the reply.

The broker requester is lazy-imported (the optional ``broker`` extra) so the
skill loader works even when ``robotsix-agent-comm`` is not installed —
:meth:`get_tools` returns ``[]`` in that case.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
from collections.abc import Callable
from typing import Any

from robotsix_chat.broker_client import (
    _is_broker_unavailable,
)
from robotsix_chat.skills.spec import BrokerConfig, CapabilityDef, SkillManifest

logger = logging.getLogger(__name__)

__all__ = ["BrokerSkill"]


def _broker_extra_installed() -> bool:
    """Return ``True`` when the optional ``broker`` extra is importable."""
    return importlib.util.find_spec("robotsix_agent_comm") is not None


class BrokerSkill:
    """A loaded broker skill — holds the manifest and lazily creates tools.

    Each :class:`BrokerSkill` wraps one :class:`SkillManifest` and produces
    one async callable per :class:`CapabilityDef`.  The tools send structured
    payloads to the broker::

        {
            "kind": "<capability.effective_kind>",
            "<broker.request_key>": "<request_text>",
            "<param_name>": <param_value>",
            ...
        }

    The requester is built once at first tool call (lazy init) so disabled
    skills never trigger the broker-extra import.
    """

    def __init__(self, manifest: SkillManifest) -> None:
        """Wrap *manifest* — tools are built lazily on first use."""
        self._manifest = manifest
        self._requester: Any = None  # BrokeredRequester, lazy-init

    @property
    def skill_id(self) -> str:
        """The manifest's ``skill_id``."""
        return self._manifest.skill_id

    def get_tools(self) -> list[Callable[..., Any]]:
        """Return the list of LLM-callable tools for this skill.

        Returns ``[]`` when the broker extra is not installed or the manifest
        has no broker config.
        """
        if not _broker_extra_installed():
            logger.debug(
                "Skill %r: broker extra not installed, no tools.", self.skill_id
            )
            return []
        manifest = self._manifest
        if manifest.broker is None:
            logger.warning("Skill %r has no broker config, skipping.", self.skill_id)
            return []
        tools: list[Callable[..., Any]] = []
        for cap in manifest.capabilities:
            tools.append(self._make_tool(cap, manifest.broker))
        return tools

    # ------------------------------------------------------------------
    # Tool factory
    # ------------------------------------------------------------------

    def _make_tool(
        self,
        cap: CapabilityDef,
        broker: BrokerConfig,
    ) -> Callable[..., Any]:
        """Build a single async tool callable for *cap*."""
        tool_name = f"{self.skill_id}_{cap.name}"
        tool_doc = cap.description

        # Determine which parameters the tool accepts.
        # We build the function dynamically so the LLM framework can
        # introspect its signature for tool-schema generation.

        async def tool_fn(**kwargs: Any) -> str:
            return await self._call_broker(cap, broker, kwargs)

        # Build a proper signature so llmio can extract parameter schemas.
        params = []
        for pname, pdef in cap.parameters.items():
            default = inspect.Parameter.empty if pdef.required else pdef.default
            # Map JSON-schema type to Python type for the signature hint.
            py_type: type = str
            if pdef.type_ == "integer":
                py_type = int
            elif pdef.type_ == "boolean":
                py_type = bool
            elif pdef.type_ == "array":
                py_type = list
            params.append(
                inspect.Parameter(
                    pname,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=default,
                    annotation=py_type,
                )
            )

        tool_fn.__name__ = tool_name
        tool_fn.__qualname__ = tool_name
        tool_fn.__doc__ = tool_doc
        tool_fn.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
        tool_fn.__annotations__ = {p.name: p.annotation for p in params}

        return tool_fn

    # ------------------------------------------------------------------
    # Broker call
    # ------------------------------------------------------------------

    async def _call_broker(
        self,
        cap: CapabilityDef,
        broker: BrokerConfig,
        kwargs: dict[str, Any],
    ) -> str:
        """Send a capability-scoped request to the broker and return the reply."""
        self._ensure_requester(broker)

        # Build the payload: kind discriminator + parameters.
        payload: dict[str, object] = {"kind": cap.effective_kind}
        payload.update(kwargs)

        try:
            return await asyncio.to_thread(self._requester.request, payload)
        except Exception as exc:
            if _is_broker_unavailable(exc):
                logger.warning(
                    "Skill %r capability %r: broker unavailable: %s",
                    self.skill_id,
                    cap.name,
                    exc,
                )
                return (
                    f"The {self.skill_id} agent is temporarily unreachable. "
                    f"Please try again in a moment."
                )
            logger.warning(
                "Skill %r capability %r failed: %s",
                self.skill_id,
                cap.name,
                exc,
            )
            return f"The {self.skill_id} request could not be completed: {exc}"

    def _ensure_requester(self, broker: BrokerConfig) -> None:
        """Lazy-init the :class:`BrokeredRequester` on first use."""
        if self._requester is not None:
            return
        from robotsix_agent_comm.sdk import BrokeredRequester  # noqa: I001

        self._requester = BrokeredRequester(
            broker.agent_id,
            broker.target_agent_id,
            broker_host=broker.host,
            broker_port=broker.port,
            broker_scheme=broker.scheme,
            broker_token=broker.token,
            timeout=broker.timeout,
            default_reply=f"The {self.skill_id} agent returned an empty reply.",
        )

"""Langfuse Settings Models."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

#: Langfuse project for the main chat agent's LLM traffic.  The component
#: standard fixes a component's main project name as ``<repo>``.
PROJECT_MAIN = "robotsix-chat"


_logger = logging.getLogger(__name__)


class LangfuseProjectCreds(BaseModel):
    """Credentials for one Langfuse project.

    Attributes:
        public_key: Langfuse public key for the project.
        secret_key: Langfuse secret key for the project.
        project_id: Langfuse project id.  Optional — only consumers that
            address a project by id rather than by name need it.

    """

    public_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    project_id: str = ""
    model_config = ConfigDict(extra="forbid")

    def is_configured(self) -> bool:
        """Return ``True`` when both key halves are set."""
        return bool(
            self.public_key.get_secret_value() and self.secret_key.get_secret_value()
        )


class LangfuseSettings(BaseModel):
    """Canonical Langfuse credential block (component standard).

    One block per component, holding the instance ``host`` and every
    Langfuse project the component traces to, keyed by the project's
    **name**.  The component standard fixes those names as ``<repo>`` for
    the component's main LLM function and ``<repo>-<function>`` for each
    additional LLM-generating subsystem — so this component declares
    ``robotsix-chat`` (main agent).

    Keeping every project in one standard block is what lets central-deploy
    enumerate the fleet's credentials uniformly and dispatch them to the
    consumers that need them (the chat trace proxy, cost-monitor's
    reconciliation).  See ``PROJECT_MAIN`` / ``PROJECT_MEMORY`` in
    :mod:`robotsix_chat.config` for this component's names.

    Attributes:
        host: Langfuse instance base URL.
        projects: Langfuse project name → credentials.

    """

    host: str = "https://cloud.langfuse.com"
    projects: dict[str, LangfuseProjectCreds] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_single_project_keys(cls, data: Any) -> Any:
        """Strip the pre-block ``public_key``/``secret_key`` fields.

        A deployed ``config.json`` written before the credential block
        existed carries these at the top of ``langfuse``.  ``extra="forbid"``
        would otherwise reject the whole file and crash-loop the container on
        the first start after an image upgrade.

        The values are **not** migrated — per the standard there is no
        credential fallback, so an unmigrated deployment traces nothing and
        reports no projects until its config is rewritten.  Dropping them
        only keeps that a visible, fixable state instead of an outage.
        """
        if isinstance(data, dict):
            legacy = [k for k in ("public_key", "secret_key") if k in data]
            if legacy:
                data = {k: v for k, v in data.items() if k not in legacy}
                _logger.warning(
                    "Ignoring legacy langfuse.%s — credentials now live in "
                    "langfuse.projects.<project-name>; this deployment will "
                    "not trace until its config is migrated",
                    "/".join(legacy),
                )
        return data

    def creds(self, project: str) -> LangfuseProjectCreds:
        """Return credentials for *project*, or empty creds when absent.

        Absent and half-filled projects both yield credentials whose
        ``is_configured()`` is ``False``, so callers degrade to "tracing
        off" rather than raising.
        """
        return self.projects.get(project) or LangfuseProjectCreds()


class LangfuseInspectSettings(BaseModel):
    """Langfuse trace-inspection tool — lets the agent query recent traces.

    Reuses the main ``langfuse`` credentials (public key + secret key + host)
    for API authentication — no separate credential fields.  When enabled, the
    agent gains an ``inspect_langfuse_trace`` tool that fetches and summarises
    recent implement traces for a given ticket or trace id.

    Attributes:
        enabled: Master switch.  Default ``False``.
        max_traces: Maximum number of traces returned per query.  Default ``5``.

    """

    enabled: bool = False
    max_traces: int = 5
    model_config = ConfigDict(extra="forbid")

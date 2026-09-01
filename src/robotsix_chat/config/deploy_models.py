"""Deploy Settings Models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from robotsix_chat.config.constants import drop_blank_numeric_sentinels


class ComponentCredentials(BaseModel):
    """Stored credentials for a single roster component.

    Keys are component IDs matching the ``id`` field returned by the
    central-deploy ``GET /chat/components`` roster. Each entry carries
    credentials for all supported auth schemes; the roster entry's ``auth.type``
    selects which fields are used.

    Attributes:
        basic_auth_username: Username for HTTP Basic authentication.
        basic_auth_password: Password for HTTP Basic authentication.
        header_token: Token value for header-based authentication
            (e.g. ``X-API-Key``).

    """

    basic_auth_username: SecretStr = SecretStr("")
    basic_auth_password: SecretStr = SecretStr("")
    header_token: SecretStr = SecretStr("")
    model_config = ConfigDict(extra="forbid")


class CentralDeploySettings(BaseModel):
    """Central-deploy roster and component-access settings.

    Provides the base URL for the central-deploy management-plane API.
    At session start the agent fetches the
    ``GET /chat/components`` roster (a list of component agents the chat
    is allowed to call), caches it with a short TTL, and loads each
    component's declared skill into the agent.

    Attributes:
        url: Canonical base URL of the central-deploy / deploy-lifecycle API
            (no trailing slash).  Single source of truth for the deploy-plane
            address; the lifecycle client and feedback roster lookup both read
            it (the former ``lifecycle.base_url`` was retired in favour of it).
        roster_cache_ttl: Seconds to cache the roster before re-fetching.
            Default 300 (5 min).
        component_response_max_chars: Maximum characters of a component API
            response returned to the agent; longer bodies are truncated.
            Default 200000.
        deploy_api_key: Canonical deploy-plane credential — the shared
            secret between this chat component and central-deploy.  Sent as
            the ``X-API-Key`` header on outbound roster/lifecycle calls, and
            required (matched) on inbound central-deploy → chat endpoints
            (``/chat/github/*`` and the feedback roster lookup).  This is the
            single source of truth; the per-block ``deploy_api_key`` fields
            were retired in favour of it.
        component_credentials: Per-component credentials keyed by
            component id.  Each entry carries credentials for all
            supported auth schemes; the roster entry's ``auth.type``
            selects which fields are used.

    """

    model_config = ConfigDict(extra="forbid")

    url: str = ""
    deploy_api_key: SecretStr = SecretStr("")
    roster_cache_ttl: float = 300.0
    component_response_max_chars: int = 200_000
    component_request_timeout: float = Field(
        default=60.0,
        description=(
            "Per-request HTTP timeout (seconds) for component API calls "
            "made via component_request.  Also bounds the total wall-clock "
            "time for all retry attempts via the retry deadline.  Default "
            "60s — raise this if upstream components are slow to respond."
        ),
    )
    component_credentials: dict[str, ComponentCredentials] = Field(default_factory=dict)
    component_fallbacks: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Baked-in fallback base URLs for components that may be missing "
            "from the central-deploy roster (e.g. after a redeploy). "
            'Keyed by component id (e.g. "robotsix-mill"). When the roster '
            "returned by central-deploy is missing a component, the fallback "
            "URL is used instead. This keeps monitors running through "
            "transient roster gaps."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_api_token(cls, data: Any) -> Any:
        """Strip ``api_token`` from incoming config dicts.

        The field was retired fleet-wide (2026-08-21) when central-deploy
        removed the auto-provisioning engine.  Existing config files may
        still carry the key; strip it so validation passes rather than
        crashing on the unknown field.
        """
        if isinstance(data, dict):
            data.pop("api_token", None)
        return data

    @model_validator(mode="before")
    @classmethod
    def _strip_blank_numeric(cls, data: Any) -> Any:
        """Drop legacy ``""`` sentinels for numeric fields so old configs load."""
        return drop_blank_numeric_sentinels(cls, data)

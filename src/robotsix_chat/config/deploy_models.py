"""Deploy Settings Models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


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

    Provides the base URL and bearer token for the central-deploy
    management-plane API.  At session start the agent fetches the
    ``GET /chat/components`` roster (a list of component agents the chat
    is allowed to call), caches it with a short TTL, and loads each
    component's declared skill into the agent.

    Attributes:
        url: Base URL of the central-deploy API (no trailing slash).
        api_token: Bearer token for authenticating to the central-deploy
            API.  Required when any component access is expected.
        roster_cache_ttl: Seconds to cache the roster before re-fetching.
            Default 300 (5 min).
        component_credentials: Per-component credentials keyed by
            component id.  Each entry carries credentials for all
            supported auth schemes; the roster entry's ``auth.type``
            selects which fields are used.

    """

    model_config = ConfigDict(extra="forbid")

    url: str = ""
    api_token: SecretStr = SecretStr("")
    roster_cache_ttl: float = 300.0
    component_response_max_chars: int = 200_000
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

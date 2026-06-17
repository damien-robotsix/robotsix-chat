"""Layered configuration for robotsix-chat.

Settings resolve through a single, predictable cascade that matches the
rest of the robotsix stack (``robotsix-mill`` / ``robotsix-auto-mail``):

    pydantic field defaults  →  YAML config file  →  environment variables

with each later layer overriding the earlier one field-by-field. YAML
loading is delegated to the shared ``robotsix-yaml-config`` library so
every repo in the stack reads YAML the same way.

The LLM is selected through ``robotsix-llmio``'s consumer-facing
``transport`` + ``model_level`` config (``robotsix_llmio.config``): you pick a
transport alias and a model level, never a concrete provider class.

The YAML file lives at ``config/chat.local.yaml`` by default (gitignored
so credentials never land in the repo); copy ``config/chat.local.example.yaml``
to create it. Override the path with the ``CHAT_CONFIG_PATH`` env var.

A ``.env`` file in the working directory is still loaded (via
``python-dotenv``) before environment variables are read, so existing
``.env``-based deployments keep working.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from robotsix_llmio.config import LEVEL1_DEFAULT, LEVEL2_DEFAULT, LEVEL3_DEFAULT
from robotsix_yaml_config import (
    YamlConfigError,
    flatten_config,
    read_yaml_file,
)

logger = logging.getLogger(__name__)

# Default YAML config file (gitignored; copy from the committed example).
DEFAULT_CONFIG_PATH = Path("config") / "chat.local.yaml"

# Env var that overrides the YAML config file path.
CONFIG_PATH_ENV = "CHAT_CONFIG_PATH"

# Case-insensitive truthy spellings for the AUTH_ENABLED env override.
_TRUE_VALUES = {"1", "true", "yes", "on"}

# robotsix-llmio now owns the level → (transport, model) mapping. The chat
# just picks a capability *level*; the transport (and model) for that level
# come from llmio's baked default TierConfig (single source of truth):
#   level 1 → openrouter[deepseek]/deepseek-v4-flash  (cheapest)
#   level 2 → openrouter[deepseek]/deepseek-v4-pro
#   level 3 → claude-sdk/opus                          (most capable; keyless)
_LEVEL_DEFAULTS = {1: LEVEL1_DEFAULT, 2: LEVEL2_DEFAULT, 3: LEVEL3_DEFAULT}
_VALID_MODEL_LEVELS = set(_LEVEL_DEFAULTS)

# Transports that authenticate without an API key (via the `claude` CLI).
_KEYLESS_TRANSPORTS = {"claude-sdk"}


def level_needs_api_key(level: int) -> bool:
    """Whether *level*'s default transport requires an ``api_key``.

    True for key-bearing transports (e.g. ``openrouter[deepseek]``), False for
    keyless ones (``claude-sdk``). Unknown levels are treated as needing a key
    (model_level is validated separately before this matters).
    """
    tlc = _LEVEL_DEFAULTS.get(level)
    return tlc is None or tlc.transport not in _KEYLESS_TRANSPORTS


# Maps nested YAML ``section.field`` paths to ``Settings`` field names.
# The whole ``auth`` mapping is passed through as-is (a dict) so pydantic
# parses it into the nested :class:`AuthSettings` model.
_YAML_PATH_TO_FIELD: dict[str, str] = {
    "llmio.model_level": "llmio_model_level",
    "llmio.api_key": "llmio_api_key",  # pragma: allowlist secret
    "agent.instruction": "agent_instruction",
    "server.host": "server_host",
    "server.port": "server_port",
    "server.log_level": "log_level",
    "server.cors_allow_origins": "cors_allow_origins",
    "auth": "auth",
}


class ConfigError(YamlConfigError):
    """Raised for config-loading failures (missing file, malformed YAML).

    Subclasses the shared base so ``except YamlConfigError`` handlers in
    the stack keep working.
    """


def _parse_bool(value: str) -> bool:
    """Parse an env-var string into a bool (``"true"``/``"1"``/… → True)."""
    return value.strip().lower() in _TRUE_VALUES


class AuthSettings(BaseModel):
    """HTTP Basic Auth settings gating the browser UI and ``/chat``.

    Attributes:
        enabled: When ``True``, every request except ``GET /health`` must
            carry valid HTTP Basic credentials.
        username: The single accepted username.
        password: The single accepted password (required when *enabled*).
    """

    enabled: bool = False
    username: str = "admin"
    password: str = ""


class Settings(BaseModel):
    """Application settings, resolved from defaults → YAML → environment.

    The LLM is configured the robotsix-llmio way — pick a capability
    ``model_level`` and llmio resolves the transport + model for that level
    (from its baked default :class:`~robotsix_llmio.config.TierConfig`).

    Attributes:
        llmio_model_level: Capability level — ``1`` (cheapest/fastest), ``2``,
            or ``3`` (most capable). The level encodes the transport + model:
            by default levels 1-2 use ``openrouter[deepseek]`` and level 3 uses
            ``claude-sdk``/``opus``.
        llmio_api_key: Provider API key, forwarded to llmio when the chosen
            level's transport needs one (e.g. ``openrouter[deepseek]``); unused
            by keyless transports like ``claude-sdk``.
        agent_instruction: System instruction handed to the LLM agent.
        server_host: Host address the chat SSE server binds to.
        server_port: Port the chat SSE server listens on.
        log_level: Python logging level name.
        cors_allow_origins: Origins allowed to call /chat cross-origin
            (empty = none; ``["*"]`` = any). Only needed when the browser
            UI is hosted on a different origin than the server.
        auth: HTTP Basic Auth settings gating the UI and ``/chat``.
    """

    llmio_model_level: int = 3
    llmio_api_key: str = ""
    agent_instruction: str = "You are a helpful assistant."
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    log_level: str = "INFO"
    cors_allow_origins: list[str] = Field(default_factory=list)
    auth: AuthSettings = Field(default_factory=AuthSettings)

    def model_post_init(self, __context: Any) -> None:
        """Validate fields that cannot be expressed via simple type annotations."""
        if self.llmio_model_level not in _VALID_MODEL_LEVELS:
            raise ValueError(
                f"llmio.model_level must be one of {sorted(_VALID_MODEL_LEVELS)}, "
                f"got {self.llmio_model_level!r}"
            )
        # Levels whose transport authenticates via the `claude` CLI need no
        # key; key-bearing levels (e.g. openrouter[deepseek]) require one.
        if level_needs_api_key(self.llmio_model_level) and not self.llmio_api_key:
            raise ValueError(
                f"llmio.api_key must be set for model_level "
                f"{self.llmio_model_level} (its transport needs a key) — provide "
                "it via LLMIO_API_KEY, a .env file, or the `llmio.api_key` field "
                "of your config file (or use model_level 3, which is keyless)"
            )
        if self.auth.enabled and not self.auth.password:
            raise ValueError(
                "auth.password must be set when auth is enabled — provide it "
                "via AUTH_PASSWORD or the `auth.password` field of your config file"
            )

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> Settings:
        """Load settings through the full cascade (YAML file + environment).

        Resolution order (highest priority last):

        1. pydantic field defaults
        2. the YAML config file — explicit *config_path* arg, else the
           ``CHAT_CONFIG_PATH`` env var, else ``config/chat.local.yaml`` if
           present (otherwise no file)
        3. environment variables (with ``.env`` support)

        Pass ``config_path=""`` to skip the YAML file entirely (used by the
        test suite).
        """
        _load_dotenv()
        nested = cls._read_yaml(config_path)
        flat = flatten_config(nested, _YAML_PATH_TO_FIELD)
        return cls._build(flat)

    @classmethod
    def from_env(cls) -> Settings:
        """Load settings from environment variables only (no YAML file).

        Calls ``load_dotenv()`` first so a ``.env`` file in the working
        directory (or any parent) is picked up automatically. Equivalent
        to ``Settings.load(config_path="")``.
        """
        _load_dotenv()
        return cls._build({})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @classmethod
    def _read_yaml(cls, config_path: str | Path | None) -> dict[str, Any]:
        """Resolve the YAML config path and read it into a nested dict.

        Returns an empty dict when no file applies. Raises
        :class:`FileNotFoundError` when an explicit path (arg or
        ``CHAT_CONFIG_PATH``) points at a missing file, and
        :class:`ConfigError` on malformed YAML.
        """
        explicit = True
        if config_path is not None:
            if config_path == "":
                return {}
            path = Path(config_path)
        else:
            env_path = os.getenv(CONFIG_PATH_ENV)
            if env_path:
                path = Path(env_path)
            elif DEFAULT_CONFIG_PATH.exists():
                path, explicit = DEFAULT_CONFIG_PATH, False
            else:
                return {}

        if explicit and not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        try:
            return read_yaml_file(path)
        except YamlConfigError as exc:
            raise ConfigError(str(exc)) from exc

    @classmethod
    def _build(cls, flat: dict[str, Any]) -> Settings:
        """Overlay environment variables onto *flat* YAML values and build."""
        raw: dict[str, Any] = {k: v for k, v in flat.items() if k != "auth"}
        auth_raw: dict[str, Any] = dict(flat.get("auth") or {})

        def env_override(field: str, env_name: str) -> None:
            value = os.getenv(env_name)
            if value is not None:
                raw[field] = value

        env_override("llmio_api_key", "LLMIO_API_KEY")
        env_override("agent_instruction", "AGENT_INSTRUCTION")
        env_override("server_host", "SERVER_HOST")
        env_override("log_level", "LOG_LEVEL")

        level_str = os.getenv("LLMIO_MODEL_LEVEL")
        if level_str is not None:
            try:
                raw["llmio_model_level"] = int(level_str)
            except ValueError:
                raise ValueError(
                    f"LLMIO_MODEL_LEVEL must be an integer, got {level_str!r}"
                ) from None

        port_str = os.getenv("SERVER_PORT")
        if port_str is not None:
            try:
                raw["server_port"] = int(port_str)
            except ValueError:
                raise ValueError(
                    f"SERVER_PORT must be an integer, got {port_str!r}"
                ) from None

        cors_raw = os.getenv("CORS_ALLOW_ORIGINS")
        if cors_raw is not None:
            raw["cors_allow_origins"] = [
                origin.strip() for origin in cors_raw.split(",") if origin.strip()
            ]

        auth_enabled = os.getenv("AUTH_ENABLED")
        if auth_enabled is not None:
            auth_raw["enabled"] = _parse_bool(auth_enabled)
        auth_username = os.getenv("AUTH_USERNAME")
        if auth_username is not None:
            auth_raw["username"] = auth_username
        auth_password = os.getenv("AUTH_PASSWORD")
        if auth_password is not None:
            auth_raw["password"] = auth_password

        if auth_raw:
            raw["auth"] = auth_raw

        return cls(**raw)


def _load_dotenv() -> None:
    """Load a ``.env`` file into the environment if python-dotenv is present."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover — python-dotenv is a required dep
        logger.debug("python-dotenv not installed; skipping .env loading")

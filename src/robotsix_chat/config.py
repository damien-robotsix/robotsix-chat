"""Layered configuration for robotsix-chat.

Settings resolve through a single, predictable cascade that matches the
rest of the robotsix stack (``robotsix-mill`` / ``robotsix-auto-mail``):

    pydantic field defaults  →  YAML config file  →  environment variables

with each later layer overriding the earlier one field-by-field. YAML
loading is delegated to the shared ``robotsix-yaml-config`` library so
every repo in the stack reads YAML the same way.

The LLM is selected through ``robotsix-llmio``'s consumer-facing
``provider-model`` tier identifier (``robotsix_llmio.config``): you pick a
capability level and llmio resolves the provider + model from its baked
defaults, never a concrete provider class.

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

# robotsix-llmio now owns the level → provider-model mapping. The chat
# just picks a capability *level*; the combined provider-model identifier for
# that level comes from llmio's baked default TierLevelConfig (single source
# of truth):
#   level 1 → openrouter[deepseek]-deepseek/deepseek-v4-flash  (cheapest)
#   level 2 → openrouter[deepseek]-deepseek/deepseek-v4-pro
#   level 3 → claudeSDK-opus  (most capable; keyless)
_LEVEL_DEFAULTS = {1: LEVEL1_DEFAULT, 2: LEVEL2_DEFAULT, 3: LEVEL3_DEFAULT}
_VALID_MODEL_LEVELS = set(_LEVEL_DEFAULTS)

# Provider prefix for the keyless Claude SDK tier (auth via logged-in
# `claude` CLI — no API key needed).
_KEYLESS_PROVIDER = "claudeSDK"


def level_needs_api_key(level: int) -> bool:
    """Whether *level*'s default provider requires an ``api_key``.

    True for key-bearing providers (e.g. ``openrouter``), False for the
    keyless ``claudeSDK`` provider. Unknown levels are treated as needing a
    key (model_level is validated separately before this matters).
    """
    tlc = _LEVEL_DEFAULTS.get(level)
    return tlc is None or tlc.provider != _KEYLESS_PROVIDER


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
    "server.correlation_id_header": "correlation_id_header",
    "auth": "auth",
    "memory": "memory",
    "mill": "mill",
    "conversation": "conversation",
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


class MemoryLlmSettings(BaseModel):
    """Extraction-LLM config for cognee memory (OpenRouter via litellm).

    Defaults match the validated robotsix setup: the cheap OpenRouter DeepSeek
    model through the ``custom`` provider. ``api_key`` is required when memory
    is enabled (provide it via ``MEMORY_LLM_API_KEY``).
    """

    provider: str = "custom"
    model: str = "openrouter/deepseek/deepseek-v4-flash"
    endpoint: str = "https://openrouter.ai/api/v1"
    api_key: str = ""


class MemoryEmbeddingSettings(BaseModel):
    """Embedding config for cognee memory (remote OpenAI-compatible server).

    Defaults target a self-hosted Ollama ``bge-m3`` endpoint. ``provider`` must
    be ``openai_compatible`` for that path (it tolerates a non-OpenAI model
    name); ``endpoint`` (e.g. ``http://host:11434/v1``) is required when memory
    is enabled. ``dimensions`` is sticky — changing it invalidates stored
    vectors.
    """

    provider: str = "openai_compatible"
    model: str = "bge-m3"
    endpoint: str = ""
    dimensions: int = 1024
    api_key: str = "ollama"
    huggingface_tokenizer: str = "BAAI/bge-m3"


class MemorySettings(BaseModel):
    """Long-term agent memory (cognee). Disabled by default.

    Attributes:
        enabled: When ``True``, the agent recalls before and persists after each
            reply. Requires the ``memory`` extra (cognee) installed.
        data_dir: Directory for cognee's stores (relative to the working dir).
            Put it under the persistent ``.data`` mount so memory survives
            container redeploys.
        recall_search_type: cognee ``SearchType`` name used for recall.
            ``GRAPH_COMPLETION`` (default) returns clean, relevant facts as text
            but costs one (cheap) LLM call per message; retrieval-only types
            like ``CHUNKS``/``SUMMARIES`` are faster but return raw, noisier
            payloads.
        llm: Extraction-LLM config (graph building / consolidation).
        embedding: Embedding-server config (semantic search).

    """

    enabled: bool = False
    data_dir: str = ".data/cognee"
    recall_search_type: str = "GRAPH_COMPLETION"
    llm: MemoryLlmSettings = Field(default_factory=MemoryLlmSettings)
    embedding: MemoryEmbeddingSettings = Field(default_factory=MemoryEmbeddingSettings)


class MillSettings(BaseModel):
    """robotsix-mill integration over the agent-comm broker. Disabled by default.

    When enabled, the chat agent gains a tool that forwards natural-language
    requests to the mill's board manager (``board-manager-robotsix-mill``) over
    the broker and relays its reply — so a user can have the mill track/do
    development work from chat. Mirrors the cost-analyst → board pattern.

    Attributes:
        enabled: Master switch. Requires the ``broker`` extra (robotsix-agent-comm).
        broker_host: Broker hostname (the shared agent-comm broker).
        broker_port: Broker port (443 for the public TLS endpoint).
        broker_scheme: ``https`` (TLS) or ``http``.
        broker_token: This agent's bearer token, registered on the broker.
            Required when enabled.
        agent_id: This agent's id on the broker.
        board_manager_id: Recipient agent id — the mill's NL board manager.
        repo_id: Optional repo to scope requests to; empty lets the board manager
            choose the target repo from the conversation.
        timeout: Per-request timeout (seconds); generous, the recipient is an LLM.

    """

    enabled: bool = False
    broker_host: str = "ai-broker.robotsix.net"
    broker_port: int = 443
    broker_scheme: str = "https"
    broker_token: str = ""
    agent_id: str = "robotsix-chat"
    board_manager_id: str = "board-manager-robotsix-mill"
    repo_id: str = ""
    timeout: float = 240.0


class ConversationSettings(BaseModel):
    """Multi-turn conversation continuity for the browser chat.

    The server keys conversations by a per-browser ``client_id`` (sent with each
    message). Messages within ``idle_reset_seconds`` of the previous one share a
    conversation: the prior turns are fed back to the agent and all spans are
    grouped under one trace session. After that idle window a fresh conversation
    starts — a new trace session with empty history.

    Attributes:
        idle_reset_seconds: Idle gap (seconds) after which the next message
            starts a new conversation. Default ``1800`` (30 minutes).
        max_history_turns: Most recent user/assistant turns kept per
            conversation and replayed to the agent (bounds prompt size).
        max_conversations: Maximum number of distinct clients tracked at once
            (LRU-evicted); bounds the in-memory store.

    """

    idle_reset_seconds: int = 1800
    max_history_turns: int = 20
    max_conversations: int = 1000


class Settings(BaseModel):
    """Application settings, resolved from defaults → YAML → environment.

    The LLM is configured the robotsix-llmio way — pick a capability
    ``model_level`` and llmio resolves the provider + model for that level
    (from its baked default :class:`~robotsix_llmio.config.TierLevelConfig`).

    Attributes:
        llmio_model_level: Capability level — ``1`` (cheapest/fastest), ``2``,
            or ``3`` (most capable). The level encodes the provider + model:
            by default levels 1-2 use ``openrouter`` and level 3 uses
            ``claudeSDK``/``opus``.
        llmio_api_key: Provider API key, forwarded to llmio when the chosen
            level's provider needs one (e.g. ``openrouter``); unused
            by keyless providers like ``claudeSDK``.
        agent_instruction: System instruction handed to the LLM agent.
        server_host: Host address the chat SSE server binds to.
        server_port: Port the chat SSE server listens on.
        log_level: Python logging level name.
        cors_allow_origins: Origins allowed to call /chat cross-origin
            (empty = none; ``["*"]`` = any). Only needed when the browser
            UI is hosted on a different origin than the server.
        correlation_id_header: HTTP header name used for the correlation /
            request-id (both inbound and outbound). Default ``X-Request-ID``.
        auth: HTTP Basic Auth settings gating the UI and ``/chat``.

    """

    llmio_model_level: int = 3
    llmio_api_key: str = ""
    agent_instruction: str = "You are a helpful assistant."
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    log_level: str = "INFO"
    cors_allow_origins: list[str] = Field(default_factory=list)
    correlation_id_header: str = "X-Request-ID"
    auth: AuthSettings = Field(default_factory=AuthSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    mill: MillSettings = Field(default_factory=MillSettings)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)

    def model_post_init(self, __context: Any) -> None:
        """Validate fields that cannot be expressed via simple type annotations."""
        if self.llmio_model_level not in _VALID_MODEL_LEVELS:
            raise ValueError(
                f"llmio.model_level must be one of {sorted(_VALID_MODEL_LEVELS)}, "
                f"got {self.llmio_model_level!r}"
            )
        # The keyless Claude SDK provider (level 3) needs no API key;
        # key-bearing providers (e.g. openrouter, levels 1-2) require one.
        if level_needs_api_key(self.llmio_model_level) and not self.llmio_api_key:
            raise ValueError(
                f"llmio.api_key must be set for model_level "
                f"{self.llmio_model_level} (its provider needs a key) — provide "
                "it via LLMIO_API_KEY, a .env file, or the `llmio.api_key` field "
                "of your config file (or use model_level 3, which is keyless)"
            )
        if self.auth.enabled and not self.auth.password:
            raise ValueError(
                "auth.password must be set when auth is enabled — provide it "
                "via AUTH_PASSWORD or the `auth.password` field of your config file"
            )
        if self.memory.enabled:
            if not self.memory.llm.api_key:
                raise ValueError(
                    "memory.llm.api_key must be set when memory is enabled — "
                    "provide it via MEMORY_LLM_API_KEY or the `memory.llm.api_key` "
                    "field of your config file"
                )
            if not self.memory.embedding.endpoint:
                raise ValueError(
                    "memory.embedding.endpoint must be set when memory is enabled "
                    "(e.g. http://host:11434/v1) — provide it via "
                    "MEMORY_EMBEDDING_ENDPOINT or the config file"
                )
        if self.mill.enabled:
            if not self.mill.broker_token:
                raise ValueError(
                    "mill.broker_token must be set when mill is enabled — provide "
                    "it via MILL_BROKER_TOKEN or the `mill.broker_token` config field"
                )
            if not self.mill.broker_host:
                raise ValueError(
                    "mill.broker_host must be set when mill is enabled — provide it "
                    "via MILL_BROKER_HOST or the config file"
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
        raw: dict[str, Any] = {
            k: v
            for k, v in flat.items()
            if k not in ("auth", "memory", "mill", "conversation")
        }
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

        env_override("correlation_id_header", "CORRELATION_ID_HEADER")

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

        memory_raw = _build_memory_raw(flat.get("memory"))
        if memory_raw:
            raw["memory"] = memory_raw

        mill_raw = _build_mill_raw(flat.get("mill"))
        if mill_raw:
            raw["mill"] = mill_raw

        conversation_raw = _build_conversation_raw(flat.get("conversation"))
        if conversation_raw:
            raw["conversation"] = conversation_raw

        return cls(**raw)


def _build_memory_raw(yaml_memory: Any) -> dict[str, Any]:
    """Overlay ``MEMORY_*`` env vars onto the YAML ``memory`` subtree.

    Returns a nested dict (with ``llm`` / ``embedding`` sub-dicts) ready to be
    parsed into :class:`MemorySettings`, or an empty dict when nothing is set.
    """
    memory_raw: dict[str, Any] = dict(yaml_memory or {})
    llm_raw: dict[str, Any] = dict(memory_raw.get("llm") or {})
    embed_raw: dict[str, Any] = dict(memory_raw.get("embedding") or {})

    def env_set(target: dict[str, Any], field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            target[field] = value

    enabled = os.getenv("MEMORY_ENABLED")
    if enabled is not None:
        memory_raw["enabled"] = _parse_bool(enabled)
    env_set(memory_raw, "data_dir", "MEMORY_DATA_DIR")
    env_set(memory_raw, "recall_search_type", "MEMORY_RECALL_SEARCH_TYPE")

    env_set(llm_raw, "provider", "MEMORY_LLM_PROVIDER")
    env_set(llm_raw, "model", "MEMORY_LLM_MODEL")
    env_set(llm_raw, "endpoint", "MEMORY_LLM_ENDPOINT")
    env_set(llm_raw, "api_key", "MEMORY_LLM_API_KEY")

    env_set(embed_raw, "provider", "MEMORY_EMBEDDING_PROVIDER")
    env_set(embed_raw, "model", "MEMORY_EMBEDDING_MODEL")
    env_set(embed_raw, "endpoint", "MEMORY_EMBEDDING_ENDPOINT")
    env_set(embed_raw, "api_key", "MEMORY_EMBEDDING_API_KEY")
    env_set(embed_raw, "huggingface_tokenizer", "MEMORY_EMBEDDING_TOKENIZER")

    dims = os.getenv("MEMORY_EMBEDDING_DIMENSIONS")
    if dims is not None:
        try:
            embed_raw["dimensions"] = int(dims)
        except ValueError:
            raise ValueError(
                f"MEMORY_EMBEDDING_DIMENSIONS must be an integer, got {dims!r}"
            ) from None

    if llm_raw:
        memory_raw["llm"] = llm_raw
    if embed_raw:
        memory_raw["embedding"] = embed_raw
    return memory_raw


def _build_mill_raw(yaml_mill: Any) -> dict[str, Any]:
    """Overlay ``MILL_*`` env vars onto the YAML ``mill`` subtree.

    Returns a dict ready to parse into :class:`MillSettings`, or empty when
    nothing is set.
    """
    mill_raw: dict[str, Any] = dict(yaml_mill or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            mill_raw[field] = value

    enabled = os.getenv("MILL_ENABLED")
    if enabled is not None:
        mill_raw["enabled"] = _parse_bool(enabled)
    env_set("broker_host", "MILL_BROKER_HOST")
    env_set("broker_scheme", "MILL_BROKER_SCHEME")
    env_set("broker_token", "MILL_BROKER_TOKEN")
    env_set("agent_id", "MILL_AGENT_ID")
    env_set("board_manager_id", "MILL_BOARD_MANAGER_ID")
    env_set("repo_id", "MILL_REPO_ID")

    port_str = os.getenv("MILL_BROKER_PORT")
    if port_str is not None:
        try:
            mill_raw["broker_port"] = int(port_str)
        except ValueError:
            raise ValueError(
                f"MILL_BROKER_PORT must be an integer, got {port_str!r}"
            ) from None

    timeout_str = os.getenv("MILL_TIMEOUT")
    if timeout_str is not None:
        try:
            mill_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"MILL_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return mill_raw


def _build_conversation_raw(yaml_conversation: Any) -> dict[str, Any]:
    """Overlay ``CONVERSATION_*`` env vars onto the YAML ``conversation`` subtree.

    Returns a dict ready to parse into :class:`ConversationSettings`, or empty
    when nothing is set.
    """
    conversation_raw: dict[str, Any] = dict(yaml_conversation or {})

    def env_int(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is None:
            return
        try:
            conversation_raw[field] = int(value)
        except ValueError:
            raise ValueError(f"{env_name} must be an integer, got {value!r}") from None

    env_int("idle_reset_seconds", "CONVERSATION_IDLE_RESET_SECONDS")
    env_int("max_history_turns", "CONVERSATION_MAX_HISTORY_TURNS")
    env_int("max_conversations", "CONVERSATION_MAX_CONVERSATIONS")

    return conversation_raw


def _load_dotenv() -> None:
    """Load a ``.env`` file into the environment if python-dotenv is present."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover — python-dotenv is a required dep
        logger.debug("python-dotenv not installed; skipping .env loading")

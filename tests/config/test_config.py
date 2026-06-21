"""Tests for the layered configuration system (defaults → YAML → env)."""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_chat.config import (
    AuthSettings,
    MemoryEmbeddingSettings,
    MemoryLlmSettings,
    MemorySettings,
    MillSettings,
    Settings,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wipe_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all config-related env vars so tests start from a clean slate."""
    for name in (
        "LLMIO_MODEL_LEVEL",
        "LLMIO_API_KEY",
        "AGENT_INSTRUCTION",
        "SERVER_HOST",
        "SERVER_PORT",
        "LOG_LEVEL",
        "CORS_ALLOW_ORIGINS",
        "CORRELATION_ID_HEADER",
        "AUTH_ENABLED",
        "AUTH_USERNAME",
        "AUTH_PASSWORD",
        "CHAT_CONFIG_PATH",
        "MEMORY_ENABLED",
        "MEMORY_DATA_DIR",
        "MEMORY_RECALL_SEARCH_TYPE",
        "MEMORY_LLM_PROVIDER",
        "MEMORY_LLM_MODEL",
        "MEMORY_LLM_ENDPOINT",
        "MEMORY_LLM_API_KEY",
        "MEMORY_EMBEDDING_PROVIDER",
        "MEMORY_EMBEDDING_MODEL",
        "MEMORY_EMBEDDING_ENDPOINT",
        "MEMORY_EMBEDDING_API_KEY",
        "MEMORY_EMBEDDING_TOKENIZER",
        "MEMORY_EMBEDDING_DIMENSIONS",
        "MILL_ENABLED",
        "MILL_BROKER_HOST",
        "MILL_BROKER_PORT",
        "MILL_BROKER_SCHEME",
        "MILL_BROKER_TOKEN",
        "MILL_AGENT_ID",
        "MILL_BOARD_MANAGER_ID",
        "MILL_REPO_ID",
        "MILL_TIMEOUT",
        "CONVERSATION_IDLE_RESET_SECONDS",
        "CONVERSATION_MAX_HISTORY_TURNS",
        "CONVERSATION_MAX_CONVERSATIONS",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional fields fall back to their documented defaults."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.llmio_model_level == 3
    assert settings.llmio_api_key == ""
    assert settings.server_host == "127.0.0.1"
    assert settings.server_port == 8000
    assert settings.log_level == "INFO"
    assert settings.agent_instruction == "You are a helpful assistant."


def test_log_level_default() -> None:
    """Explicit check that ``log_level`` defaults to ``"INFO"``."""
    assert Settings().log_level == "INFO"


# ---------------------------------------------------------------------------
# Model level + API key validation
# ---------------------------------------------------------------------------


def test_default_level_is_keyless() -> None:
    """The default level (3 → claude-sdk) is keyless — constructs with no key."""
    settings = Settings()
    assert settings.llmio_model_level == 3
    assert settings.llmio_api_key == ""


def test_key_bearing_level_requires_api_key() -> None:
    """A key-bearing level (1 → openrouter) without a key raises ``ValueError``."""
    with pytest.raises(ValueError, match="api_key"):
        Settings(llmio_model_level=1)


def test_key_bearing_level_with_key_ok() -> None:
    """Level 1 constructs fine with a key."""
    settings = Settings(llmio_model_level=1, llmio_api_key="sk-x")
    assert settings.llmio_model_level == 1
    assert settings.llmio_api_key == "sk-x"  # pragma: allowlist secret


def test_invalid_model_level_raises() -> None:
    """A model_level outside 1-3 is rejected."""
    with pytest.raises(ValueError, match="model_level"):
        Settings(llmio_model_level=4)


def test_key_required_via_from_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``from_env()`` raises when a key-bearing level has no key."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLMIO_MODEL_LEVEL", "2")

    with pytest.raises(ValueError, match="api_key"):
        Settings.from_env()


# ---------------------------------------------------------------------------
# Loading from environment variables
# ---------------------------------------------------------------------------


def test_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Settings.from_env()`` picks up values from ``os.environ``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLMIO_MODEL_LEVEL", "2")
    monkeypatch.setenv("LLMIO_API_KEY", "sk-env-test")
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("SERVER_PORT", "9090")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings.from_env()

    assert settings.llmio_model_level == 2
    assert settings.llmio_api_key == "sk-env-test"  # pragma: allowlist secret
    assert settings.server_host == "0.0.0.0"
    assert settings.server_port == 9090
    assert settings.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# Integer coercion
# ---------------------------------------------------------------------------


def test_server_port_coerced_to_int(monkeypatch: pytest.MonkeyPatch) -> None:
    """A string ``SERVER_PORT`` is coerced to ``int``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("SERVER_PORT", "8080")

    settings = Settings.from_env()

    assert settings.server_port == 8080
    assert isinstance(settings.server_port, int)


def test_server_port_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``SERVER_PORT`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("SERVER_PORT", "twelve")

    with pytest.raises(ValueError, match="SERVER_PORT"):
        Settings.from_env()


def test_model_level_invalid_int_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``LLMIO_MODEL_LEVEL`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLMIO_MODEL_LEVEL", "high")

    with pytest.raises(ValueError, match="LLMIO_MODEL_LEVEL"):
        Settings.from_env()


# ---------------------------------------------------------------------------
# .env file loading
# ---------------------------------------------------------------------------


def test_dotenv_file_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Values from a ``.env`` file are picked up by ``from_env()``."""
    _wipe_env_vars(monkeypatch)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLMIO_MODEL_LEVEL=2\n"
        "LLMIO_API_KEY=sk-dotenv-test\n"
        "SERVER_HOST=0.0.0.0\n"
        "SERVER_PORT=3000\n"
    )

    # Load the explicit file before from_env() runs; from_env() will
    # call load_dotenv() again (harmless), but the env vars are
    # already present.
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=str(env_file))

    settings = Settings.from_env()

    assert settings.llmio_model_level == 2
    assert settings.llmio_api_key == "sk-dotenv-test"  # pragma: allowlist secret
    assert settings.server_host == "0.0.0.0"
    assert settings.server_port == 3000


# ---------------------------------------------------------------------------
# Auth + agent instruction
# ---------------------------------------------------------------------------


def test_auth_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth is off by default."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.auth.enabled is False
    assert settings.auth.username == "admin"


def test_auth_enabled_without_password_raises() -> None:
    """Enabling auth without a password is rejected at construction."""
    with pytest.raises(ValueError, match="auth.password"):
        Settings(auth=AuthSettings(enabled=True))


def test_auth_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AUTH_*`` env vars populate the nested auth settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_USERNAME", "ops")
    monkeypatch.setenv("AUTH_PASSWORD", "s3cret")

    settings = Settings.from_env()

    assert settings.auth.enabled is True
    assert settings.auth.username == "ops"
    assert settings.auth.password == "s3cret"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# YAML config file loading
# ---------------------------------------------------------------------------


def test_load_from_yaml_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``Settings.load()`` reads nested values from a YAML config file."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "llmio:\n"
        "  model_level: 2\n"
        "  api_key: sk-yaml\n"
        "agent:\n"
        "  instruction: Be terse.\n"
        "server:\n"
        "  host: 0.0.0.0\n"
        "  port: 9000\n"
        "  cors_allow_origins: ['https://ui.example.com']\n"
        "auth:\n"
        "  enabled: true\n"
        "  username: ops\n"
        "  password: hunter2\n"
    )

    settings = Settings.load(config_path=config_file)

    assert settings.llmio_model_level == 2
    assert settings.llmio_api_key == "sk-yaml"  # pragma: allowlist secret
    assert settings.agent_instruction == "Be terse."
    assert settings.server_host == "0.0.0.0"
    assert settings.server_port == 9000
    assert settings.cors_allow_origins == ["https://ui.example.com"]
    assert settings.auth.enabled is True
    assert settings.auth.username == "ops"
    assert settings.auth.password == "hunter2"  # pragma: allowlist secret


def test_load_claude_sdk_yaml_needs_no_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A keyless level (3 → claude-sdk) loads without any api_key."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("llmio:\n  model_level: 3\n")

    settings = Settings.load(config_path=config_file)

    assert settings.llmio_model_level == 3
    assert settings.llmio_api_key == ""


def test_env_overrides_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Environment variables win over YAML values, field-by-field."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "llmio:\n  model_level: 1\n  api_key: sk-yaml\nserver:\n  port: 9000\n"
    )
    monkeypatch.setenv("LLMIO_API_KEY", "sk-env")
    monkeypatch.setenv("SERVER_PORT", "1234")

    settings = Settings.load(config_path=config_file)

    # Overridden by env...
    assert settings.llmio_api_key == "sk-env"  # pragma: allowlist secret
    assert settings.server_port == 1234
    # ...but the un-overridden YAML value survives.
    assert settings.llmio_model_level == 1


def test_load_via_config_path_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``CHAT_CONFIG_PATH`` selects the YAML file when no arg is given."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "elsewhere.yaml"
    config_file.write_text("llmio:\n  model_level: 3\n")
    monkeypatch.setenv("CHAT_CONFIG_PATH", str(config_file))

    settings = Settings.load()

    assert settings.llmio_model_level == 3


def test_load_missing_explicit_file_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit config path that does not exist raises ``FileNotFoundError``."""
    _wipe_env_vars(monkeypatch)

    with pytest.raises(FileNotFoundError):
        Settings.load(config_path="/nonexistent/chat.local.yaml")


def test_load_empty_string_skips_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """``config_path=""`` loads from the environment only (no file)."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLMIO_MODEL_LEVEL", "3")

    settings = Settings.load(config_path="")

    assert settings.llmio_model_level == 3


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def test_memory_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory is off by default, with the validated robotsix defaults present."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.memory.enabled is False
    assert settings.memory.data_dir == ".data/cognee"
    assert settings.memory.recall_search_type == "GRAPH_COMPLETION"
    assert settings.memory.llm.model == "openrouter/deepseek/deepseek-v4-flash"
    assert settings.memory.embedding.provider == "openai_compatible"
    assert settings.memory.embedding.dimensions == 1024


def test_memory_enabled_requires_llm_key() -> None:
    """Enabling memory without an extraction-LLM key is rejected."""
    with pytest.raises(ValueError, match="memory.llm.api_key"):
        Settings(
            memory=MemorySettings(
                enabled=True,
                embedding=MemoryEmbeddingSettings(endpoint="http://box:11434/v1"),
            )
        )


def test_memory_enabled_requires_embedding_endpoint() -> None:
    """Enabling memory without an embedding endpoint is rejected."""
    with pytest.raises(ValueError, match="memory.embedding.endpoint"):
        Settings(
            memory=MemorySettings(
                enabled=True,
                llm=MemoryLlmSettings(api_key="sk-or-x"),  # pragma: allowlist secret
            )
        )


def test_memory_enabled_with_key_and_endpoint_ok() -> None:
    """Memory constructs once both required fields are present."""
    settings = Settings(
        memory=MemorySettings(
            enabled=True,
            llm=MemoryLlmSettings(api_key="sk-or-x"),  # pragma: allowlist secret
            embedding=MemoryEmbeddingSettings(endpoint="http://box:11434/v1"),
        )
    )
    assert settings.memory.enabled is True


def test_memory_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MEMORY_*`` env vars populate the nested memory settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("MEMORY_LLM_API_KEY", "sk-or-env")
    monkeypatch.setenv("MEMORY_EMBEDDING_ENDPOINT", "http://box:11434/v1")
    monkeypatch.setenv("MEMORY_EMBEDDING_DIMENSIONS", "768")

    settings = Settings.from_env()

    assert settings.memory.enabled is True
    assert settings.memory.llm.api_key == "sk-or-env"  # pragma: allowlist secret
    assert settings.memory.embedding.endpoint == "http://box:11434/v1"
    assert settings.memory.embedding.dimensions == 768


def test_memory_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``MEMORY_*`` env vars win over nested YAML, field-by-field."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "memory:\n"
        "  enabled: true\n"
        "  llm:\n"
        "    api_key: sk-yaml\n"
        "    model: yaml-model\n"
        "  embedding:\n"
        "    endpoint: http://yaml:11434/v1\n"
    )
    monkeypatch.setenv("MEMORY_LLM_API_KEY", "sk-env")
    monkeypatch.setenv("MEMORY_EMBEDDING_ENDPOINT", "http://env:11434/v1")

    settings = Settings.load(config_path=config_file)

    assert settings.memory.enabled is True
    assert settings.memory.llm.api_key == "sk-env"  # pragma: allowlist secret
    assert settings.memory.embedding.endpoint == "http://env:11434/v1"
    # Un-overridden YAML value survives.
    assert settings.memory.llm.model == "yaml-model"


def test_memory_dimensions_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``MEMORY_EMBEDDING_DIMENSIONS`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MEMORY_EMBEDDING_DIMENSIONS", "lots")

    with pytest.raises(ValueError, match="MEMORY_EMBEDDING_DIMENSIONS"):
        Settings.from_env()


# ---------------------------------------------------------------------------
# Mill (broker integration)
# ---------------------------------------------------------------------------


def test_mill_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mill integration is off by default, with broker defaults present."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.mill.enabled is False
    assert settings.mill.broker_host == "ai-broker.robotsix.net"
    assert settings.mill.broker_port == 443
    assert settings.mill.agent_id == "robotsix-chat"
    assert settings.mill.board_manager_id == "board-manager-robotsix-mill"
    assert settings.mill.repo_id == ""


def test_mill_enabled_requires_token() -> None:
    """Enabling the mill without a broker token is rejected."""
    with pytest.raises(ValueError, match="mill.broker_token"):
        Settings(mill=MillSettings(enabled=True))


def test_mill_enabled_with_token_ok() -> None:
    """The mill constructs once a broker token is present."""
    settings = Settings(mill=MillSettings(enabled=True, broker_token="tok"))
    assert settings.mill.enabled is True


def test_mill_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MILL_*`` env vars populate the nested mill settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MILL_ENABLED", "true")
    monkeypatch.setenv("MILL_BROKER_TOKEN", "sek")
    monkeypatch.setenv("MILL_BROKER_PORT", "8443")
    monkeypatch.setenv("MILL_REPO_ID", "robotsix-chat")

    settings = Settings.from_env()

    assert settings.mill.enabled is True
    assert settings.mill.broker_token == "sek"  # pragma: allowlist secret
    assert settings.mill.broker_port == 8443
    assert settings.mill.repo_id == "robotsix-chat"


def test_mill_port_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``MILL_BROKER_PORT`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MILL_BROKER_PORT", "https")

    with pytest.raises(ValueError, match="MILL_BROKER_PORT"):
        Settings.from_env()


# ---------------------------------------------------------------------------
# Conversation settings
# ---------------------------------------------------------------------------


def test_conversation_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Conversation continuity defaults to a 30-minute idle reset."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.conversation.idle_reset_seconds == 1800
    assert settings.conversation.max_history_turns == 20
    assert settings.conversation.max_conversations == 1000


def test_conversation_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CONVERSATION_*`` env vars populate the nested conversation settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("CONVERSATION_IDLE_RESET_SECONDS", "600")
    monkeypatch.setenv("CONVERSATION_MAX_HISTORY_TURNS", "8")
    monkeypatch.setenv("CONVERSATION_MAX_CONVERSATIONS", "50")

    settings = Settings.from_env()

    assert settings.conversation.idle_reset_seconds == 600
    assert settings.conversation.max_history_turns == 8
    assert settings.conversation.max_conversations == 50


def test_conversation_idle_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``CONVERSATION_IDLE_RESET_SECONDS`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("CONVERSATION_IDLE_RESET_SECONDS", "soon")

    with pytest.raises(ValueError, match="CONVERSATION_IDLE_RESET_SECONDS"):
        Settings.from_env()

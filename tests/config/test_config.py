"""Tests for the layered configuration system (defaults → YAML → env)."""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_chat.config import (
    AuthSettings,
    BoardReaderSettings,
    CalendarSettings,
    ComponentAgentSettings,
    ComponentClientSettings,
    DiagnosticsSettings,
    MailSettings,
    MemoryEmbeddingSettings,
    MemoryLlmSettings,
    MemorySettings,
    MillSettings,
    PendingQuestionsSettings,
    RefDocsSettings,
    SelfReviewSettings,
    Settings,
    VersionCheckSettings,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wipe_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all config-related env vars so tests start from a clean slate."""
    for name in (
        "LLMIO_MODEL_LEVEL",
        "LLMIO_API_KEY",
        "LLMIO_SUBAGENT_MODEL",
        "LLMIO_CHECK_LOOP_MODEL",
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
        "REFDOCS_ENABLED",
        "REFDOCS_REPOS",
        "REFDOCS_REF",
        "REFDOCS_GITHUB_TOKEN",
        "REFDOCS_BASE_URL",
        "REFDOCS_TIMEOUT",
        "IDLE_TIMEOUT_MINUTES",
        "MAX_BACKGROUND_TASKS",
        "MAX_CHECK_LOOPS",
        "MIN_CHECK_LOOP_INTERVAL_SECONDS",
        "KNOWLEDGE_ENABLED",
        "KNOWLEDGE_PATH",
        "BOARD_READER_ENABLED",
        "BOARD_READER_API_BASE_URL",
        "BOARD_READER_API_TOKEN",
        "BOARD_READER_TIMEOUT",
        "BOARD_READER_CACHE_TTL",
        "DIAGNOSTICS_ENABLED",
        "DIAGNOSTICS_STORE_PATH",
        "DIAGNOSTICS_PROPOSALS_PATH",
        "DIAGNOSTICS_EFFECTIVENESS_PATH",
        "DIAGNOSTICS_RECURRENCE_THRESHOLD",
        "DIAGNOSTICS_RECURRENCE_WINDOW_DAYS",
        "DIAGNOSTICS_OBSERVATION_WINDOW_DAYS",
        "CALENDAR_ENABLED",
        "CALENDAR_BROKER_HOST",
        "CALENDAR_BROKER_PORT",
        "CALENDAR_BROKER_SCHEME",
        "CALENDAR_BROKER_TOKEN",
        "CALENDAR_AGENT_ID",
        "CALENDAR_CALENDAR_AGENT_ID",
        "CALENDAR_TIMEOUT",
        "CALENDAR_CACHE_TTL",
        "COMPONENT_AGENT_ENABLED",
        "COMPONENT_AGENT_BROKER_HOST",
        "COMPONENT_AGENT_BROKER_PORT",
        "COMPONENT_AGENT_BROKER_SCHEME",
        "COMPONENT_AGENT_BROKER_TOKEN",
        "COMPONENT_AGENT_AGENT_ID",
        "COMPONENT_AGENT_TIMEOUT",
        "COMPONENT_CLIENT_ENABLED",
        "COMPONENT_CLIENT_BROKER_HOST",
        "COMPONENT_CLIENT_BROKER_PORT",
        "COMPONENT_CLIENT_BROKER_SCHEME",
        "COMPONENT_CLIENT_BROKER_TOKEN",
        "COMPONENT_CLIENT_AGENT_ID",
        "COMPONENT_CLIENT_TIMEOUT",
        "MAIL_ENABLED",
        "MAIL_API_BASE_URL",
        "MAIL_API_TOKEN",
        "MAIL_TIMEOUT",
        "MAX_IMAGES_PER_MESSAGE",
        "MAX_IMAGE_BYTES",
        "PENDING_QUESTIONS_ENABLED",
        "ALLOWED_IMAGE_MEDIA_TYPES",
        "SELF_REVIEW_ENABLED",
        "SELF_REVIEW_RECENT_ACTIVITY_LIMIT",
        "VERSION_CHECK_ENABLED",
        "VERSION_CHECK_REPO",
        "VERSION_CHECK_GITHUB_TOKEN",
        "VERSION_CHECK_BASE_URL",
        "VERSION_CHECK_TIMEOUT",
        "VERSION_CHECK_CACHE_TTL",
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
    assert settings.agent_instruction.startswith("You are a helpful assistant.")


def test_log_level_default() -> None:
    """Explicit check that ``log_level`` defaults to ``"INFO"``."""
    assert Settings().log_level == "INFO"


# ---------------------------------------------------------------------------
# Model level + API key validation
# ---------------------------------------------------------------------------


def test_default_level_is_keyless() -> None:
    """The default level (3 → claudeSDK) is keyless — constructs with no key."""
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
    """A keyless level (3 → claudeSDK) loads without any api_key."""
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
    assert settings.conversation.max_history_turns == 50
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


# ---------------------------------------------------------------------------
# Refdocs (reference-docs tool)
# ---------------------------------------------------------------------------


def test_refdocs_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refdocs is off by default, with sensible defaults present."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.refdocs.enabled is False
    assert settings.refdocs.repos == []
    assert settings.refdocs.ref == "main"
    assert settings.refdocs.github_token == ""
    assert settings.refdocs.base_url == "https://api.github.com"
    assert settings.refdocs.timeout == 30.0


def test_refdocs_enabled_without_repos_raises() -> None:
    """Enabling refdocs without any repos is rejected."""
    with pytest.raises(ValueError, match="refdocs.repos"):
        Settings(refdocs=RefDocsSettings(enabled=True))


def test_refdocs_enabled_with_repos_ok() -> None:
    """Refdocs constructs once repos are present."""
    settings = Settings(
        refdocs=RefDocsSettings(enabled=True, repos=["org/board-workflow"])
    )
    assert settings.refdocs.enabled is True
    assert settings.refdocs.repos == ["org/board-workflow"]


def test_refdocs_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``REFDOCS_*`` env vars populate the nested refdocs settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("REFDOCS_ENABLED", "true")
    monkeypatch.setenv("REFDOCS_REPOS", "org/repo-a, org/repo-b")
    monkeypatch.setenv("REFDOCS_REF", "develop")
    monkeypatch.setenv("REFDOCS_GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("REFDOCS_BASE_URL", "https://ghe.example.com/api/v3")
    monkeypatch.setenv("REFDOCS_TIMEOUT", "15.5")

    settings = Settings.from_env()

    assert settings.refdocs.enabled is True
    assert settings.refdocs.repos == ["org/repo-a", "org/repo-b"]
    assert settings.refdocs.ref == "develop"
    assert settings.refdocs.github_token == "ghp_test"  # pragma: allowlist secret
    assert settings.refdocs.base_url == "https://ghe.example.com/api/v3"
    assert settings.refdocs.timeout == 15.5


def test_refdocs_timeout_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``REFDOCS_TIMEOUT`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("REFDOCS_TIMEOUT", "slow")

    with pytest.raises(ValueError, match="REFDOCS_TIMEOUT"):
        Settings.from_env()


# ---------------------------------------------------------------------------
# Knowledge (writable knowledge base)
# ---------------------------------------------------------------------------


def test_knowledge_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Knowledge is on by default, with sensible defaults present."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.knowledge.enabled is True
    assert settings.knowledge.path == ".data/knowledge.json"


def test_knowledge_disabled_ok() -> None:
    """Knowledge can be disabled explicitly — no extra requirements."""
    from robotsix_chat.config import KnowledgeSettings

    settings = Settings(knowledge=KnowledgeSettings(enabled=False))
    assert settings.knowledge.enabled is False


def test_knowledge_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``KNOWLEDGE_*`` env vars populate the knowledge settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")
    monkeypatch.setenv("KNOWLEDGE_PATH", ".data/test_knowledge.json")

    settings = Settings.from_env()

    assert settings.knowledge.enabled is False
    assert settings.knowledge.path == ".data/test_knowledge.json"


def test_knowledge_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``KNOWLEDGE_*`` env vars win over YAML, field-by-field."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "knowledge:\n  enabled: true\n  path: .data/yaml_knowledge.json\n"
    )
    monkeypatch.setenv("KNOWLEDGE_PATH", ".data/env_knowledge.json")

    settings = Settings.load(config_path=config_file)

    assert settings.knowledge.enabled is True  # from YAML
    assert settings.knowledge.path == ".data/env_knowledge.json"  # env wins


# ---------------------------------------------------------------------------
# Pending questions
# ---------------------------------------------------------------------------


def test_pending_questions_disabled_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PENDING_QUESTIONS_ENABLED=false`` disables the pending-questions feature."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("PENDING_QUESTIONS_ENABLED", "false")

    settings = Settings.from_env()

    assert settings.pending_questions.enabled is False


# ---------------------------------------------------------------------------
# Idle timeout
# ---------------------------------------------------------------------------


def test_idle_timeout_default() -> None:
    """``idle_timeout_minutes`` defaults to 30."""
    settings = Settings()
    assert settings.idle_timeout_minutes == 30


def test_idle_timeout_from_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``server.idle_timeout_minutes`` in YAML overrides the default."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("server:\n  idle_timeout_minutes: 15\n")

    settings = Settings.load(config_path=config_file)

    assert settings.idle_timeout_minutes == 15


def test_idle_timeout_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``IDLE_TIMEOUT_MINUTES`` env var overrides the default."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("IDLE_TIMEOUT_MINUTES", "10")

    settings = Settings.from_env()

    assert settings.idle_timeout_minutes == 10


def test_idle_timeout_env_override_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Env var ``IDLE_TIMEOUT_MINUTES`` wins over YAML value."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("server:\n  idle_timeout_minutes: 25\n")
    monkeypatch.setenv("IDLE_TIMEOUT_MINUTES", "5")

    settings = Settings.load(config_path=config_file)

    assert settings.idle_timeout_minutes == 5


def test_idle_timeout_env_non_integer_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer ``IDLE_TIMEOUT_MINUTES`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("IDLE_TIMEOUT_MINUTES", "five")

    with pytest.raises(ValueError, match="IDLE_TIMEOUT_MINUTES"):
        Settings.from_env()


def test_idle_timeout_negative_raises() -> None:
    """A negative ``idle_timeout_minutes`` is rejected by ``model_post_init``."""
    with pytest.raises(ValueError, match="idle_timeout_minutes"):
        Settings(idle_timeout_minutes=-1)


def test_idle_timeout_zero_allowed() -> None:
    """``idle_timeout_minutes = 0`` is valid (disables the feature)."""
    settings = Settings(idle_timeout_minutes=0)
    assert settings.idle_timeout_minutes == 0


# ---------------------------------------------------------------------------
# max_background_tasks
# ---------------------------------------------------------------------------


def test_max_background_tasks_default() -> None:
    """``max_background_tasks`` defaults to 5."""
    settings = Settings()
    assert settings.max_background_tasks == 5


def test_max_background_tasks_from_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``server.max_background_tasks`` in YAML overrides the default."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("server:\n  max_background_tasks: 3\n")

    settings = Settings.load(config_path=config_file)

    assert settings.max_background_tasks == 3


def test_max_background_tasks_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MAX_BACKGROUND_TASKS`` env var overrides the default."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MAX_BACKGROUND_TASKS", "10")

    settings = Settings.from_env()

    assert settings.max_background_tasks == 10


def test_max_background_tasks_env_override_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Env var ``MAX_BACKGROUND_TASKS`` wins over YAML value."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("server:\n  max_background_tasks: 8\n")
    monkeypatch.setenv("MAX_BACKGROUND_TASKS", "2")

    settings = Settings.load(config_path=config_file)

    assert settings.max_background_tasks == 2


def test_max_background_tasks_env_non_integer_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer ``MAX_BACKGROUND_TASKS`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MAX_BACKGROUND_TASKS", "many")

    with pytest.raises(ValueError, match="MAX_BACKGROUND_TASKS"):
        Settings.from_env()


def test_max_background_tasks_zero_raises() -> None:
    """``max_background_tasks = 0`` is rejected by ``model_post_init``."""
    with pytest.raises(ValueError, match="max_background_tasks"):
        Settings(max_background_tasks=0)


def test_max_background_tasks_one_allowed() -> None:
    """``max_background_tasks = 1`` is valid."""
    settings = Settings(max_background_tasks=1)
    assert settings.max_background_tasks == 1


# ---------------------------------------------------------------------------
# max_check_loops
# ---------------------------------------------------------------------------


def test_max_check_loops_default() -> None:
    """``max_check_loops`` defaults to 5."""
    settings = Settings()
    assert settings.max_check_loops == 5


def test_max_check_loops_from_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``server.max_check_loops`` in YAML overrides the default."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("server:\n  max_check_loops: 3\n")

    settings = Settings.load(config_path=config_file)

    assert settings.max_check_loops == 3


def test_max_check_loops_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MAX_CHECK_LOOPS`` env var overrides the default."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MAX_CHECK_LOOPS", "10")

    settings = Settings.from_env()

    assert settings.max_check_loops == 10


def test_max_check_loops_env_override_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Env var ``MAX_CHECK_LOOPS`` wins over YAML value."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("server:\n  max_check_loops: 8\n")
    monkeypatch.setenv("MAX_CHECK_LOOPS", "2")

    settings = Settings.load(config_path=config_file)

    assert settings.max_check_loops == 2


def test_max_check_loops_env_non_integer_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer ``MAX_CHECK_LOOPS`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MAX_CHECK_LOOPS", "many")

    with pytest.raises(ValueError, match="MAX_CHECK_LOOPS"):
        Settings.from_env()


def test_max_check_loops_zero_raises() -> None:
    """``max_check_loops = 0`` is rejected by ``model_post_init``."""
    with pytest.raises(ValueError, match="max_check_loops"):
        Settings(max_check_loops=0)


def test_max_check_loops_one_allowed() -> None:
    """``max_check_loops = 1`` is valid."""
    settings = Settings(max_check_loops=1)
    assert settings.max_check_loops == 1


# ---------------------------------------------------------------------------
# min_check_loop_interval_seconds
# ---------------------------------------------------------------------------


def test_min_check_loop_interval_seconds_default() -> None:
    """``min_check_loop_interval_seconds`` defaults to 60.0."""
    settings = Settings()
    assert settings.min_check_loop_interval_seconds == 60.0


def test_min_check_loop_interval_seconds_from_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``server.min_check_loop_interval_seconds`` in YAML overrides the default."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("server:\n  min_check_loop_interval_seconds: 30.0\n")

    settings = Settings.load(config_path=config_file)

    assert settings.min_check_loop_interval_seconds == 30.0


def test_min_check_loop_interval_seconds_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MIN_CHECK_LOOP_INTERVAL_SECONDS`` env var overrides the default."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MIN_CHECK_LOOP_INTERVAL_SECONDS", "120.5")

    settings = Settings.from_env()

    assert settings.min_check_loop_interval_seconds == 120.5


def test_min_check_loop_interval_seconds_env_override_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Env var ``MIN_CHECK_LOOP_INTERVAL_SECONDS`` wins over YAML value."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("server:\n  min_check_loop_interval_seconds: 90.0\n")
    monkeypatch.setenv("MIN_CHECK_LOOP_INTERVAL_SECONDS", "15.0")

    settings = Settings.load(config_path=config_file)

    assert settings.min_check_loop_interval_seconds == 15.0


def test_min_check_loop_interval_seconds_env_non_float_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-float ``MIN_CHECK_LOOP_INTERVAL_SECONDS`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MIN_CHECK_LOOP_INTERVAL_SECONDS", "fast")

    with pytest.raises(ValueError, match="MIN_CHECK_LOOP_INTERVAL_SECONDS"):
        Settings.from_env()


def test_min_check_loop_interval_seconds_zero_raises() -> None:
    """``min_check_loop_interval_seconds = 0.0`` is rejected by ``model_post_init``."""
    with pytest.raises(ValueError, match="min_check_loop_interval_seconds"):
        Settings(min_check_loop_interval_seconds=0.0)


def test_min_check_loop_interval_seconds_negative_raises() -> None:
    """``min_check_loop_interval_seconds = -5.0`` is rejected."""
    with pytest.raises(ValueError, match="min_check_loop_interval_seconds"):
        Settings(min_check_loop_interval_seconds=-5.0)


def test_min_check_loop_interval_seconds_one_allowed() -> None:
    """``min_check_loop_interval_seconds = 1.0`` is valid."""
    settings = Settings(min_check_loop_interval_seconds=1.0)
    assert settings.min_check_loop_interval_seconds == 1.0


# ---------------------------------------------------------------------------
# subagent_model
# ---------------------------------------------------------------------------


def test_subagent_model_default() -> None:
    """``subagent_model`` defaults to ``"sonnet"``."""
    settings = Settings()
    assert settings.subagent_model == "sonnet"


def test_subagent_model_explicit() -> None:
    """``subagent_model`` can be set to ``"haiku"``."""
    settings = Settings(subagent_model="haiku")
    assert settings.subagent_model == "haiku"


def test_subagent_model_null_disables_downgrade() -> None:
    """``subagent_model=None`` disables the downgrade (None stored)."""
    settings = Settings(subagent_model=None)
    assert settings.subagent_model is None


def test_subagent_model_from_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``llmio.subagent_model`` in YAML overrides the default."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("llmio:\n  subagent_model: haiku\n")

    settings = Settings.load(config_path=config_file)

    assert settings.subagent_model == "haiku"


def test_subagent_model_yaml_null_stores_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``llmio.subagent_model: null`` in YAML results in ``None``."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("llmio:\n  subagent_model: null\n")

    settings = Settings.load(config_path=config_file)

    assert settings.subagent_model is None


def test_subagent_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``LLMIO_SUBAGENT_MODEL`` env var overrides the default."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLMIO_SUBAGENT_MODEL", "haiku")

    settings = Settings.from_env()

    assert settings.subagent_model == "haiku"


def test_subagent_model_env_empty_string_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``LLMIO_SUBAGENT_MODEL`` is treated as ``None`` (no override)."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLMIO_SUBAGENT_MODEL", "")

    settings = Settings.from_env()

    assert settings.subagent_model is None


def test_subagent_model_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Env ``LLMIO_SUBAGENT_MODEL`` wins over YAML ``llmio.subagent_model``."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("llmio:\n  subagent_model: sonnet\n")
    monkeypatch.setenv("LLMIO_SUBAGENT_MODEL", "haiku")

    settings = Settings.load(config_path=config_file)

    assert settings.subagent_model == "haiku"


# ---------------------------------------------------------------------------
# check_loop_model
# ---------------------------------------------------------------------------


def test_check_loop_model_default() -> None:
    """``check_loop_model`` defaults to ``"haiku"``."""
    settings = Settings()
    assert settings.check_loop_model == "haiku"


def test_check_loop_model_explicit() -> None:
    """``check_loop_model`` can be set to ``"sonnet"``."""
    settings = Settings(check_loop_model="sonnet")
    assert settings.check_loop_model == "sonnet"


def test_check_loop_model_null_disables_downgrade() -> None:
    """``check_loop_model=None`` disables the downgrade (None stored)."""
    settings = Settings(check_loop_model=None)
    assert settings.check_loop_model is None


def test_check_loop_model_from_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``llmio.check_loop_model`` in YAML overrides the default."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("llmio:\n  check_loop_model: sonnet\n")

    settings = Settings.load(config_path=config_file)

    assert settings.check_loop_model == "sonnet"


def test_check_loop_model_yaml_null_stores_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``llmio.check_loop_model: null`` in YAML results in ``None``."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("llmio:\n  check_loop_model: null\n")

    settings = Settings.load(config_path=config_file)

    assert settings.check_loop_model is None


def test_check_loop_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``LLMIO_CHECK_LOOP_MODEL`` env var overrides the default."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLMIO_CHECK_LOOP_MODEL", "sonnet")

    settings = Settings.from_env()

    assert settings.check_loop_model == "sonnet"


def test_check_loop_model_env_empty_string_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``LLMIO_CHECK_LOOP_MODEL`` is treated as ``None`` (no override)."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLMIO_CHECK_LOOP_MODEL", "")

    settings = Settings.from_env()

    assert settings.check_loop_model is None


def test_check_loop_model_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Env ``LLMIO_CHECK_LOOP_MODEL`` wins over YAML ``llmio.check_loop_model``."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text("llmio:\n  check_loop_model: haiku\n")
    monkeypatch.setenv("LLMIO_CHECK_LOOP_MODEL", "sonnet")

    settings = Settings.load(config_path=config_file)

    assert settings.check_loop_model == "sonnet"


# ---------------------------------------------------------------------------
# Mail (broker integration)
# ---------------------------------------------------------------------------


def test_mail_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mail integration is off by default, with direct-HTTP defaults present."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.mail.enabled is False
    assert settings.mail.api_base_url == "http://127.0.0.1:8077"
    assert settings.mail.api_token == ""
    assert settings.mail.timeout == 30.0


def test_mail_enabled_ok() -> None:
    """Mail constructs with just enabled=True (no required broker fields)."""
    settings = Settings(mail=MailSettings(enabled=True))
    assert settings.mail.enabled is True


def test_mail_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MAIL_*`` env vars populate the nested mail settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MAIL_ENABLED", "true")
    monkeypatch.setenv("MAIL_API_BASE_URL", "https://mail.example.com:9000")
    monkeypatch.setenv("MAIL_API_TOKEN", "sek")

    settings = Settings.from_env()

    assert settings.mail.enabled is True
    assert settings.mail.api_base_url == "https://mail.example.com:9000"
    assert settings.mail.api_token == "sek"  # pragma: allowlist secret


def test_mail_timeout_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``MAIL_TIMEOUT`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MAIL_TIMEOUT", "slow")

    with pytest.raises(ValueError, match="MAIL_TIMEOUT"):
        Settings.from_env()


def test_mail_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``MAIL_*`` env vars win over YAML, field-by-field."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "mail:\n  enabled: true\n  api_base_url: http://yaml-host:8077\n"
        "  api_token: yaml-tok\n"
    )
    monkeypatch.setenv("MAIL_API_BASE_URL", "http://env-host:9000")
    monkeypatch.setenv("MAIL_API_TOKEN", "env-tok")

    settings = Settings.load(config_path=config_file)

    assert settings.mail.enabled is True
    assert settings.mail.api_base_url == "http://env-host:9000"
    assert settings.mail.api_token == "env-tok"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# Calendar (broker integration)
# ---------------------------------------------------------------------------


def test_calendar_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calendar integration is off by default, with broker defaults present."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.calendar.enabled is False
    assert settings.calendar.broker_host == "ai-broker.robotsix.net"
    assert settings.calendar.broker_port == 443
    assert settings.calendar.broker_scheme == "https"
    assert settings.calendar.agent_id == "robotsix-chat"
    assert settings.calendar.calendar_agent_id == "robotsix-calendar"
    assert settings.calendar.timeout == 240.0


def test_calendar_enabled_requires_token() -> None:
    """Enabling calendar without a broker token is rejected."""
    with pytest.raises(ValueError, match="calendar.broker_token"):
        Settings(calendar=CalendarSettings(enabled=True))


def test_calendar_enabled_with_token_ok() -> None:
    """Calendar constructs once a broker token is present."""
    settings = Settings(calendar=CalendarSettings(enabled=True, broker_token="tok"))
    assert settings.calendar.enabled is True


def test_calendar_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CALENDAR_*`` env vars populate the nested calendar settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("CALENDAR_ENABLED", "true")
    monkeypatch.setenv("CALENDAR_BROKER_TOKEN", "sek")
    monkeypatch.setenv("CALENDAR_BROKER_PORT", "8443")
    monkeypatch.setenv("CALENDAR_CALENDAR_AGENT_ID", "my-cal-agent")
    monkeypatch.setenv("CALENDAR_CACHE_TTL", "120.0")

    settings = Settings.from_env()

    assert settings.calendar.enabled is True
    assert settings.calendar.broker_token == "sek"  # pragma: allowlist secret
    assert settings.calendar.broker_port == 8443
    assert settings.calendar.calendar_agent_id == "my-cal-agent"
    assert settings.calendar.cache_ttl == 120.0


def test_calendar_port_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``CALENDAR_BROKER_PORT`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("CALENDAR_BROKER_PORT", "https")

    with pytest.raises(ValueError, match="CALENDAR_BROKER_PORT"):
        Settings.from_env()


def test_calendar_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``CALENDAR_*`` env vars win over YAML, field-by-field."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "calendar:\n"
        "  enabled: true\n"
        "  broker_host: yaml-host\n"
        "  broker_token: yaml-tok\n"
    )
    monkeypatch.setenv("CALENDAR_BROKER_HOST", "env-host")
    monkeypatch.setenv("CALENDAR_BROKER_TOKEN", "env-tok")

    settings = Settings.load(config_path=config_file)

    assert settings.calendar.enabled is True
    assert settings.calendar.broker_host == "env-host"
    assert settings.calendar.broker_token == "env-tok"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# Board reader (direct HTTP board API)
# ---------------------------------------------------------------------------


def test_board_reader_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Board reader is off by default, with sensible defaults present."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.board_reader.enabled is False
    assert settings.board_reader.api_base_url == "http://127.0.0.1:8077"
    assert settings.board_reader.api_token == ""
    assert settings.board_reader.timeout == 30.0
    assert settings.board_reader.cache_ttl == 60.0


def test_board_reader_enabled_ok() -> None:
    """Board reader constructs with no extra requirements beyond enabled."""
    settings = Settings(board_reader=BoardReaderSettings(enabled=True))
    assert settings.board_reader.enabled is True


def test_board_reader_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BOARD_READER_*`` env vars populate the nested board reader settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("BOARD_READER_ENABLED", "true")
    monkeypatch.setenv("BOARD_READER_API_BASE_URL", "http://board:8077")
    monkeypatch.setenv("BOARD_READER_API_TOKEN", "bsek")
    monkeypatch.setenv("BOARD_READER_TIMEOUT", "15.5")
    monkeypatch.setenv("BOARD_READER_CACHE_TTL", "120.0")

    settings = Settings.from_env()

    assert settings.board_reader.enabled is True
    assert settings.board_reader.api_base_url == "http://board:8077"
    assert settings.board_reader.api_token == "bsek"  # pragma: allowlist secret
    assert settings.board_reader.timeout == 15.5
    assert settings.board_reader.cache_ttl == 120.0


def test_board_reader_timeout_invalid_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric ``BOARD_READER_TIMEOUT`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("BOARD_READER_TIMEOUT", "slow")

    with pytest.raises(ValueError, match="BOARD_READER_TIMEOUT"):
        Settings.from_env()


def test_board_reader_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``BOARD_READER_*`` env vars win over YAML, field-by-field."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "board_reader:\n"
        "  enabled: true\n"
        "  api_base_url: http://yaml:8077\n"
        "  timeout: 10.0\n"
    )
    monkeypatch.setenv("BOARD_READER_API_BASE_URL", "http://env:8077")

    settings = Settings.load(config_path=config_file)

    assert settings.board_reader.enabled is True
    assert settings.board_reader.api_base_url == "http://env:8077"
    assert settings.board_reader.timeout == 10.0  # un-overridden YAML


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_diagnostics_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Diagnostics is on by default, with sensible defaults present."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.diagnostics.enabled is True
    assert settings.diagnostics.store_path == ".data/diagnostics.json"
    assert settings.diagnostics.proposals_path == ".data/fix_proposals.json"
    assert settings.diagnostics.effectiveness_path == (
        ".data/diagnostics_effectiveness.json"
    )
    assert settings.diagnostics.recurrence_threshold == 3
    assert settings.diagnostics.recurrence_window_days == 30
    assert settings.diagnostics.observation_window_days == 30


def test_diagnostics_disabled_ok() -> None:
    """Diagnostics can be explicitly disabled."""
    settings = Settings(diagnostics=DiagnosticsSettings(enabled=False))
    assert settings.diagnostics.enabled is False


def test_diagnostics_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``DIAGNOSTICS_*`` env vars populate the nested diagnostics settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("DIAGNOSTICS_ENABLED", "false")
    monkeypatch.setenv("DIAGNOSTICS_STORE_PATH", "/custom/diag.json")
    monkeypatch.setenv("DIAGNOSTICS_EFFECTIVENESS_PATH", "/custom/eff.json")
    monkeypatch.setenv("DIAGNOSTICS_RECURRENCE_THRESHOLD", "5")
    monkeypatch.setenv("DIAGNOSTICS_RECURRENCE_WINDOW_DAYS", "60")
    monkeypatch.setenv("DIAGNOSTICS_OBSERVATION_WINDOW_DAYS", "45")

    settings = Settings.from_env()

    assert settings.diagnostics.enabled is False
    assert settings.diagnostics.store_path == "/custom/diag.json"
    assert settings.diagnostics.effectiveness_path == "/custom/eff.json"
    assert settings.diagnostics.recurrence_threshold == 5
    assert settings.diagnostics.recurrence_window_days == 60
    assert settings.diagnostics.observation_window_days == 45


def test_diagnostics_recurrence_threshold_invalid_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer ``DIAGNOSTICS_RECURRENCE_THRESHOLD`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("DIAGNOSTICS_RECURRENCE_THRESHOLD", "fast")

    with pytest.raises(ValueError, match="DIAGNOSTICS_RECURRENCE_THRESHOLD"):
        Settings.from_env()


def test_diagnostics_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``DIAGNOSTICS_*`` env vars win over YAML, field-by-field."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "diagnostics:\n"
        "  enabled: true\n"
        "  store_path: /yaml/diag.json\n"
        "  recurrence_threshold: 10\n"
    )
    monkeypatch.setenv("DIAGNOSTICS_RECURRENCE_THRESHOLD", "7")

    settings = Settings.load(config_path=config_file)

    assert settings.diagnostics.enabled is True
    assert settings.diagnostics.recurrence_threshold == 7  # env overrides YAML
    assert settings.diagnostics.store_path == "/yaml/diag.json"  # un-overridden YAML


# ---------------------------------------------------------------------------
# Self review
# ---------------------------------------------------------------------------


def test_self_review_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Self review is off by default, with sensible defaults present."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.self_review.enabled is False
    assert settings.self_review.recent_activity_limit == 20


def test_self_review_disabled_ok() -> None:
    """Self review can be disabled explicitly — no extra requirements."""
    settings = Settings(self_review=SelfReviewSettings(enabled=False))
    assert settings.self_review.enabled is False


def test_self_review_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SELF_REVIEW_*`` env vars populate the nested self review settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("SELF_REVIEW_ENABLED", "true")
    monkeypatch.setenv("SELF_REVIEW_RECENT_ACTIVITY_LIMIT", "50")

    settings = Settings.from_env()

    assert settings.self_review.enabled is True
    assert settings.self_review.recent_activity_limit == 50


def test_self_review_limit_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``SELF_REVIEW_RECENT_ACTIVITY_LIMIT`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("SELF_REVIEW_RECENT_ACTIVITY_LIMIT", "many")

    with pytest.raises(ValueError, match="SELF_REVIEW_RECENT_ACTIVITY_LIMIT"):
        Settings.from_env()


def test_self_review_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``SELF_REVIEW_*`` env vars win over YAML, field-by-field."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "self_review:\n  enabled: false\n  recent_activity_limit: 10\n"
    )
    monkeypatch.setenv("SELF_REVIEW_RECENT_ACTIVITY_LIMIT", "30")

    settings = Settings.load(config_path=config_file)

    assert settings.self_review.enabled is False  # from YAML
    assert settings.self_review.recent_activity_limit == 30  # env wins


# ---------------------------------------------------------------------------
# Version check
# ---------------------------------------------------------------------------


def test_version_check_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Version check is off by default, with sensible defaults present."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.version_check.enabled is False
    assert settings.version_check.repo == ""
    assert settings.version_check.github_token == ""
    assert settings.version_check.base_url == "https://api.github.com"
    assert settings.version_check.timeout == 30.0
    assert settings.version_check.cache_ttl == 300.0


def test_version_check_enabled_requires_repo() -> None:
    """Enabling version check without a repo is rejected."""
    with pytest.raises(ValueError, match="version_check.repo"):
        Settings(version_check=VersionCheckSettings(enabled=True))


def test_version_check_enabled_with_repo_ok() -> None:
    """Version check constructs once a repo is present."""
    settings = Settings(
        version_check=VersionCheckSettings(enabled=True, repo="robotsix/robotsix-chat")
    )
    assert settings.version_check.enabled is True


def test_version_check_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``VERSION_CHECK_*`` env vars populate the nested version check settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("VERSION_CHECK_ENABLED", "true")
    monkeypatch.setenv("VERSION_CHECK_REPO", "org/repo")
    monkeypatch.setenv("VERSION_CHECK_GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("VERSION_CHECK_BASE_URL", "https://ghe.example.com/api/v3")
    monkeypatch.setenv("VERSION_CHECK_TIMEOUT", "15.0")
    monkeypatch.setenv("VERSION_CHECK_CACHE_TTL", "600.0")

    settings = Settings.from_env()

    assert settings.version_check.enabled is True
    assert settings.version_check.repo == "org/repo"
    assert settings.version_check.github_token == "ghp_test"  # pragma: allowlist secret
    assert settings.version_check.base_url == "https://ghe.example.com/api/v3"
    assert settings.version_check.timeout == 15.0
    assert settings.version_check.cache_ttl == 600.0


def test_version_check_timeout_invalid_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric ``VERSION_CHECK_TIMEOUT`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("VERSION_CHECK_TIMEOUT", "slow")

    with pytest.raises(ValueError, match="VERSION_CHECK_TIMEOUT"):
        Settings.from_env()


def test_version_check_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``VERSION_CHECK_*`` env vars win over YAML, field-by-field."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "version_check:\n  enabled: true\n  repo: yaml/repo\n  timeout: 10.0\n"
    )
    monkeypatch.setenv("VERSION_CHECK_REPO", "env/repo")

    settings = Settings.load(config_path=config_file)

    assert settings.version_check.enabled is True
    assert settings.version_check.repo == "env/repo"
    assert settings.version_check.timeout == 10.0  # un-overridden YAML


# ---------------------------------------------------------------------------
# Component agent (broker responder)
# ---------------------------------------------------------------------------


def test_component_agent_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Component agent responder is off by default, with broker defaults."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.component_agent.enabled is False
    assert settings.component_agent.broker_host == "ai-broker.robotsix.net"
    assert settings.component_agent.broker_port == 443
    assert settings.component_agent.broker_scheme == "https"
    assert settings.component_agent.agent_id == "robotsix-chat-component"
    assert settings.component_agent.timeout == 240.0


def test_component_agent_enabled_requires_token() -> None:
    """Enabling component agent without a broker token is rejected."""
    with pytest.raises(ValueError, match="component_agent.broker_token"):
        Settings(component_agent=ComponentAgentSettings(enabled=True))


def test_component_agent_enabled_with_token_ok() -> None:
    """Component agent constructs once a broker token is present."""
    settings = Settings(
        component_agent=ComponentAgentSettings(enabled=True, broker_token="tok")
    )
    assert settings.component_agent.enabled is True


def test_component_agent_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``COMPONENT_AGENT_*`` env vars populate the component agent settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("COMPONENT_AGENT_ENABLED", "true")
    monkeypatch.setenv("COMPONENT_AGENT_BROKER_TOKEN", "sek")
    monkeypatch.setenv("COMPONENT_AGENT_BROKER_PORT", "8443")
    monkeypatch.setenv("COMPONENT_AGENT_AGENT_ID", "my-component")

    settings = Settings.from_env()

    assert settings.component_agent.enabled is True
    assert settings.component_agent.broker_token == "sek"  # pragma: allowlist secret
    assert settings.component_agent.broker_port == 8443
    assert settings.component_agent.agent_id == "my-component"


def test_component_agent_port_invalid_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric ``COMPONENT_AGENT_BROKER_PORT`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("COMPONENT_AGENT_BROKER_PORT", "https")

    with pytest.raises(ValueError, match="COMPONENT_AGENT_BROKER_PORT"):
        Settings.from_env()


def test_component_agent_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``COMPONENT_AGENT_*`` env vars win over YAML, field-by-field."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "component_agent:\n"
        "  enabled: true\n"
        "  broker_host: yaml-host\n"
        "  broker_token: yaml-tok\n"
    )
    monkeypatch.setenv("COMPONENT_AGENT_BROKER_HOST", "env-host")
    monkeypatch.setenv("COMPONENT_AGENT_BROKER_TOKEN", "env-tok")

    settings = Settings.load(config_path=config_file)

    assert settings.component_agent.enabled is True
    assert settings.component_agent.broker_host == "env-host"
    assert (
        settings.component_agent.broker_token == "env-tok"
    )  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# Component client (broker requester)
# ---------------------------------------------------------------------------


def test_component_client_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Component client is off by default, with broker defaults present."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.component_client.enabled is False
    assert settings.component_client.broker_host == "ai-broker.robotsix.net"
    assert settings.component_client.broker_port == 443
    assert settings.component_client.broker_scheme == "https"
    assert settings.component_client.agent_id == "robotsix-chat"
    assert settings.component_client.timeout == 240.0
    assert settings.component_client.components == []


def test_component_client_enabled_requires_token() -> None:
    """Enabling component client without a broker token is rejected."""
    with pytest.raises(ValueError, match="component_client.broker_token"):
        Settings(component_client=ComponentClientSettings(enabled=True))


def test_component_client_enabled_with_token_ok() -> None:
    """Component client constructs once a broker token is present."""
    settings = Settings(
        component_client=ComponentClientSettings(enabled=True, broker_token="tok")
    )
    assert settings.component_client.enabled is True


def test_component_client_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``COMPONENT_CLIENT_*`` env vars populate the component client settings."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("COMPONENT_CLIENT_ENABLED", "true")
    monkeypatch.setenv("COMPONENT_CLIENT_BROKER_TOKEN", "sek")
    monkeypatch.setenv("COMPONENT_CLIENT_BROKER_PORT", "8443")
    monkeypatch.setenv("COMPONENT_CLIENT_AGENT_ID", "my-client")

    settings = Settings.from_env()

    assert settings.component_client.enabled is True
    assert settings.component_client.broker_token == "sek"  # pragma: allowlist secret
    assert settings.component_client.broker_port == 8443
    assert settings.component_client.agent_id == "my-client"


def test_component_client_port_invalid_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric ``COMPONENT_CLIENT_BROKER_PORT`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("COMPONENT_CLIENT_BROKER_PORT", "https")

    with pytest.raises(ValueError, match="COMPONENT_CLIENT_BROKER_PORT"):
        Settings.from_env()


def test_component_client_env_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``COMPONENT_CLIENT_*`` env vars win over YAML, field-by-field."""
    _wipe_env_vars(monkeypatch)

    config_file = tmp_path / "chat.local.yaml"
    config_file.write_text(
        "component_client:\n"
        "  enabled: true\n"
        "  broker_host: yaml-host\n"
        "  broker_token: yaml-tok\n"
    )
    monkeypatch.setenv("COMPONENT_CLIENT_BROKER_HOST", "env-host")
    monkeypatch.setenv("COMPONENT_CLIENT_BROKER_TOKEN", "env-tok")

    settings = Settings.load(config_path=config_file)

    assert settings.component_client.enabled is True
    assert settings.component_client.broker_host == "env-host"
    assert (
        settings.component_client.broker_token == "env-tok"
    )  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# Pending questions
# ---------------------------------------------------------------------------


def test_pending_questions_enabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending questions is on by default."""
    _wipe_env_vars(monkeypatch)

    settings = Settings.from_env()

    assert settings.pending_questions.enabled is True


def test_pending_questions_disabled_ok() -> None:
    """Pending questions can be disabled explicitly — no extra requirements."""
    settings = Settings(pending_questions=PendingQuestionsSettings(enabled=False))
    assert settings.pending_questions.enabled is False


# ---------------------------------------------------------------------------
# Top-level image attachment fields
# ---------------------------------------------------------------------------


def test_max_images_per_message_default() -> None:
    """``max_images_per_message`` defaults to 8."""
    settings = Settings()
    assert settings.max_images_per_message == 8


def test_max_images_per_message_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MAX_IMAGES_PER_MESSAGE`` env var overrides the default."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MAX_IMAGES_PER_MESSAGE", "16")

    settings = Settings.from_env()

    assert settings.max_images_per_message == 16


def test_max_images_per_message_env_non_integer_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer ``MAX_IMAGES_PER_MESSAGE`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MAX_IMAGES_PER_MESSAGE", "many")

    with pytest.raises(ValueError, match="MAX_IMAGES_PER_MESSAGE"):
        Settings.from_env()


def test_max_image_bytes_default() -> None:
    """``max_image_bytes`` defaults to 5_242_880 (5 MiB)."""
    settings = Settings()
    assert settings.max_image_bytes == 5_242_880


def test_max_image_bytes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MAX_IMAGE_BYTES`` env var overrides the default."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MAX_IMAGE_BYTES", "2097152")

    settings = Settings.from_env()

    assert settings.max_image_bytes == 2097152


def test_max_image_bytes_env_non_integer_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer ``MAX_IMAGE_BYTES`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("MAX_IMAGE_BYTES", "big")

    with pytest.raises(ValueError, match="MAX_IMAGE_BYTES"):
        Settings.from_env()


def test_allowed_image_media_types_default() -> None:
    """``allowed_image_media_types`` defaults to four common image types."""
    settings = Settings()
    assert settings.allowed_image_media_types == [
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    ]


def test_allowed_image_media_types_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ALLOWED_IMAGE_MEDIA_TYPES`` env var (comma-separated) overrides default."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("ALLOWED_IMAGE_MEDIA_TYPES", "image/png, image/webp")

    settings = Settings.from_env()

    assert settings.allowed_image_media_types == ["image/png", "image/webp"]

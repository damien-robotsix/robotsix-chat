"""Tests for the configuration system (JSON-based, no env overlay)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from robotsix_chat.config import (
    CalendarSettings,
    ComponentAgentSettings,
    ComponentClientSettings,
    ComponentTarget,
    DiagnosticsSettings,
    MailSettings,
    MemoryEmbeddingSettings,
    MemoryLlmSettings,
    MemorySettings,
    MillSettings,
    RefDocsSettings,
    SelfReviewSettings,
    Settings,
    SubsessionsSettings,
    VersionCheckSettings,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config_json(tmp_path: Path, overrides: dict | None = None) -> Path:
    """Write a minimal valid config.json to *tmp_path* and return its path."""
    data: dict = {
        "llmio_model_level": 3,
    }
    if overrides:
        data.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults() -> None:
    """Optional fields fall back to their documented defaults."""
    settings = Settings()

    assert settings.llmio_model_level == 3
    assert settings.llmio_api_key.get_secret_value() == ""
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
    """The default level (3) is keyless — constructs with no key."""
    settings = Settings()
    assert settings.llmio_model_level == 3
    assert settings.llmio_api_key.get_secret_value() == ""


def test_key_bearing_level_requires_api_key() -> None:
    """A key-bearing level (1 → openrouter) without a key raises ``ValueError``."""
    with pytest.raises(ValueError, match="api_key"):
        Settings(llmio_model_level=1)


def test_key_bearing_level_with_key_ok() -> None:
    """Level 1 constructs fine with a key."""
    settings = Settings(llmio_model_level=1, llmio_api_key=SecretStr("sk-x"))
    assert settings.llmio_model_level == 1
    # pragma: allowlist secret
    assert settings.llmio_api_key.get_secret_value() == "sk-x"


def test_invalid_model_level_raises() -> None:
    """A model_level outside llmio's tiers (1-4) is rejected."""
    with pytest.raises(ValueError, match="model_level"):
        Settings(llmio_model_level=5)


def test_level_3_is_keyless() -> None:
    """Level 3 constructs with no key."""
    settings = Settings(llmio_model_level=3)
    assert settings.llmio_model_level == 3


def test_level_4_is_keyless() -> None:
    """Level 4 (frontier, claudeSDK) constructs with no key."""
    settings = Settings(llmio_model_level=4)
    assert settings.llmio_model_level == 4


# ---------------------------------------------------------------------------
# Loading from JSON config file
# ---------------------------------------------------------------------------


def test_load_from_json_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``Settings.load()`` reads values from a JSON config file."""
    config_path = _write_config_json(
        tmp_path,
        {
            "llmio_model_level": 2,
            "llmio_api_key": "sk-json",  # pragma: allowlist secret
            "server_host": "0.0.0.0",
            "server_port": 9000,
            "log_level": "DEBUG",
        },
    )
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(config_path))

    settings = Settings.load()

    assert settings.llmio_model_level == 2
    # pragma: allowlist secret
    assert settings.llmio_api_key.get_secret_value() == "sk-json"
    assert settings.server_host == "0.0.0.0"
    assert settings.server_port == 9000
    assert settings.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def test_memory_disabled_by_default() -> None:
    """Memory is off by default, with the validated robotsix defaults present."""
    settings = Settings()

    assert settings.memory.enabled is False
    assert settings.memory.data_dir == "/data/cognee"
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
                llm=MemoryLlmSettings(
                    api_key=SecretStr("sk-or-x")  # pragma: allowlist secret
                ),
            )
        )


def test_memory_enabled_with_key_and_endpoint_ok() -> None:
    """Memory constructs once both required fields are present."""
    settings = Settings(
        memory=MemorySettings(
            enabled=True,
            llm=MemoryLlmSettings(
                api_key=SecretStr("sk-or-x")  # pragma: allowlist secret
            ),
            embedding=MemoryEmbeddingSettings(endpoint="http://box:11434/v1"),
        )
    )
    assert settings.memory.enabled is True


def test_memory_from_json_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory settings can be loaded from JSON config file."""
    config_path = _write_config_json(
        tmp_path,
        {
            "memory": {
                "enabled": True,
                "llm": {"api_key": "sk-or-env"},  # pragma: allowlist secret
                "embedding": {"endpoint": "http://box:11434/v1", "dimensions": 768},
            },
        },
    )
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(config_path))

    settings = Settings.load()

    assert settings.memory.enabled is True
    # pragma: allowlist secret
    assert settings.memory.llm.api_key.get_secret_value() == "sk-or-env"
    assert settings.memory.embedding.endpoint == "http://box:11434/v1"
    assert settings.memory.embedding.dimensions == 768


# ---------------------------------------------------------------------------
# Mill (broker integration)
# ---------------------------------------------------------------------------


def test_mill_disabled_by_default() -> None:
    """Mill integration is off by default, with broker defaults present."""
    settings = Settings()

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
    settings = Settings(mill=MillSettings(enabled=True, broker_token=SecretStr("tok")))
    assert settings.mill.enabled is True


# ---------------------------------------------------------------------------
# Conversation settings
# ---------------------------------------------------------------------------


def test_conversation_defaults() -> None:
    """Conversation continuity defaults to a 30-minute idle reset."""
    settings = Settings()

    assert settings.conversation.idle_reset_seconds == 1800
    assert settings.conversation.max_history_turns == 50
    assert settings.conversation.max_conversations == 1000


# ---------------------------------------------------------------------------
# Refdocs (reference-docs tool)
# ---------------------------------------------------------------------------


def test_refdocs_disabled_by_default() -> None:
    """Refdocs is off by default, with sensible defaults present."""
    settings = Settings()

    assert settings.refdocs.enabled is False
    assert settings.refdocs.repos == []
    assert settings.refdocs.ref == "main"
    assert settings.refdocs.github_token.get_secret_value() == ""
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


# ---------------------------------------------------------------------------
# Knowledge (writable knowledge base)
# ---------------------------------------------------------------------------


def test_knowledge_enabled_by_default() -> None:
    """Knowledge is on by default, with sensible defaults present."""
    settings = Settings()

    assert settings.knowledge.enabled is True
    assert settings.knowledge.path == "/data/knowledge.json"


def test_knowledge_disabled_ok() -> None:
    """Knowledge can be disabled explicitly — no extra requirements."""
    from robotsix_chat.config import KnowledgeSettings

    settings = Settings(knowledge=KnowledgeSettings(enabled=False))
    assert settings.knowledge.enabled is False


# ---------------------------------------------------------------------------
# Idle timeout
# ---------------------------------------------------------------------------


def test_idle_timeout_default() -> None:
    """``idle_timeout_minutes`` defaults to 30."""
    settings = Settings()
    assert settings.idle_timeout_minutes == 30


def test_idle_timeout_negative_raises() -> None:
    """A negative ``idle_timeout_minutes`` is rejected by ``model_post_init``."""
    with pytest.raises(ValueError, match="idle_timeout_minutes"):
        Settings(idle_timeout_minutes=-1)


def test_idle_timeout_zero_allowed() -> None:
    """``idle_timeout_minutes = 0`` is valid (disables the feature)."""
    settings = Settings(idle_timeout_minutes=0)
    assert settings.idle_timeout_minutes == 0


# ---------------------------------------------------------------------------
# Subsessions settings
# ---------------------------------------------------------------------------


def test_subsessions_defaults() -> None:
    """``subsessions`` sub-model falls back to its documented defaults."""
    settings = Settings()
    assert settings.subsessions == SubsessionsSettings()
    assert settings.subsessions.max_concurrent == 8
    assert settings.subsessions.max_depth == 3
    assert settings.subsessions.default_model_level == 3
    assert settings.subsessions.min_interval_seconds == 60.0
    assert settings.subsessions.auto_stop_no_change_runs == 5
    assert settings.subsessions.store_path == "/data/subsessions.json"
    assert settings.subsessions.transcript_max_entries == 200


def test_subsessions_max_concurrent_zero_raises() -> None:
    """``subsessions.max_concurrent = 0`` is rejected by ``model_post_init``."""
    with pytest.raises(ValueError, match="max_concurrent"):
        Settings(subsessions={"max_concurrent": 0})


def test_subsessions_max_depth_zero_raises() -> None:
    """``subsessions.max_depth = 0`` is rejected by ``model_post_init``."""
    with pytest.raises(ValueError, match="max_depth"):
        Settings(subsessions={"max_depth": 0})


def test_subsessions_default_model_level_invalid_raises() -> None:
    """``subsessions.default_model_level = 5`` is rejected."""
    with pytest.raises(ValueError, match="default_model_level"):
        Settings(subsessions={"default_model_level": 5})


def test_subsessions_min_interval_zero_raises() -> None:
    """``subsessions.min_interval_seconds = 0.0`` is rejected."""
    with pytest.raises(ValueError, match="min_interval_seconds"):
        Settings(subsessions={"min_interval_seconds": 0.0})


def test_subsessions_auto_stop_zero_raises() -> None:
    """``subsessions.auto_stop_no_change_runs = 0`` is rejected."""
    with pytest.raises(ValueError, match="auto_stop_no_change_runs"):
        Settings(subsessions={"auto_stop_no_change_runs": 0})


def test_subsessions_min_interval_one_allowed() -> None:
    """``subsessions.min_interval_seconds = 1.0`` is valid."""
    settings = Settings(subsessions={"min_interval_seconds": 1.0})
    assert settings.subsessions.min_interval_seconds == 1.0


# ---------------------------------------------------------------------------
# Mail (direct HTTP)
# ---------------------------------------------------------------------------


def test_mail_disabled_by_default() -> None:
    """Mail integration is off by default, with direct-HTTP defaults present."""
    settings = Settings()

    assert settings.mail.enabled is False
    assert settings.mail.api_base_url == "http://127.0.0.1:8077"
    assert settings.mail.api_token.get_secret_value() == ""
    assert settings.mail.timeout == 30.0


def test_mail_enabled_ok() -> None:
    """Mail constructs with just enabled=True (no required broker fields)."""
    settings = Settings(mail=MailSettings(enabled=True))
    assert settings.mail.enabled is True


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def test_calendar_disabled_by_default() -> None:
    """Calendar integration is off by default, with broker defaults."""
    settings = Settings()
    assert settings.calendar.enabled is False
    assert settings.calendar.broker_host == "ai-broker.robotsix.net"
    assert settings.calendar.broker_port == 443
    assert settings.calendar.broker_token.get_secret_value() == ""
    assert settings.calendar.calendar_agent_id == "robotsix-calendar"
    assert settings.calendar.timeout == 240.0
    assert settings.calendar.cache_ttl == 60.0


def test_calendar_enabled_requires_token() -> None:
    """Enabling calendar without a broker token is rejected."""
    with pytest.raises(ValueError, match="calendar.broker_token"):
        Settings(calendar=CalendarSettings(enabled=True))


def test_calendar_enabled_with_token_ok() -> None:
    """Calendar constructs once a broker token is present."""
    settings = Settings(
        calendar=CalendarSettings(enabled=True, broker_token=SecretStr("tok"))
    )
    assert settings.calendar.enabled is True


# ---------------------------------------------------------------------------
# Board reader
# ---------------------------------------------------------------------------


def test_board_reader_disabled_by_default() -> None:
    """Board reader is off by default, with sensible defaults present."""
    settings = Settings()
    assert settings.board_reader.enabled is False
    assert settings.board_reader.api_base_url == "http://127.0.0.1:8077"
    assert settings.board_reader.api_token.get_secret_value() == ""
    assert settings.board_reader.cache_ttl == 60.0


# ---------------------------------------------------------------------------
# Direct repo
# ---------------------------------------------------------------------------


def test_direct_repo_disabled_by_default() -> None:
    """Direct repo is off by default, with sensible defaults present."""
    settings = Settings()
    assert settings.direct_repo.enabled is False
    assert settings.direct_repo.github_app_id == ""
    assert settings.direct_repo.github_app_private_key.get_secret_value() == ""
    assert settings.direct_repo.github_app_installation_id == ""
    assert settings.direct_repo.github_api_base_url == "https://api.github.com"
    assert settings.direct_repo.board_api_base_url == "http://127.0.0.1:8077"
    assert settings.direct_repo.board_api_token.get_secret_value() == ""
    assert settings.direct_repo.timeout == 30.0


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_diagnostics_enabled_by_default() -> None:
    """Diagnostics is on by default, with sensible defaults present."""
    settings = Settings()
    assert settings.diagnostics.enabled is True
    assert settings.diagnostics.store_path == "/data/diagnostics.json"
    assert settings.diagnostics.proposals_path == "/data/fix_proposals.json"
    assert settings.diagnostics.effectiveness_path == (
        "/data/diagnostics_effectiveness.json"
    )
    assert settings.diagnostics.recurrence_threshold == 3
    assert settings.diagnostics.recurrence_window_days == 30
    assert settings.diagnostics.observation_window_days == 30


def test_diagnostics_disabled_ok() -> None:
    """Diagnostics can be explicitly disabled."""
    settings = Settings(diagnostics=DiagnosticsSettings(enabled=False))
    assert settings.diagnostics.enabled is False


# ---------------------------------------------------------------------------
# Self review
# ---------------------------------------------------------------------------


def test_self_review_disabled_by_default() -> None:
    """Self review is off by default, with sensible defaults present."""
    settings = Settings()
    assert settings.self_review.enabled is False
    assert settings.self_review.recent_activity_limit == 20


def test_self_review_disabled_ok() -> None:
    """Self review can be disabled explicitly — no extra requirements."""
    settings = Settings(self_review=SelfReviewSettings(enabled=False))
    assert settings.self_review.enabled is False


# ---------------------------------------------------------------------------
# Version check
# ---------------------------------------------------------------------------


def test_version_check_disabled_by_default() -> None:
    """Version check is off by default, with sensible defaults present."""
    settings = Settings()
    assert settings.version_check.enabled is False
    assert settings.version_check.repo == ""
    assert settings.version_check.github_token.get_secret_value() == ""
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


# ---------------------------------------------------------------------------
# Component agent (broker responder)
# ---------------------------------------------------------------------------


def test_component_agent_disabled_by_default() -> None:
    """Component agent responder is off by default, with broker defaults."""
    settings = Settings()
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
        component_agent=ComponentAgentSettings(
            enabled=True, broker_token=SecretStr("tok")
        )
    )
    assert settings.component_agent.enabled is True


# ---------------------------------------------------------------------------
# Component client (direct HTTP)
# ---------------------------------------------------------------------------


def test_component_client_disabled_by_default() -> None:
    """Component client is off by default, with no components configured."""
    settings = Settings()
    assert settings.component_client.enabled is False
    assert settings.component_client.timeout == 240.0
    assert settings.component_client.components == []


def test_component_client_enabled_ok_without_components() -> None:
    """Enabling component client without components is allowed."""
    settings = Settings(component_client=ComponentClientSettings(enabled=True))
    assert settings.component_client.enabled is True
    assert settings.component_client.components == []


def test_component_client_enabled_with_components_ok() -> None:
    """Component client constructs when components are configured."""
    settings = Settings(
        component_client=ComponentClientSettings(
            enabled=True,
            components=[ComponentTarget(base_url="http://comp-1:8090")],
        )
    )
    assert settings.component_client.enabled is True
    assert len(settings.component_client.components) == 1
    assert settings.component_client.components[0].base_url == "http://comp-1:8090"


# ---------------------------------------------------------------------------
# Top-level image attachment fields
# ---------------------------------------------------------------------------


def test_max_images_per_message_default() -> None:
    """``max_images_per_message`` defaults to 8."""
    settings = Settings()
    assert settings.max_images_per_message == 8


def test_max_image_bytes_default() -> None:
    """``max_image_bytes`` defaults to 5_242_880 (5 MiB)."""
    settings = Settings()
    assert settings.max_image_bytes == 5_242_880


def test_allowed_image_media_types_default() -> None:
    """``allowed_image_media_types`` defaults to four common image types."""
    settings = Settings()
    assert settings.allowed_image_media_types == [
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    ]


# ---------------------------------------------------------------------------
# LangfuseSettings
# ---------------------------------------------------------------------------


def test_langfuse_settings_defaults() -> None:
    """Langfuse settings have correct defaults."""
    settings = Settings()
    assert settings.langfuse.public_key.get_secret_value() == ""
    assert settings.langfuse.secret_key.get_secret_value() == ""
    assert settings.langfuse.host == "https://cloud.langfuse.com"


def test_memory_langfuse_settings_defaults() -> None:
    """Memory langfuse settings have correct defaults."""
    settings = Settings()
    assert settings.memory.langfuse.public_key.get_secret_value() == ""
    assert settings.memory.langfuse.secret_key.get_secret_value() == ""
    assert settings.memory.langfuse.host == "https://cloud.langfuse.com"

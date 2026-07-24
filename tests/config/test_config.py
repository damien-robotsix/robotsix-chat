"""Tests for the configuration system (JSON-based, no env overlay)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from robotsix_chat.config import (
    ComponentClientSettings,
    ComponentTarget,
    DiagnosticsSettings,
    MailSettings,
    MemoryEmbeddingSettings,
    MemoryLlmSettings,
    MemorySettings,
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
    assert settings.server_host == "0.0.0.0"
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
    assert settings.memory.llm.model == "openrouter/openai/gpt-5-mini"
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


# ---------------------------------------------------------------------------
# Conversation settings
# ---------------------------------------------------------------------------


def test_conversation_defaults() -> None:
    """Conversation continuity defaults to a 30-minute idle reset."""
    settings = Settings()

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
    assert settings.subsessions.default_model_level == 2
    assert settings.subsessions.min_interval_seconds == 60.0
    assert settings.subsessions.auto_stop_no_change_runs == 5
    assert settings.subsessions.max_idle_runs == 5
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


def test_subsessions_max_idle_runs_default() -> None:
    """``subsessions.max_idle_runs`` defaults to 5."""
    settings = Settings()
    assert settings.subsessions.max_idle_runs == 5


def test_subsessions_max_idle_runs_zero_allowed() -> None:
    """``subsessions.max_idle_runs = 0`` (disabled) is valid."""
    settings = Settings(subsessions={"max_idle_runs": 0})
    assert settings.subsessions.max_idle_runs == 0


def test_subsessions_max_idle_runs_negative_raises() -> None:
    """``subsessions.max_idle_runs = -1`` is rejected."""
    with pytest.raises(ValueError, match="max_idle_runs"):
        Settings(subsessions={"max_idle_runs": -1})


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


# ---------------------------------------------------------------------------
# Legacy empty-string coercion
# ---------------------------------------------------------------------------


def test_coerce_cors_allow_origins_empty_string_to_list() -> None:
    """``cors_allow_origins=""`` is coerced to ``[]``."""
    settings = Settings(cors_allow_origins="")  # type: ignore[arg-type]
    assert settings.cors_allow_origins == []


def test_coerce_allowed_image_media_types_empty_string_to_list() -> None:
    """``allowed_image_media_types=""`` is coerced to ``[]``."""
    settings = Settings(allowed_image_media_types="")  # type: ignore[arg-type]
    assert settings.allowed_image_media_types == []


def test_coerce_top_level_object_empty_string_to_dict() -> None:
    """Top-level object fields like ``memory=""`` fall back to defaults."""
    settings = Settings(memory="")  # type: ignore[arg-type]
    assert settings.memory.enabled is False
    assert settings.memory.data_dir == "/data/cognee"


def test_coerce_refdocs_empty_string_to_dict() -> None:
    """``refdocs=""`` is coerced to ``{}`` → defaults."""
    settings = Settings(refdocs="")  # type: ignore[arg-type]
    assert settings.refdocs.enabled is False
    assert settings.refdocs.repos == []


def test_coerce_component_client_empty_string_to_dict() -> None:
    """``component_client=""`` is coerced to ``{}`` → defaults."""
    settings = Settings(component_client="")  # type: ignore[arg-type]
    assert settings.component_client.enabled is False
    assert settings.component_client.components == []


def test_coerce_refdocs_repos_empty_string_to_list() -> None:
    """``refdocs.repos=""`` inside a valid refdocs dict is coerced to ``[]``."""
    settings = Settings(refdocs={"repos": ""})  # type: ignore[arg-type]
    assert settings.refdocs.repos == []


def test_coerce_component_client_components_empty_string_to_list() -> None:
    """``component_client.components=""`` is coerced to ``[]``."""
    settings = Settings(component_client={"components": ""})  # type: ignore[arg-type]
    assert settings.component_client.components == []


def test_coerce_memory_nested_empty_string_to_dict() -> None:
    """Coerce ``memory.llm=""`` and friends to ``{}`` → defaults.

    ``memory.llm=""``, ``memory.langfuse=""``, and ``memory.embedding=""``
    are each coerced to ``{}`` → defaults.
    """
    settings = Settings(
        memory={
            "llm": "",
            "langfuse": "",
            "embedding": "",
        }  # type: ignore[arg-type]
    )
    assert settings.memory.llm.model == "openrouter/openai/gpt-5-mini"
    assert settings.memory.langfuse.host == "https://cloud.langfuse.com"
    assert settings.memory.embedding.model == "bge-m3"


# ---------------------------------------------------------------------------
# JS-toString sentinel coercion ([object Object], undefined, null)
# ---------------------------------------------------------------------------


def test_coerce_object_object_sentinel_top_level_object() -> None:
    """``memory="[object Object]"`` is coerced to ``{}`` → defaults."""
    settings = Settings(memory="[object Object]")  # type: ignore[arg-type]
    assert settings.memory.enabled is False
    assert settings.memory.data_dir == "/data/cognee"


def test_coerce_object_object_sentinel_nested_memory_llm() -> None:
    """``memory.llm="[object Object]"`` is coerced to ``{}`` → defaults."""
    settings = Settings(
        memory={"llm": "[object Object]"}  # type: ignore[arg-type]
    )
    assert settings.memory.llm.model == "openrouter/openai/gpt-5-mini"


def test_coerce_object_object_sentinel_nested_memory_embedding() -> None:
    """``memory.embedding="[object Object]"`` is coerced to ``{}`` → defaults."""
    settings = Settings(
        memory={"embedding": "[object Object]"}  # type: ignore[arg-type]
    )
    assert settings.memory.embedding.model == "bge-m3"


def test_coerce_object_object_sentinel_nested_memory_langfuse() -> None:
    """``memory.langfuse="[object Object]"`` is coerced to ``{}`` → defaults."""
    settings = Settings(
        memory={"langfuse": "[object Object]"}  # type: ignore[arg-type]
    )
    assert settings.memory.langfuse.host == "https://cloud.langfuse.com"


def test_coerce_object_object_sentinel_top_level_list() -> None:
    """``cors_allow_origins="[object Object]"`` is coerced to ``[]``."""
    settings = Settings(cors_allow_origins="[object Object]")  # type: ignore[arg-type]
    assert settings.cors_allow_origins == []


def test_coerce_object_object_sentinel_nested_refdocs_repos() -> None:
    """``refdocs.repos="[object Object]"`` is coerced to ``[]``."""
    settings = Settings(refdocs={"repos": "[object Object]"})  # type: ignore[arg-type]
    assert settings.refdocs.repos == []


def test_coerce_object_object_sentinel_nested_component_client_components() -> None:
    """``component_client.components="[object Object]"`` is coerced to ``[]``."""
    settings = Settings(
        component_client={"components": "[object Object]"}  # type: ignore[arg-type]
    )
    assert settings.component_client.components == []


def test_coerce_undefined_sentinel_top_level_object() -> None:
    """``memory="undefined"`` is coerced to ``{}`` → defaults."""
    settings = Settings(memory="undefined")  # type: ignore[arg-type]
    assert settings.memory.enabled is False


def test_coerce_null_sentinel_top_level_object() -> None:
    """``memory="null"`` is coerced to ``{}`` → defaults."""
    settings = Settings(memory="null")  # type: ignore[arg-type]
    assert settings.memory.enabled is False


def test_coerce_undefined_sentinel_top_level_list() -> None:
    """``cors_allow_origins="undefined"`` is coerced to ``[]``."""
    settings = Settings(cors_allow_origins="undefined")  # type: ignore[arg-type]
    assert settings.cors_allow_origins == []


# ---------------------------------------------------------------------------
# Round-trip integrity: model_dump → model_validate
# ---------------------------------------------------------------------------


def test_roundtrip_nested_object_field_preserves_structure() -> None:
    """``model_dump()`` → ``model_validate()`` round-trips a nested object intact."""
    original = Settings()
    dumped = original.model_dump()
    reloaded = Settings.model_validate(dumped)
    assert reloaded.memory.llm.model == original.memory.llm.model
    assert reloaded.memory.llm.provider == original.memory.llm.provider
    assert reloaded.memory.llm.endpoint == original.memory.llm.endpoint
    # Whole nested dict is equal
    assert reloaded.memory.llm.model_dump() == original.memory.llm.model_dump()


def test_roundtrip_empty_array_field_preserves_structure() -> None:
    """Empty ``list`` fields round-trip as ``[]``, not ``""``."""
    original = Settings(cors_allow_origins=[])
    dumped = original.model_dump()
    reloaded = Settings.model_validate(dumped)
    assert reloaded.cors_allow_origins == []
    assert isinstance(reloaded.cors_allow_origins, list)


def test_roundtrip_empty_object_field_preserves_structure() -> None:
    """Empty ``dict`` fields round-trip as ``{}``, not ``""``."""
    # Start from defaults — memory.langfuse is an object with defaults
    original = Settings()
    dumped = original.model_dump()
    reloaded = Settings.model_validate(dumped)
    assert isinstance(reloaded.memory.langfuse, dict) or hasattr(
        reloaded.memory.langfuse, "model_dump"
    )
    # Verify it's not a string
    assert not isinstance(dumped.get("memory", {}).get("langfuse"), str)


# ---------------------------------------------------------------------------
# Unknown-key rejection (extra="forbid")
# ---------------------------------------------------------------------------


class TestUnknownKeys:
    """Unknown keys in any model raise a ``ValidationError`` (extra="forbid")."""

    def test_top_level_settings_rejects_unknown(self) -> None:
        """Typo in a top-level key (e.g. ``memry`` for ``memory``) is rejected."""
        with pytest.raises(ValidationError, match="memry"):
            Settings(memry={"enabled": True})  # type: ignore[call-arg]

    def test_nested_submodel_rejects_unknown(self) -> None:
        """Unknown key inside a nested sub-model is rejected."""
        with pytest.raises(ValidationError, match="typo_key"):
            MemorySettings(enabled=True, typo_key="value")  # type: ignore[call-arg]

    def test_list_field_model_rejects_unknown(self) -> None:
        """Unknown key inside a list-field sub-model is rejected."""
        with pytest.raises(ValidationError, match="unknown_field"):
            ComponentClientSettings(enabled=True, unknown_field=[])  # type: ignore[call-arg]

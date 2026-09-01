"""Tests for the configuration system (JSON-based, no env overlay)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from robotsix_chat.config import (
    CentralDeploySettings,
    ComponentClientSettings,
    ComponentTarget,
    DiagnosticsSettings,
    FeedbackSettings,
    FileHubToolsSettings,
    KindTurnBudget,
    MemoryEmbeddingSettings,
    MemorySettings,
    OpenRouterSettings,
    RefDocsSettings,
    SelfReviewSettings,
    Settings,
    SubsessionsSettings,
    TurnBudgetSettings,
    VersionCheckSettings,
)
from robotsix_chat.config.models import EvergoingSettings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config_json(tmp_path: Path, overrides: dict | None = None) -> Path:
    """Write a minimal valid config.json to *tmp_path* and return its path."""
    data: dict = {
        "llmio_model_level": 2,
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

    assert settings.llmio_model_level == 2
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
    """The default level (2, workhorse) is keyless — constructs with no key."""
    settings = Settings()
    assert settings.llmio_model_level == 2
    assert settings.llmio_api_key.get_secret_value() == ""


def test_no_level_requires_api_key() -> None:
    """No level needs a key at config load.

    Every level is served by the keyless Claude SDK default slot; the
    OpenRouter key only matters when provider failover routes calls to the
    keyed fallback slot.
    """
    for level in (1, 2, 3):
        settings = Settings(llmio_model_level=level)
        assert settings.llmio_model_level == level


def test_key_bearing_config_with_key_ok() -> None:
    """A configured key is kept for the failover slot."""
    settings = Settings(llmio_model_level=1, llmio_api_key=SecretStr("sk-x"))
    assert settings.llmio_model_level == 1
    # pragma: allowlist secret
    assert settings.llmio_api_key.get_secret_value() == "sk-x"


def test_invalid_model_level_raises() -> None:
    """A model_level outside llmio's levels (1-3) is rejected."""
    with pytest.raises(ValueError, match="model_level"):
        Settings(llmio_model_level=6)
    with pytest.raises(ValueError, match="model_level"):
        Settings(llmio_model_level=4)


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
    # Automatic recall is retrieval-only (no LLM hop per message); the
    # LLM-mediated GRAPH_COMPLETION moved to the on-demand search_memory tool.
    assert settings.memory.recall_search_type == "CHUNKS"
    assert settings.memory.deep_recall_search_type == "GRAPH_COMPLETION"
    assert settings.memory.deep_recall_timeout_seconds == 180.0
    assert settings.memory.llm.model == "openrouter/openai/gpt-5-nano"
    assert settings.memory.embedding.provider == "openai_compatible"
    assert settings.memory.embedding.dimensions == 1024


def test_memory_enabled_requires_llm_key() -> None:
    """Enabling memory without an extraction-LLM key is rejected."""
    with pytest.raises(ValueError, match="openrouter.keys"):
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
            memory=MemorySettings(enabled=True),
            openrouter=OpenRouterSettings(
                keys={  # pragma: allowlist secret
                    "robotsix-chat-cognee": SecretStr("sk-or-x")
                }
            ),
        )


def test_memory_enabled_with_key_and_endpoint_ok() -> None:
    """Memory constructs once both required fields are present."""
    settings = Settings(
        memory=MemorySettings(
            enabled=True,
            embedding=MemoryEmbeddingSettings(endpoint="http://box:11434/v1"),
        ),
        openrouter=OpenRouterSettings(
            keys={  # pragma: allowlist secret
                "robotsix-chat-cognee": SecretStr("sk-or-x")
            }
        ),
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
                "embedding": {"endpoint": "http://box:11434/v1", "dimensions": 768},
            },
            "openrouter": {
                "keys": {
                    "robotsix-chat-cognee": "sk-or-env"  # pragma: allowlist secret
                }
            },
        },
    )
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(config_path))

    settings = Settings.load()

    assert settings.memory.enabled is True
    # pragma: allowlist secret
    assert (
        settings.openrouter.key("robotsix-chat-cognee").get_secret_value()
        == "sk-or-env"
    )
    assert settings.memory.embedding.endpoint == "http://box:11434/v1"
    assert settings.memory.embedding.dimensions == 768


def test_memory_legacy_llm_api_key_migrates_to_openrouter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy ``memory.llm.api_key`` loads into ``openrouter.keys`` on read."""
    config_path = _write_config_json(
        tmp_path,
        {
            "memory": {
                "enabled": True,
                "llm": {"api_key": "sk-legacy"},  # pragma: allowlist secret
                "embedding": {"endpoint": "http://box:11434/v1"},
            },
        },
    )
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(config_path))

    settings = Settings.load()

    assert settings.memory.enabled is True
    # pragma: allowlist secret
    assert (
        settings.openrouter.key("robotsix-chat-cognee").get_secret_value()
        == "sk-legacy"
    )
    assert "api_key" not in settings.memory.llm.model_dump()


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
    assert settings.subsessions.auto_stop_no_change_runs == 3
    assert settings.subsessions.max_idle_runs == 15
    assert settings.subsessions.max_no_change_pauses == 3
    assert settings.subsessions.store_path == "/data/subsessions.json"
    assert settings.subsessions.transcript_max_entries == 200


def test_subsessions_turn_budget_defaults() -> None:
    """Turn budgets default to on for task/chat, off for periodic monitors."""
    tb = Settings().subsessions.turn_budget
    # task / user_chat / on_close: warn at 25, hard-stop at 40.
    assert tb.task.soft_warn_turns == 25
    assert tb.task.hard_stop_turns == 40
    assert tb.user_chat.soft_warn_turns == 25
    assert tb.user_chat.hard_stop_turns == 40
    assert tb.on_close.soft_warn_turns == 25
    assert tb.on_close.hard_stop_turns == 40
    # periodic monitors are disabled by default — they are already bounded
    # by monitor_max_model_level / run_timeout / periodic_max_total_runs and
    # are designed to stay alive for the whole life of a ticket.
    assert tb.periodic.soft_warn_turns == 0
    assert tb.periodic.hard_stop_turns == 0


def test_subsessions_turn_budget_rejects_inverted_thresholds() -> None:
    """soft_warn_turns must be less than hard_stop_turns when both are set."""
    with pytest.raises(ValidationError):
        KindTurnBudget(soft_warn_turns=40, hard_stop_turns=25)
    with pytest.raises(ValidationError):
        KindTurnBudget(soft_warn_turns=40, hard_stop_turns=40)
    # A disabled hard-stop (0) still permits a soft-warn — no ceiling to
    # invert against.
    assert KindTurnBudget(soft_warn_turns=25, hard_stop_turns=0).hard_stop_turns == 0


def test_subsessions_turn_budget_extra_keys_rejected() -> None:
    """Turn budget models reject unknown keys."""
    with pytest.raises(ValidationError):
        KindTurnBudget(soft_warn_turns=25, hard_stop_turns=40, bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        TurnBudgetSettings(task={"soft_warn_turns": 25}, bogus={})  # type: ignore[call-arg]


def test_subsessions_max_concurrent_zero_raises() -> None:
    """``subsessions.max_concurrent = 0`` is rejected by ``model_post_init``."""
    with pytest.raises(ValueError, match="max_concurrent"):
        Settings(subsessions={"max_concurrent": 0})


def test_subsessions_max_depth_zero_raises() -> None:
    """``subsessions.max_depth = 0`` is rejected by ``model_post_init``."""
    with pytest.raises(ValueError, match="max_depth"):
        Settings(subsessions={"max_depth": 0})


def test_subsessions_default_model_level_invalid_raises() -> None:
    """``subsessions.default_model_level = 6`` is rejected."""
    with pytest.raises(ValueError, match="default_model_level"):
        Settings(subsessions={"default_model_level": 6})


def test_subsessions_monitor_max_model_level_invalid_raises() -> None:
    """``subsessions.monitor_max_model_level = 6`` is rejected."""
    with pytest.raises(ValueError, match="monitor_max_model_level"):
        Settings(subsessions={"monitor_max_model_level": 6})


def test_subsessions_min_interval_zero_raises() -> None:
    """``subsessions.min_interval_seconds = 0.0`` is rejected."""
    with pytest.raises(ValueError, match="min_interval_seconds"):
        Settings(subsessions={"min_interval_seconds": 0.0})


def test_subsessions_auto_stop_zero_raises() -> None:
    """``subsessions.auto_stop_no_change_runs = 0`` is rejected."""
    with pytest.raises(ValueError, match="auto_stop_no_change_runs"):
        Settings(subsessions={"auto_stop_no_change_runs": 0})


def test_subsessions_max_idle_runs_default() -> None:
    """``subsessions.max_idle_runs`` defaults to 15."""
    settings = Settings()
    assert settings.subsessions.max_idle_runs == 15


def test_subsessions_max_idle_runs_zero_allowed() -> None:
    """``subsessions.max_idle_runs = 0`` (disabled) is valid."""
    settings = Settings(subsessions={"max_idle_runs": 0})
    assert settings.subsessions.max_idle_runs == 0


def test_subsessions_max_no_change_pauses_default() -> None:
    """``subsessions.max_no_change_pauses`` defaults to 3."""
    settings = Settings()
    assert settings.subsessions.max_no_change_pauses == 3


def test_subsessions_max_no_change_pauses_zero_allowed() -> None:
    """``subsessions.max_no_change_pauses = 0`` (disabled) is valid."""
    settings = Settings(subsessions={"max_no_change_pauses": 0})
    assert settings.subsessions.max_no_change_pauses == 0


def test_subsessions_max_no_change_pauses_negative_raises() -> None:
    """``subsessions.max_no_change_pauses = -1`` is rejected."""
    with pytest.raises(ValueError, match="max_no_change_pauses"):
        Settings(subsessions={"max_no_change_pauses": -1})


def test_subsessions_max_idle_runs_negative_raises() -> None:
    """``subsessions.max_idle_runs = -1`` is rejected."""
    with pytest.raises(ValueError, match="max_idle_runs"):
        Settings(subsessions={"max_idle_runs": -1})


def test_subsessions_min_interval_one_allowed() -> None:
    """``subsessions.min_interval_seconds = 1.0`` is valid."""
    settings = Settings(subsessions={"min_interval_seconds": 1.0})
    assert settings.subsessions.min_interval_seconds == 1.0


# ---------------------------------------------------------------------------
# Legacy deploy-plane consolidation + mail-block removal (migration)
# ---------------------------------------------------------------------------


def test_legacy_mail_block_is_dropped() -> None:
    """A deployed config still carrying the retired ``mail`` block loads."""
    settings = Settings.model_validate(
        {
            "llmio_model_level": 2,
            "mail": {
                "enabled": False,
                "api_base_url": "http://127.0.0.1:8077",
                "api_token": "",
                "timeout": 30.0,
            },
        }
    )
    # The block is gone from the model entirely.
    assert not hasattr(settings, "mail")


def test_legacy_lifecycle_base_url_migrates_to_central_deploy_url() -> None:
    """``lifecycle.base_url`` is folded into the canonical ``central_deploy.url``."""
    settings = Settings.model_validate(
        {
            "llmio_model_level": 2,
            "lifecycle": {"enabled": True, "base_url": "http://central-deploy:9000"},
        }
    )
    assert settings.central_deploy.url == "http://central-deploy:9000"


def test_legacy_per_block_deploy_api_key_migrates() -> None:
    """A per-block ``deploy_api_key`` folds into ``central_deploy.deploy_api_key``."""
    settings = Settings.model_validate(
        {
            "llmio_model_level": 2,
            "feedback": {"deploy_api_key": "legacy-secret"},  # pragma: allowlist secret
        }
    )
    assert settings.central_deploy.deploy_api_key.get_secret_value() == "legacy-secret"


def test_explicit_central_deploy_values_win_over_legacy() -> None:
    """An explicitly-set canonical value is never clobbered by a legacy copy."""
    settings = Settings.model_validate(
        {
            "llmio_model_level": 2,
            "central_deploy": {
                "url": "http://canonical:9000",
                "deploy_api_key": "canonical-secret",  # pragma: allowlist secret
            },
            "lifecycle": {"base_url": "http://legacy:1"},
            "feedback": {"deploy_api_key": "legacy-secret"},  # pragma: allowlist secret
            "github_security": {
                "deploy_api_key": "sec-secret"  # pragma: allowlist secret
            },
            "github_actions": {
                "deploy_api_key": "act-secret"  # pragma: allowlist secret
            },
        }
    )
    assert settings.central_deploy.url == "http://canonical:9000"
    assert (
        settings.central_deploy.deploy_api_key.get_secret_value() == "canonical-secret"
    )


def test_production_config_with_all_legacy_keys_loads_cleanly() -> None:
    """A copy of a deployed production config loads cleanly after migration.

    Exercises every retired path at once: the ``mail`` block, the legacy
    ``lifecycle.base_url``, and the per-block ``deploy_api_key`` copies on
    ``feedback``/``github_security``/``github_actions``. Under
    ``extra="forbid"`` these would raise ``extra_forbidden`` without the
    migration — so a clean load is the assertion.
    """
    raw = {
        "llmio_model_level": 2,
        "mail": {
            "enabled": False,
            "api_base_url": "http://127.0.0.1:8077",
            "api_token": "",
            "timeout": 30.0,
        },
        "lifecycle": {"enabled": True, "base_url": "http://central-deploy:9000"},
        "feedback": {
            "enabled": True,
            "board_url": "http://mill:8077",
            "deploy_api_key": "deploy-key",  # pragma: allowlist secret
        },
        "github_security": {
            "enabled": True,
            "deploy_api_key": "deploy-key",  # pragma: allowlist secret
        },
        "github_actions": {
            "enabled": True,
            "deploy_api_key": "deploy-key",  # pragma: allowlist secret
        },
    }

    settings = Settings.model_validate(raw)

    # Canonical sources are populated from the legacy copies.
    assert settings.central_deploy.url == "http://central-deploy:9000"
    assert settings.central_deploy.deploy_api_key.get_secret_value() == "deploy-key"
    # Retired surfaces are gone / stripped.
    assert not hasattr(settings, "mail")
    assert not hasattr(settings.feedback, "deploy_api_key")
    assert not hasattr(settings.github_security, "deploy_api_key")
    assert not hasattr(settings.github_actions, "deploy_api_key")


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
    assert settings.direct_repo.board_api_base_url == "http://mill:8077"
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


def test_self_review_enabled_by_default() -> None:
    """Self review is on by default, with sensible defaults present."""
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
    """The canonical Langfuse block defaults to a host and no projects."""
    settings = Settings()
    assert settings.langfuse.host == "https://cloud.langfuse.com"
    assert settings.langfuse.projects == {}


def test_langfuse_creds_absent_project_is_unconfigured() -> None:
    """An absent project yields empty creds rather than raising."""
    settings = Settings()
    creds = settings.langfuse.creds("robotsix-chat")
    assert creds.public_key.get_secret_value() == ""
    assert creds.secret_key.get_secret_value() == ""
    assert creds.is_configured() is False


def test_langfuse_projects_are_parsed_and_resolvable() -> None:
    """Both of this component's projects round-trip through the block."""
    settings = Settings(
        langfuse={
            "host": "https://langfuse.example.net",
            "projects": {
                "robotsix-chat": {
                    "public_key": "pk-main",
                    "secret_key": "sk-main",  # pragma: allowlist secret
                    "project_id": "cm-main",
                },
                "robotsix-chat-cognee": {
                    "public_key": "pk-mem",
                    "secret_key": "sk-mem",  # pragma: allowlist secret
                },
            },
        }  # type: ignore[arg-type]
    )
    main = settings.langfuse.creds("robotsix-chat")
    assert main.public_key.get_secret_value() == "pk-main"
    assert main.project_id == "cm-main"
    assert main.is_configured() is True
    mem = settings.langfuse.creds("robotsix-chat-cognee")
    assert mem.secret_key.get_secret_value() == "sk-mem"
    assert mem.project_id == ""


def test_langfuse_half_filled_project_is_not_configured() -> None:
    """A project missing one key half is treated as unconfigured."""
    settings = Settings(
        langfuse={"projects": {"robotsix-chat": {"public_key": "pk-only"}}}  # type: ignore[arg-type]
    )
    assert settings.langfuse.creds("robotsix-chat").is_configured() is False


def test_legacy_langfuse_keys_are_stripped_not_migrated() -> None:
    """A pre-block config loads, but its credentials are NOT carried over.

    ``extra="forbid"`` would otherwise reject the whole file and crash-loop
    the container on the first start after an image upgrade.  Per the
    standard's no-fallback rule the old values are dropped, not migrated —
    the deployment traces nothing until its config is rewritten.
    """
    settings = Settings(
        langfuse={
            "public_key": "pk-legacy",
            "secret_key": "sk-legacy",  # pragma: allowlist secret
            "host": "https://langfuse.example.net",
        }  # type: ignore[arg-type]
    )
    assert settings.langfuse.host == "https://langfuse.example.net"
    assert settings.langfuse.projects == {}
    assert settings.langfuse.creds("robotsix-chat").is_configured() is False


def test_legacy_memory_langfuse_block_is_stripped() -> None:
    """The removed ``memory.langfuse`` sub-block no longer rejects the file."""
    settings = Settings(
        memory={
            "data_dir": "/data/cognee",
            "langfuse": {
                "public_key": "pk-legacy",
                "secret_key": "sk-legacy",  # pragma: allowlist secret
                "host": "https://langfuse.example.net",
            },
        }  # type: ignore[arg-type]
    )
    assert settings.memory.data_dir == "/data/cognee"
    assert settings.memory.langfuse_project == "robotsix-chat-cognee"
    assert not hasattr(settings.memory, "langfuse")


def test_memory_langfuse_project_default() -> None:
    """Memory names its own project rather than carrying credentials."""
    settings = Settings()
    assert settings.memory.langfuse_project == "robotsix-chat-cognee"


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

    ``memory.llm=""`` and ``memory.embedding=""`` are each coerced to
    ``{}`` → defaults.
    """
    settings = Settings(
        memory={
            "llm": "",
            "embedding": "",
        }  # type: ignore[arg-type]
    )
    assert settings.memory.llm.model == "openrouter/openai/gpt-5-nano"
    assert settings.memory.embedding.model == "bge-m3"


def test_periodic_empty_sessions_stays_empty() -> None:
    """An explicit ``periodic.sessions: []`` means nothing fires.

    No hidden default preset is injected.
    """
    settings = Settings(periodic={"sessions": []})  # type: ignore[arg-type]
    assert settings.periodic.sessions == []


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
    assert settings.memory.llm.model == "openrouter/openai/gpt-5-nano"


def test_coerce_object_object_sentinel_nested_memory_embedding() -> None:
    """``memory.embedding="[object Object]"`` is coerced to ``{}`` → defaults."""
    settings = Settings(
        memory={"embedding": "[object Object]"}  # type: ignore[arg-type]
    )
    assert settings.memory.embedding.model == "bge-m3"


def test_coerce_object_object_sentinel_top_level_langfuse() -> None:
    """``langfuse="[object Object]"`` is coerced to ``{}`` → defaults."""
    settings = Settings(langfuse="[object Object]")  # type: ignore[arg-type]
    assert settings.langfuse.host == "https://cloud.langfuse.com"
    assert settings.langfuse.projects == {}


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
    # Start from defaults — langfuse is an object with defaults
    original = Settings()
    dumped = original.model_dump()
    reloaded = Settings.model_validate(dumped)
    assert isinstance(reloaded.langfuse, dict) or hasattr(
        reloaded.langfuse, "model_dump"
    )
    # Verify it's not a string, and the projects map survives as a dict
    assert not isinstance(dumped.get("langfuse"), str)
    assert dumped["langfuse"]["projects"] == {}


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


# ---------------------------------------------------------------------------
# Real config/config.json round-trip
# ---------------------------------------------------------------------------


def test_real_config_json_is_valid_and_loads() -> None:
    """The shipped ``config/config.json`` is valid JSON and parses as Settings.

    A previous reformatting accidentally introduced trailing commas which
    Python's stdlib ``json`` rejects.  This test guards against regressions.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    config_path = repo_root / "config" / "config.json"
    assert config_path.is_file(), f"config/config.json not found at {config_path}"

    raw = config_path.read_text()
    data = json.loads(raw)

    # Verify the full model loads (strict, extra="forbid" on sub-models).
    settings = Settings.model_validate(data)
    assert settings.public_fetch.enabled is False
    assert settings.public_fetch.max_body_bytes == 1_048_576


class TestPeriodicSessionDefinition:
    """Validation of the periodic preset model (clean break, extra=forbid)."""

    def test_defaults(self) -> None:
        """A bare preset gets the daily interval and global model level."""
        from robotsix_chat.config.periodic_models import (
            DEFAULT_SCHEDULE_INTERVAL_SECONDS,
            PeriodicSessionDefinition,
        )

        d = PeriodicSessionDefinition(name="p")
        assert d.schedule_interval_seconds == DEFAULT_SCHEDULE_INTERVAL_SECONDS
        assert d.initial_prompt == ""
        assert d.model_level is None
        assert d.enabled is True

    def test_interval_floor_rejects_storm_values(self) -> None:
        """Intervals under the 300s floor are rejected."""
        import pydantic
        import pytest

        from robotsix_chat.config.periodic_models import PeriodicSessionDefinition

        with pytest.raises(pydantic.ValidationError):
            PeriodicSessionDefinition(name="p", schedule_interval_seconds=10)

    def test_model_level_bounds(self) -> None:
        """model_level accepts 1-3 and rejects out-of-range values."""
        import pydantic
        import pytest

        from robotsix_chat.config.periodic_models import PeriodicSessionDefinition

        assert PeriodicSessionDefinition(name="p", model_level=3).model_level == 3
        with pytest.raises(pydantic.ValidationError):
            PeriodicSessionDefinition(name="p", model_level=4)

    def test_legacy_keys_rejected(self) -> None:
        """Old autonomous preset keys fail loudly.

        The deploy migration rewrites stored presets; the code carries no
        compatibility aliases.
        """
        import pydantic
        import pytest

        from robotsix_chat.config.periodic_models import PeriodicSessionDefinition

        with pytest.raises(pydantic.ValidationError):
            PeriodicSessionDefinition(
                name="p",
                trigger_interval_seconds=45.0,  # pyright: ignore[reportCallIssue]
            )


# ---------------------------------------------------------------------------
# Guard: no concrete model names in source
# ---------------------------------------------------------------------------

# Patterns that should never appear in chat source — llmio owns the mapping
# from provider+model to level.  Provider-prefix constants (``claudeSDK``,
# ``openrouter``) ARE allowed.
_CONCRETE_MODEL_PATTERNS: list[str] = [
    r"deepseek/",
    r"mimo-",
    r"-opus",
    r"claude-fable",
    r"gpt-",
]

# Files with pre-existing concrete model references that are not in scope
# for this ticket.  Each entry is ``(file, line_number, pattern)``.
# Remove entries as the leaks are cleaned up in follow-up tickets.
_PREEXISTING_ALLOWLIST: set[tuple[str, int, str]] = {
    # memory/cognee.py — gpt-5-mini / gpt-5-nano in comments
    ("src/robotsix_chat/memory/cognee.py", 468, "gpt-"),
    ("src/robotsix_chat/memory/cognee.py", 471, "gpt-"),
    ("src/robotsix_chat/memory/cognee.py", 615, "gpt-"),
    # config/settings.py — opus / claude-fable-5 in Settings docstring
    ("src/robotsix_chat/config/settings.py", 98, "-opus"),
    ("src/robotsix_chat/config/settings.py", 98, "claude-fable"),
    # config/memory_models.py — gpt-5-nano / gpt-5-mini / deepseek-v4-flash
    ("src/robotsix_chat/config/memory_models.py", 19, "gpt-"),
    ("src/robotsix_chat/config/memory_models.py", 43, "gpt-"),
    ("src/robotsix_chat/config/memory_models.py", 46, "gpt-"),
    ("src/robotsix_chat/config/memory_models.py", 46, "deepseek/"),
    ("src/robotsix_chat/config/memory_models.py", 49, "gpt-"),
}


def test_no_concrete_model_names_in_source() -> None:
    """Assert no concrete model id appears under ``src/``.

    robotsix-llmio owns the provider → model mapping per capability level.
    chat must only reference levels, never concrete model names.
    """
    import re
    from pathlib import Path

    src_root = Path("src")
    if not src_root.is_dir():
        pytest.skip("src/ directory not found")
    violations: list[str] = []
    for py_file in sorted(src_root.rglob("*.py")):
        lines = py_file.read_text().splitlines()
        rel = str(py_file)
        for lineno, line in enumerate(lines, start=1):
            for pattern in _CONCRETE_MODEL_PATTERNS:
                if re.search(pattern, line):
                    if (rel, lineno, pattern) in _PREEXISTING_ALLOWLIST:
                        continue
                    violations.append(f"{rel}:{lineno}: {pattern!r} in: {line.strip()}")
    assert not violations, (
        "Concrete model names found in source — llmio owns the mapping:\n"
        + "\n".join(violations)
    )


def test_legacy_memory_api_key_survives_the_config_library_strip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration must run through robotsix-config's pre-strip hook.

    `load_config` strips keys the model no longer declares *before* calling
    `model_validate`, so relying only on the `@model_validator(mode="before")`
    left the legacy value stripped and unrecoverable — and then validation
    failed for the missing canonical key, i.e. exactly the crash-loop the
    migration exists to prevent. Guards that `Settings.migrate_legacy_config`
    stays wired up.
    """
    assert callable(getattr(Settings, "migrate_legacy_config", None)), (
        "robotsix-config calls Settings.migrate_legacy_config before stripping "
        "unknown keys; without it the legacy key is gone before the "
        "before-validator runs"
    )

    config_path = _write_config_json(
        tmp_path,
        {
            "memory": {
                "enabled": True,
                "llm": {"api_key": "sk-legacy-via-hook"},  # pragma: allowlist secret
                "embedding": {"endpoint": "http://box:11434/v1"},
            },
        },
    )
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(config_path))

    settings = Settings.load()

    assert (
        settings.openrouter.key("robotsix-chat-cognee").get_secret_value()
        == "sk-legacy-via-hook"  # pragma: allowlist secret
    )

    # Second load sees the already-cleaned file: the hook must be a no-op.
    again = Settings.load()
    assert (
        again.openrouter.key("robotsix-chat-cognee").get_secret_value()
        == "sk-legacy-via-hook"  # pragma: allowlist secret
    )


# ---------------------------------------------------------------------------
# Legacy "" numeric sentinels — settings-UI hygiene
# ---------------------------------------------------------------------------


def test_central_deploy_blank_numeric_sentinel_falls_back_to_default() -> None:
    """A legacy ``""`` on a numeric field loads and dumps its default, not ``""``."""
    settings = CentralDeploySettings.model_validate(
        {"component_request_timeout": "", "roster_cache_ttl": ""}
    )

    assert settings.component_request_timeout == 60.0
    assert settings.roster_cache_ttl == 300.0
    dumped = settings.model_dump(mode="json")
    assert "" not in (dumped["component_request_timeout"], dumped["roster_cache_ttl"])


def test_evergoing_blank_numeric_sentinel_loads_cleanly() -> None:
    settings = EvergoingSettings.model_validate(
        {"trim_interval_seconds": "", "keep_min_recent": ""}
    )

    assert settings.trim_interval_seconds == 1800.0
    assert settings.keep_min_recent == 2


def test_kind_turn_budget_blank_numeric_sentinel_loads_cleanly() -> None:
    settings = KindTurnBudget.model_validate(
        {"soft_warn_turns": "", "hard_stop_turns": ""}
    )

    assert settings.soft_warn_turns == 25
    assert settings.hard_stop_turns == 40


def test_memory_blank_numeric_sentinel_loads_cleanly() -> None:
    settings = MemorySettings.model_validate(
        {"maintenance_interval_seconds": "", "recall_max_concurrency": ""}
    )

    assert settings.maintenance_interval_seconds == 21600.0
    assert settings.recall_max_concurrency == 4


def test_feedback_blank_numeric_sentinel_loads_cleanly() -> None:
    settings = FeedbackSettings.model_validate({"ingest_max_retries": ""})

    assert settings.ingest_max_retries == 2


def test_file_hub_tools_blank_numeric_sentinel_loads_cleanly() -> None:
    settings = FileHubToolsSettings.model_validate(
        {"max_download_bytes": "", "timeout": ""}
    )

    assert settings.max_download_bytes == 52_428_800
    assert settings.timeout == 60.0


def test_top_level_optional_numeric_blank_sentinel_becomes_null() -> None:
    """A cleared optional numeric (``int | None``) round-trips to JSON ``null``."""
    settings = Settings.model_validate(
        {"chat_model_level": "", "llmio_task_budget_tokens": ""}
    )

    assert settings.chat_model_level is None
    assert settings.llmio_task_budget_tokens is None
    dumped = settings.model_dump(mode="json")
    assert dumped["chat_model_level"] is None
    assert dumped["llmio_task_budget_tokens"] is None


def test_production_config_with_blank_numeric_sentinels_loads_cleanly() -> None:
    """A production-shaped config peppered with ``""`` numeric sentinels loads.

    Mirrors deployed config files whose optional numeric inputs were cleared
    in the settings UI (persisted as ``""``). After migration the config must
    validate and the model dump must carry no ``""`` for those numeric fields.
    """
    raw = {
        "llmio_model_level": 2,
        "chat_model_level": "",
        "llmio_task_budget_tokens": "",
        "idle_timeout_minutes": "",
        "central_deploy": {
            "component_request_timeout": "",
            "roster_cache_ttl": "",
            "component_response_max_chars": "",
        },
        "evergoing": {
            "trim_interval_seconds": "",
            "keep_min_recent": "",
        },
        "memory": {
            "maintenance_interval_seconds": "",
            "maintenance_version_retention_seconds": "",
        },
        "feedback": {"ingest_max_retries": "", "max_tickets_per_run": ""},
        "file_hub_tools": {"max_download_bytes": "", "timeout": ""},
        "subsessions": {
            "turn_budget": {
                "task": {"soft_warn_turns": "", "hard_stop_turns": ""},
                "periodic": {"soft_warn_turns": "", "hard_stop_turns": ""},
            }
        },
        # Nested submodels WITHOUT their own strip validator — covered only by
        # the recursive walk from the top-level Settings validator.
        "render_url": {"timeout": "", "viewport_width": "", "viewport_height": ""},
    }

    settings = Settings.model_validate(raw)

    # Optional numerics become null; required numerics fall back to defaults.
    assert settings.chat_model_level is None
    assert settings.central_deploy.component_request_timeout == 60.0
    assert settings.evergoing.keep_min_recent == 2
    assert settings.feedback.ingest_max_retries == 2
    assert settings.file_hub_tools.timeout == 60.0
    assert settings.subsessions.turn_budget.task.soft_warn_turns == 25
    # Recursion reached a validator-less submodel too.
    assert settings.render_url.timeout == 30.0
    assert settings.render_url.viewport_width == 1280

    dumped = settings.model_dump(mode="json")
    # No numeric field that carried a "" sentinel may re-serialize as "".
    numeric_checks = [
        dumped["chat_model_level"],
        dumped["llmio_task_budget_tokens"],
        dumped["idle_timeout_minutes"],
        dumped["central_deploy"]["component_request_timeout"],
        dumped["central_deploy"]["roster_cache_ttl"],
        dumped["central_deploy"]["component_response_max_chars"],
        dumped["evergoing"]["trim_interval_seconds"],
        dumped["evergoing"]["keep_min_recent"],
        dumped["memory"]["maintenance_interval_seconds"],
        dumped["memory"]["maintenance_version_retention_seconds"],
        dumped["feedback"]["ingest_max_retries"],
        dumped["feedback"]["max_tickets_per_run"],
        dumped["file_hub_tools"]["max_download_bytes"],
        dumped["file_hub_tools"]["timeout"],
        dumped["subsessions"]["turn_budget"]["task"]["soft_warn_turns"],
        dumped["subsessions"]["turn_budget"]["task"]["hard_stop_turns"],
        dumped["subsessions"]["turn_budget"]["periodic"]["soft_warn_turns"],
        dumped["subsessions"]["turn_budget"]["periodic"]["hard_stop_turns"],
        dumped["render_url"]["timeout"],
        dumped["render_url"]["viewport_width"],
        dumped["render_url"]["viewport_height"],
    ]
    assert all(v != "" for v in numeric_checks), numeric_checks

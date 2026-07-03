"""Environment-variable overlay builders — one per sub-model.

Each ``_build_*_raw()`` function reads optional env vars for one settings
sub-tree and overlays them onto the YAML-derived dict, returning a dict
ready to hand to Pydantic for parsing into the corresponding model.
"""

from __future__ import annotations

import os
from typing import Any

from robotsix_chat.config.constants import _parse_bool, _parse_float, _parse_int


def _env_set(raw_dict: dict[str, Any], field: str, env_name: str) -> None:
    """Set *raw_dict[field]* from *env_name* if the env var is present."""
    value = os.getenv(env_name)
    if value is not None:
        raw_dict[field] = value


def _build_memory_raw(yaml_memory: Any) -> dict[str, Any]:
    """Overlay ``MEMORY_*`` env vars onto the YAML ``memory`` subtree.

    Returns a nested dict (with ``llm`` / ``embedding`` sub-dicts) ready to be
    parsed into :class:`MemorySettings`, or an empty dict when nothing is set.
    """
    memory_raw: dict[str, Any] = dict(yaml_memory or {})
    llm_raw: dict[str, Any] = dict(memory_raw.get("llm") or {})
    embed_raw: dict[str, Any] = dict(memory_raw.get("embedding") or {})

    enabled = os.getenv("MEMORY_ENABLED")
    if enabled is not None:
        memory_raw["enabled"] = _parse_bool(enabled)
    _env_set(memory_raw, "data_dir", "MEMORY_DATA_DIR")
    _env_set(memory_raw, "recall_search_type", "MEMORY_RECALL_SEARCH_TYPE")

    _env_set(llm_raw, "provider", "MEMORY_LLM_PROVIDER")
    _env_set(llm_raw, "model", "MEMORY_LLM_MODEL")
    _env_set(llm_raw, "endpoint", "MEMORY_LLM_ENDPOINT")
    _env_set(llm_raw, "api_key", "MEMORY_LLM_API_KEY")

    _env_set(memory_raw, "langfuse_public_key", "MEMORY_LANGFUSE_PUBLIC_KEY")
    _env_set(memory_raw, "langfuse_secret_key", "MEMORY_LANGFUSE_SECRET_KEY")

    _env_set(embed_raw, "provider", "MEMORY_EMBEDDING_PROVIDER")
    _env_set(embed_raw, "model", "MEMORY_EMBEDDING_MODEL")
    _env_set(embed_raw, "endpoint", "MEMORY_EMBEDDING_ENDPOINT")
    _env_set(embed_raw, "api_key", "MEMORY_EMBEDDING_API_KEY")
    _env_set(embed_raw, "huggingface_tokenizer", "MEMORY_EMBEDDING_TOKENIZER")

    dims = _parse_int("MEMORY_EMBEDDING_DIMENSIONS", "dimensions")
    if dims is not None:
        embed_raw["dimensions"] = dims

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

    enabled = os.getenv("MILL_ENABLED")
    if enabled is not None:
        mill_raw["enabled"] = _parse_bool(enabled)
    _env_set(mill_raw, "broker_host", "MILL_BROKER_HOST")
    _env_set(mill_raw, "broker_scheme", "MILL_BROKER_SCHEME")
    _env_set(mill_raw, "broker_token", "MILL_BROKER_TOKEN")
    _env_set(mill_raw, "agent_id", "MILL_AGENT_ID")
    _env_set(mill_raw, "board_manager_id", "MILL_BOARD_MANAGER_ID")
    _env_set(mill_raw, "repo_id", "MILL_REPO_ID")

    port_val = _parse_int("MILL_BROKER_PORT", "broker_port")
    if port_val is not None:
        mill_raw["broker_port"] = port_val

    timeout_val = _parse_float("MILL_TIMEOUT", "timeout")
    if timeout_val is not None:
        mill_raw["timeout"] = timeout_val

    return mill_raw


def _build_mail_raw(yaml_mail: Any) -> dict[str, Any]:
    """Overlay ``MAIL_*`` env vars onto the YAML ``mail`` subtree.

    Returns a dict ready to parse into :class:`MailSettings`, or empty when
    nothing is set.
    """
    mail_raw: dict[str, Any] = dict(yaml_mail or {})

    enabled = os.getenv("MAIL_ENABLED")
    if enabled is not None:
        mail_raw["enabled"] = _parse_bool(enabled)
    _env_set(mail_raw, "api_base_url", "MAIL_API_BASE_URL")
    _env_set(mail_raw, "api_token", "MAIL_API_TOKEN")

    timeout_val = _parse_float("MAIL_TIMEOUT", "timeout")
    if timeout_val is not None:
        mail_raw["timeout"] = timeout_val

    return mail_raw


def _build_calendar_raw(yaml_calendar: Any) -> dict[str, Any]:
    """Overlay ``CALENDAR_*`` env vars onto the YAML ``calendar`` subtree.

    Returns a dict ready to parse into :class:`CalendarSettings`, or empty when
    nothing is set.
    """
    calendar_raw: dict[str, Any] = dict(yaml_calendar or {})

    enabled = os.getenv("CALENDAR_ENABLED")
    if enabled is not None:
        calendar_raw["enabled"] = _parse_bool(enabled)
    _env_set(calendar_raw, "broker_host", "CALENDAR_BROKER_HOST")
    _env_set(calendar_raw, "broker_scheme", "CALENDAR_BROKER_SCHEME")
    _env_set(calendar_raw, "broker_token", "CALENDAR_BROKER_TOKEN")
    _env_set(calendar_raw, "agent_id", "CALENDAR_AGENT_ID")
    _env_set(calendar_raw, "calendar_agent_id", "CALENDAR_CALENDAR_AGENT_ID")

    port_val = _parse_int("CALENDAR_BROKER_PORT", "broker_port")
    if port_val is not None:
        calendar_raw["broker_port"] = port_val

    timeout_val = _parse_float("CALENDAR_TIMEOUT", "timeout")
    if timeout_val is not None:
        calendar_raw["timeout"] = timeout_val

    cache_ttl_val = _parse_float("CALENDAR_CACHE_TTL", "cache_ttl")
    if cache_ttl_val is not None:
        calendar_raw["cache_ttl"] = cache_ttl_val

    return calendar_raw


def _build_conversation_raw(yaml_conversation: Any) -> dict[str, Any]:
    """Overlay ``CONVERSATION_*`` env vars onto the YAML ``conversation`` subtree.

    Returns a dict ready to parse into :class:`ConversationSettings`, or empty
    when nothing is set.
    """
    conversation_raw: dict[str, Any] = dict(yaml_conversation or {})

    def _set_if_not_none(field: str, env_name: str) -> None:
        val = _parse_int(env_name, field)
        if val is not None:
            conversation_raw[field] = val

    _set_if_not_none("idle_reset_seconds", "CONVERSATION_IDLE_RESET_SECONDS")
    _set_if_not_none("max_history_turns", "CONVERSATION_MAX_HISTORY_TURNS")
    _set_if_not_none("max_conversations", "CONVERSATION_MAX_CONVERSATIONS")

    persist_path = os.getenv("CONVERSATION_PERSIST_PATH")
    if persist_path is not None:
        conversation_raw["persist_path"] = persist_path

    return conversation_raw


def _build_refdocs_raw(yaml_refdocs: Any) -> dict[str, Any]:
    """Overlay ``REFDOCS_*`` env vars onto the YAML ``refdocs`` subtree.

    Returns a dict ready to parse into :class:`RefDocsSettings`, or empty
    when nothing is set.
    """
    refdocs_raw: dict[str, Any] = dict(yaml_refdocs or {})

    enabled = os.getenv("REFDOCS_ENABLED")
    if enabled is not None:
        refdocs_raw["enabled"] = _parse_bool(enabled)
    _env_set(refdocs_raw, "github_token", "REFDOCS_GITHUB_TOKEN")
    _env_set(refdocs_raw, "ref", "REFDOCS_REF")
    _env_set(refdocs_raw, "base_url", "REFDOCS_BASE_URL")

    repos_raw = os.getenv("REFDOCS_REPOS")
    if repos_raw is not None:
        refdocs_raw["repos"] = [
            repo.strip() for repo in repos_raw.split(",") if repo.strip()
        ]

    timeout_val = _parse_float("REFDOCS_TIMEOUT", "timeout")
    if timeout_val is not None:
        refdocs_raw["timeout"] = timeout_val

    return refdocs_raw


def _build_board_reader_raw(yaml_board_reader: Any) -> dict[str, Any]:
    """Overlay ``BOARD_READER_*`` env vars onto the YAML ``board_reader`` subtree.

    Returns a dict ready to parse into :class:`BoardSettings`, or empty
    when nothing is set.
    """
    board_reader_raw: dict[str, Any] = dict(yaml_board_reader or {})

    enabled = os.getenv("BOARD_READER_ENABLED")
    if enabled is not None:
        board_reader_raw["enabled"] = _parse_bool(enabled)
    _env_set(board_reader_raw, "api_base_url", "BOARD_READER_API_BASE_URL")
    _env_set(board_reader_raw, "api_token", "BOARD_READER_API_TOKEN")

    cache_ttl_val = _parse_float("BOARD_READER_CACHE_TTL", "cache_ttl")
    if cache_ttl_val is not None:
        board_reader_raw["cache_ttl"] = cache_ttl_val

    return board_reader_raw


def _build_diagnostics_raw(yaml_diagnostics: Any) -> dict[str, Any]:
    """Overlay ``DIAGNOSTICS_*`` env vars onto the YAML ``diagnostics`` subtree.

    Returns a dict ready to parse into :class:`DiagnosticsSettings`, or empty
    when nothing is set.
    """
    raw: dict[str, Any] = dict(yaml_diagnostics or {})

    enabled = os.getenv("DIAGNOSTICS_ENABLED")
    if enabled is not None:
        raw["enabled"] = _parse_bool(enabled)

    store_path = os.getenv("DIAGNOSTICS_STORE_PATH")
    if store_path is not None:
        raw["store_path"] = store_path

    proposals_path = os.getenv("DIAGNOSTICS_PROPOSALS_PATH")
    if proposals_path is not None:
        raw["proposals_path"] = proposals_path

    effectiveness_path = os.getenv("DIAGNOSTICS_EFFECTIVENESS_PATH")
    if effectiveness_path is not None:
        raw["effectiveness_path"] = effectiveness_path

    threshold_val = _parse_int(
        "DIAGNOSTICS_RECURRENCE_THRESHOLD", "recurrence_threshold"
    )
    if threshold_val is not None:
        raw["recurrence_threshold"] = threshold_val

    window_val = _parse_int(
        "DIAGNOSTICS_RECURRENCE_WINDOW_DAYS", "recurrence_window_days"
    )
    if window_val is not None:
        raw["recurrence_window_days"] = window_val

    obs_window_val = _parse_int(
        "DIAGNOSTICS_OBSERVATION_WINDOW_DAYS", "observation_window_days"
    )
    if obs_window_val is not None:
        raw["observation_window_days"] = obs_window_val

    return raw


def _build_direct_repo_raw(yaml_direct_repo: Any) -> dict[str, Any]:
    """Overlay ``DIRECT_REPO_*`` env vars onto the YAML ``direct_repo`` subtree.

    Returns a dict ready to parse into :class:`DirectRepoSettings`, or empty
    when nothing is set.
    """
    dr_raw: dict[str, Any] = dict(yaml_direct_repo or {})

    enabled = os.getenv("DIRECT_REPO_ENABLED")
    if enabled is not None:
        dr_raw["enabled"] = _parse_bool(enabled)
    _env_set(dr_raw, "github_app_id", "DIRECT_REPO_GITHUB_APP_ID")
    _env_set(dr_raw, "github_app_private_key", "DIRECT_REPO_GITHUB_APP_PRIVATE_KEY")
    _env_set(
        dr_raw,
        "github_app_installation_id",
        "DIRECT_REPO_GITHUB_APP_INSTALLATION_ID",
    )
    _env_set(dr_raw, "github_api_base_url", "DIRECT_REPO_GITHUB_API_BASE_URL")
    _env_set(dr_raw, "board_api_base_url", "DIRECT_REPO_BOARD_API_BASE_URL")
    _env_set(dr_raw, "board_api_token", "DIRECT_REPO_BOARD_API_TOKEN")

    timeout_val = _parse_float("DIRECT_REPO_TIMEOUT", "timeout")
    if timeout_val is not None:
        dr_raw["timeout"] = timeout_val

    return dr_raw


def _build_skills_raw(yaml_skills: Any) -> dict[str, Any]:
    """Overlay ``SKILLS_*`` env vars onto the YAML ``skills`` subtree.

    Returns a dict ready to parse into :class:`SkillsSettings`, or empty
    when nothing is set.
    """
    skills_raw: dict[str, Any] = dict(yaml_skills or {})

    enabled = os.getenv("SKILLS_ENABLED")
    if enabled is not None:
        skills_raw["enabled"] = _parse_bool(enabled)

    manifests_dir = os.getenv("SKILLS_MANIFESTS_DIR")
    if manifests_dir is not None:
        skills_raw["manifests_dir"] = manifests_dir

    return skills_raw


def _build_knowledge_raw(yaml_knowledge: Any) -> dict[str, Any]:
    """Overlay ``KNOWLEDGE_*`` env vars onto the YAML ``knowledge`` subtree.

    Returns a dict ready to parse into :class:`KnowledgeSettings`, or empty
    when nothing is set.
    """
    knowledge_raw: dict[str, Any] = dict(yaml_knowledge or {})

    enabled = os.getenv("KNOWLEDGE_ENABLED")
    if enabled is not None:
        knowledge_raw["enabled"] = _parse_bool(enabled)

    path = os.getenv("KNOWLEDGE_PATH")
    if path is not None:
        knowledge_raw["path"] = path

    return knowledge_raw


def _build_subsessions_raw(yaml_data: Any) -> dict[str, Any]:
    """Overlay ``SUBSESSIONS_*`` env vars onto the YAML ``subsessions`` subtree.

    Returns a dict ready to parse into :class:`SubsessionsSettings`, or empty
    when nothing is set.
    """
    raw: dict[str, Any] = dict(yaml_data or {})

    max_concurrent = _parse_int("SUBSESSIONS_MAX_CONCURRENT", "max_concurrent")
    if max_concurrent is not None:
        raw["max_concurrent"] = max_concurrent

    max_depth = _parse_int("SUBSESSIONS_MAX_DEPTH", "max_depth")
    if max_depth is not None:
        raw["max_depth"] = max_depth

    default_level = _parse_int("SUBSESSIONS_DEFAULT_MODEL_LEVEL", "default_model_level")
    if default_level is not None:
        raw["default_model_level"] = default_level

    min_interval = _parse_float(
        "SUBSESSIONS_MIN_INTERVAL_SECONDS", "min_interval_seconds"
    )
    if min_interval is not None:
        raw["min_interval_seconds"] = min_interval

    no_change_runs = _parse_int(
        "SUBSESSIONS_AUTO_STOP_NO_CHANGE_RUNS", "auto_stop_no_change_runs"
    )
    if no_change_runs is not None:
        raw["auto_stop_no_change_runs"] = no_change_runs

    store_path = os.getenv("SUBSESSIONS_STORE_PATH")
    if store_path is not None:
        raw["store_path"] = store_path

    transcript_max_entries = _parse_int(
        "SUBSESSIONS_TRANSCRIPT_MAX_ENTRIES", "transcript_max_entries"
    )
    if transcript_max_entries is not None:
        raw["transcript_max_entries"] = transcript_max_entries

    return raw


def _build_self_review_raw(yaml_self_review: Any) -> dict[str, Any]:
    """Overlay ``SELF_REVIEW_*`` env vars onto the YAML ``self_review`` subtree.

    Returns a dict ready to parse into :class:`SelfReviewSettings`, or empty
    when nothing is set.
    """
    self_review_raw: dict[str, Any] = dict(yaml_self_review or {})

    enabled = os.getenv("SELF_REVIEW_ENABLED")
    if enabled is not None:
        self_review_raw["enabled"] = _parse_bool(enabled)

    limit_val = _parse_int("SELF_REVIEW_RECENT_ACTIVITY_LIMIT", "recent_activity_limit")
    if limit_val is not None:
        self_review_raw["recent_activity_limit"] = limit_val

    return self_review_raw


def _build_component_agent_raw(yaml_component_agent: Any) -> dict[str, Any]:
    """Overlay ``COMPONENT_AGENT_*`` env vars onto the YAML ``component_agent`` subtree.

    Returns a dict ready to parse into :class:`ComponentAgentSettings`, or empty
    when nothing is set.
    """
    component_agent_raw: dict[str, Any] = dict(yaml_component_agent or {})

    enabled = os.getenv("COMPONENT_AGENT_ENABLED")
    if enabled is not None:
        component_agent_raw["enabled"] = _parse_bool(enabled)
    _env_set(component_agent_raw, "broker_host", "COMPONENT_AGENT_BROKER_HOST")
    _env_set(component_agent_raw, "broker_scheme", "COMPONENT_AGENT_BROKER_SCHEME")
    _env_set(component_agent_raw, "broker_token", "COMPONENT_AGENT_BROKER_TOKEN")
    _env_set(component_agent_raw, "agent_id", "COMPONENT_AGENT_AGENT_ID")

    port_val = _parse_int("COMPONENT_AGENT_BROKER_PORT", "broker_port")
    if port_val is not None:
        component_agent_raw["broker_port"] = port_val

    timeout_val = _parse_float("COMPONENT_AGENT_TIMEOUT", "timeout")
    if timeout_val is not None:
        component_agent_raw["timeout"] = timeout_val

    return component_agent_raw


def _build_version_check_raw(yaml_version_check: Any) -> dict[str, Any]:
    """Overlay ``VERSION_CHECK_*`` env vars onto the YAML ``version_check`` subtree.

    Returns a dict ready to parse into :class:`VersionCheckSettings`, or empty
    when nothing is set.
    """
    version_check_raw: dict[str, Any] = dict(yaml_version_check or {})

    enabled = os.getenv("VERSION_CHECK_ENABLED")
    if enabled is not None:
        version_check_raw["enabled"] = _parse_bool(enabled)
    _env_set(version_check_raw, "repo", "VERSION_CHECK_REPO")
    _env_set(version_check_raw, "github_token", "VERSION_CHECK_GITHUB_TOKEN")
    _env_set(version_check_raw, "base_url", "VERSION_CHECK_BASE_URL")

    timeout_val = _parse_float("VERSION_CHECK_TIMEOUT", "timeout")
    if timeout_val is not None:
        version_check_raw["timeout"] = timeout_val

    cache_ttl_val = _parse_float("VERSION_CHECK_CACHE_TTL", "cache_ttl")
    if cache_ttl_val is not None:
        version_check_raw["cache_ttl"] = cache_ttl_val

    return version_check_raw


def _build_component_client_raw(yaml_component_client: Any) -> dict[str, Any]:
    """Overlay ``COMPONENT_CLIENT_*`` env vars onto the ``component_client`` subtree.

    Returns a dict ready to parse into
    :class:`ComponentClientSettings`, or empty when nothing is set.
    """
    cc_raw: dict[str, Any] = dict(yaml_component_client or {})

    enabled = os.getenv("COMPONENT_CLIENT_ENABLED")
    if enabled is not None:
        cc_raw["enabled"] = _parse_bool(enabled)

    timeout_val = _parse_float("COMPONENT_CLIENT_TIMEOUT", "timeout")
    if timeout_val is not None:
        cc_raw["timeout"] = timeout_val

    return cc_raw

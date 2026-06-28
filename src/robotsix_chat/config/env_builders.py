"""Environment-variable overlay builders — one per sub-model.

Each ``_build_*_raw()`` function reads optional env vars for one settings
sub-tree and overlays them onto the YAML-derived dict, returning a dict
ready to hand to Pydantic for parsing into the corresponding model.
"""

from __future__ import annotations

import os
from typing import Any

from robotsix_chat.config.constants import _parse_bool


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


def _build_mail_raw(yaml_mail: Any) -> dict[str, Any]:
    """Overlay ``MAIL_*`` env vars onto the YAML ``mail`` subtree.

    Returns a dict ready to parse into :class:`MailSettings`, or empty when
    nothing is set.
    """
    mail_raw: dict[str, Any] = dict(yaml_mail or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            mail_raw[field] = value

    enabled = os.getenv("MAIL_ENABLED")
    if enabled is not None:
        mail_raw["enabled"] = _parse_bool(enabled)
    env_set("broker_host", "MAIL_BROKER_HOST")
    env_set("broker_scheme", "MAIL_BROKER_SCHEME")
    env_set("broker_token", "MAIL_BROKER_TOKEN")
    env_set("agent_id", "MAIL_AGENT_ID")
    env_set("board_manager_id", "MAIL_BOARD_MANAGER_ID")

    port_str = os.getenv("MAIL_BROKER_PORT")
    if port_str is not None:
        try:
            mail_raw["broker_port"] = int(port_str)
        except ValueError:
            raise ValueError(
                f"MAIL_BROKER_PORT must be an integer, got {port_str!r}"
            ) from None

    timeout_str = os.getenv("MAIL_TIMEOUT")
    if timeout_str is not None:
        try:
            mail_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"MAIL_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return mail_raw


def _build_calendar_raw(yaml_calendar: Any) -> dict[str, Any]:
    """Overlay ``CALENDAR_*`` env vars onto the YAML ``calendar`` subtree.

    Returns a dict ready to parse into :class:`CalendarSettings`, or empty when
    nothing is set.
    """
    calendar_raw: dict[str, Any] = dict(yaml_calendar or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            calendar_raw[field] = value

    enabled = os.getenv("CALENDAR_ENABLED")
    if enabled is not None:
        calendar_raw["enabled"] = _parse_bool(enabled)
    env_set("broker_host", "CALENDAR_BROKER_HOST")
    env_set("broker_scheme", "CALENDAR_BROKER_SCHEME")
    env_set("broker_token", "CALENDAR_BROKER_TOKEN")
    env_set("agent_id", "CALENDAR_AGENT_ID")
    env_set("calendar_agent_id", "CALENDAR_CALENDAR_AGENT_ID")

    port_str = os.getenv("CALENDAR_BROKER_PORT")
    if port_str is not None:
        try:
            calendar_raw["broker_port"] = int(port_str)
        except ValueError:
            raise ValueError(
                f"CALENDAR_BROKER_PORT must be an integer, got {port_str!r}"
            ) from None

    timeout_str = os.getenv("CALENDAR_TIMEOUT")
    if timeout_str is not None:
        try:
            calendar_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"CALENDAR_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    cache_ttl_str = os.getenv("CALENDAR_CACHE_TTL")
    if cache_ttl_str is not None:
        try:
            calendar_raw["cache_ttl"] = float(cache_ttl_str)
        except ValueError:
            raise ValueError(
                f"CALENDAR_CACHE_TTL must be a number, got {cache_ttl_str!r}"
            ) from None

    return calendar_raw


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

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            refdocs_raw[field] = value

    enabled = os.getenv("REFDOCS_ENABLED")
    if enabled is not None:
        refdocs_raw["enabled"] = _parse_bool(enabled)
    env_set("github_token", "REFDOCS_GITHUB_TOKEN")
    env_set("ref", "REFDOCS_REF")
    env_set("base_url", "REFDOCS_BASE_URL")

    repos_raw = os.getenv("REFDOCS_REPOS")
    if repos_raw is not None:
        refdocs_raw["repos"] = [
            repo.strip() for repo in repos_raw.split(",") if repo.strip()
        ]

    timeout_str = os.getenv("REFDOCS_TIMEOUT")
    if timeout_str is not None:
        try:
            refdocs_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"REFDOCS_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return refdocs_raw


def _build_board_reader_raw(yaml_board_reader: Any) -> dict[str, Any]:
    """Overlay ``BOARD_READER_*`` env vars onto the YAML ``board_reader`` subtree.

    Returns a dict ready to parse into :class:`BoardReaderSettings`, or empty
    when nothing is set.
    """
    board_reader_raw: dict[str, Any] = dict(yaml_board_reader or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            board_reader_raw[field] = value

    enabled = os.getenv("BOARD_READER_ENABLED")
    if enabled is not None:
        board_reader_raw["enabled"] = _parse_bool(enabled)
    env_set("api_base_url", "BOARD_READER_API_BASE_URL")
    env_set("api_token", "BOARD_READER_API_TOKEN")

    timeout_str = os.getenv("BOARD_READER_TIMEOUT")
    if timeout_str is not None:
        try:
            board_reader_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"BOARD_READER_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    cache_ttl_str = os.getenv("BOARD_READER_CACHE_TTL")
    if cache_ttl_str is not None:
        try:
            board_reader_raw["cache_ttl"] = float(cache_ttl_str)
        except ValueError:
            raise ValueError(
                f"BOARD_READER_CACHE_TTL must be a number, got {cache_ttl_str!r}"
            ) from None

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

    threshold_str = os.getenv("DIAGNOSTICS_RECURRENCE_THRESHOLD")
    if threshold_str is not None:
        try:
            raw["recurrence_threshold"] = int(threshold_str)
        except ValueError:
            raise ValueError(
                f"DIAGNOSTICS_RECURRENCE_THRESHOLD must be an integer, "
                f"got {threshold_str!r}"
            ) from None

    window_str = os.getenv("DIAGNOSTICS_RECURRENCE_WINDOW_DAYS")
    if window_str is not None:
        try:
            raw["recurrence_window_days"] = int(window_str)
        except ValueError:
            raise ValueError(
                f"DIAGNOSTICS_RECURRENCE_WINDOW_DAYS must be an integer, "
                f"got {window_str!r}"
            ) from None

    obs_window_str = os.getenv("DIAGNOSTICS_OBSERVATION_WINDOW_DAYS")
    if obs_window_str is not None:
        try:
            raw["observation_window_days"] = int(obs_window_str)
        except ValueError:
            raise ValueError(
                f"DIAGNOSTICS_OBSERVATION_WINDOW_DAYS must be an integer, "
                f"got {obs_window_str!r}"
            ) from None

    return raw


def _build_direct_repo_raw(yaml_direct_repo: Any) -> dict[str, Any]:
    """Overlay ``DIRECT_REPO_*`` env vars onto the YAML ``direct_repo`` subtree.

    Returns a dict ready to parse into :class:`DirectRepoSettings`, or empty
    when nothing is set.
    """
    dr_raw: dict[str, Any] = dict(yaml_direct_repo or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            dr_raw[field] = value

    enabled = os.getenv("DIRECT_REPO_ENABLED")
    if enabled is not None:
        dr_raw["enabled"] = _parse_bool(enabled)
    env_set("github_app_id", "DIRECT_REPO_GITHUB_APP_ID")
    env_set("github_app_private_key", "DIRECT_REPO_GITHUB_APP_PRIVATE_KEY")
    env_set("github_app_installation_id", "DIRECT_REPO_GITHUB_APP_INSTALLATION_ID")
    env_set("github_api_base_url", "DIRECT_REPO_GITHUB_API_BASE_URL")
    env_set("board_api_base_url", "DIRECT_REPO_BOARD_API_BASE_URL")
    env_set("board_api_token", "DIRECT_REPO_BOARD_API_TOKEN")

    timeout_str = os.getenv("DIRECT_REPO_TIMEOUT")
    if timeout_str is not None:
        try:
            dr_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"DIRECT_REPO_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return dr_raw


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


def _build_pending_questions_raw(yaml_data: Any) -> dict[str, Any]:
    """Overlay ``PENDING_QUESTIONS_*`` env vars onto the YAML ``pending_questions``.

    Returns a dict ready to parse into :class:`PendingQuestionsSettings`, or empty
    when nothing is set.
    """
    raw: dict[str, Any] = dict(yaml_data or {})

    enabled = os.getenv("PENDING_QUESTIONS_ENABLED")
    if enabled is not None:
        raw["enabled"] = _parse_bool(enabled)

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

    limit_str = os.getenv("SELF_REVIEW_RECENT_ACTIVITY_LIMIT")
    if limit_str is not None:
        try:
            self_review_raw["recent_activity_limit"] = int(limit_str)
        except ValueError:
            raise ValueError(
                f"SELF_REVIEW_RECENT_ACTIVITY_LIMIT must be an integer, "
                f"got {limit_str!r}"
            ) from None

    return self_review_raw


def _build_component_agent_raw(yaml_component_agent: Any) -> dict[str, Any]:
    """Overlay ``COMPONENT_AGENT_*`` env vars onto the YAML ``component_agent`` subtree.

    Returns a dict ready to parse into :class:`ComponentAgentSettings`, or empty
    when nothing is set.
    """
    component_agent_raw: dict[str, Any] = dict(yaml_component_agent or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            component_agent_raw[field] = value

    enabled = os.getenv("COMPONENT_AGENT_ENABLED")
    if enabled is not None:
        component_agent_raw["enabled"] = _parse_bool(enabled)
    env_set("broker_host", "COMPONENT_AGENT_BROKER_HOST")
    env_set("broker_scheme", "COMPONENT_AGENT_BROKER_SCHEME")
    env_set("broker_token", "COMPONENT_AGENT_BROKER_TOKEN")
    env_set("agent_id", "COMPONENT_AGENT_AGENT_ID")

    port_str = os.getenv("COMPONENT_AGENT_BROKER_PORT")
    if port_str is not None:
        try:
            component_agent_raw["broker_port"] = int(port_str)
        except ValueError:
            raise ValueError(
                f"COMPONENT_AGENT_BROKER_PORT must be an integer, got {port_str!r}"
            ) from None

    timeout_str = os.getenv("COMPONENT_AGENT_TIMEOUT")
    if timeout_str is not None:
        try:
            component_agent_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"COMPONENT_AGENT_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return component_agent_raw


def _build_version_check_raw(yaml_version_check: Any) -> dict[str, Any]:
    """Overlay ``VERSION_CHECK_*`` env vars onto the YAML ``version_check`` subtree.

    Returns a dict ready to parse into :class:`VersionCheckSettings`, or empty
    when nothing is set.
    """
    version_check_raw: dict[str, Any] = dict(yaml_version_check or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            version_check_raw[field] = value

    enabled = os.getenv("VERSION_CHECK_ENABLED")
    if enabled is not None:
        version_check_raw["enabled"] = _parse_bool(enabled)
    env_set("repo", "VERSION_CHECK_REPO")
    env_set("github_token", "VERSION_CHECK_GITHUB_TOKEN")
    env_set("base_url", "VERSION_CHECK_BASE_URL")

    timeout_str = os.getenv("VERSION_CHECK_TIMEOUT")
    if timeout_str is not None:
        try:
            version_check_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"VERSION_CHECK_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    cache_ttl_str = os.getenv("VERSION_CHECK_CACHE_TTL")
    if cache_ttl_str is not None:
        try:
            version_check_raw["cache_ttl"] = float(cache_ttl_str)
        except ValueError:
            raise ValueError(
                f"VERSION_CHECK_CACHE_TTL must be a number, got {cache_ttl_str!r}"
            ) from None

    return version_check_raw


def _build_component_client_raw(yaml_component_client: Any) -> dict[str, Any]:
    """Overlay ``COMPONENT_CLIENT_*`` env vars onto the ``component_client`` subtree.

    Returns a dict ready to parse into
    :class:`ComponentClientSettings`, or empty when nothing is set.
    """
    cc_raw: dict[str, Any] = dict(yaml_component_client or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            cc_raw[field] = value

    enabled = os.getenv("COMPONENT_CLIENT_ENABLED")
    if enabled is not None:
        cc_raw["enabled"] = _parse_bool(enabled)
    env_set("broker_host", "COMPONENT_CLIENT_BROKER_HOST")
    env_set("broker_scheme", "COMPONENT_CLIENT_BROKER_SCHEME")
    env_set("broker_token", "COMPONENT_CLIENT_BROKER_TOKEN")
    env_set("agent_id", "COMPONENT_CLIENT_AGENT_ID")

    port_str = os.getenv("COMPONENT_CLIENT_BROKER_PORT")
    if port_str is not None:
        try:
            cc_raw["broker_port"] = int(port_str)
        except ValueError:
            raise ValueError(
                f"COMPONENT_CLIENT_BROKER_PORT must be an integer, got {port_str!r}"
            ) from None

    timeout_str = os.getenv("COMPONENT_CLIENT_TIMEOUT")
    if timeout_str is not None:
        try:
            cc_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"COMPONENT_CLIENT_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return cc_raw

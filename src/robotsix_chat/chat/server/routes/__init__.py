"""Route handlers package — each module holds a focused set of related endpoints."""

from ._shared import (
    _get_session_id,
    _parse_json_body,
    _sse_frame,
    health_endpoint,
    ui_endpoint,
)
from .admin import (
    disk_usage_endpoint,
    prune_endpoint,
)
from .auth import (
    auth_callback_endpoint,
    auth_login_endpoint,
    mobile_token_endpoint,
)
from .chat import (
    ChatAgent,
    MessageCoalescer,
    RunSerializer,
    _parse_and_validate_images,
    cancel_queued_endpoint,
    chat_endpoint,
)
from .chat_skill import (
    chat_skill_endpoint,
)
from .config import (
    config_deploy_get_endpoint,
    config_get_endpoint,
    config_rollback_endpoint,
    config_save_endpoint,
    config_version_diff_endpoint,
    config_version_get_endpoint,
    config_versions_endpoint,
)
from .constants import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_HEARTBEAT_FRAME,
    SSE_HEARTBEAT_INTERVAL,
    SSE_TOKEN_TYPE,
)
from .diagnostics import (
    diagnostics_create_endpoint,
    diagnostics_list_endpoint,
)
from .draft import (
    draft_get_endpoint,
    draft_save_endpoint,
)
from .errors import (
    http_exception_handler,
    not_found_handler,
    server_error_handler,
    unhandled_exception_handler,
)
from .events import (
    events_endpoint,
)
from .github import (
    github_actions_secret_endpoint,
    github_actions_workflow_endpoint,
    github_job_log_endpoint,
    github_repo_create_endpoint,
    github_settings_endpoint,
)
from .memory import (
    memory_ingestion_structure_endpoint,
)
from .metrics import (
    metrics_endpoint,
)
from .mill_events import (
    mill_events_endpoint,
)
from .notifications import (
    notifications_read_endpoint,
    notifications_unread_endpoint,
)
from .sessions import (
    _cleanup_session,
    history_endpoint,
    models_list_endpoint,
    periodic_definitions_list_endpoint,
    periodic_definitions_run_endpoint,
    session_model_set_endpoint,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
)
from .subsessions import (
    _get_subsession_registry,
    _resolve_subsession,
    subsessions_close_endpoint,
    subsessions_get_endpoint,
    subsessions_list_endpoint,
    subsessions_message_endpoint,
    subsessions_transcript_endpoint,
)

__all__ = [
    "SSE_CONTENT_TYPE",
    "SSE_DONE_TYPE",
    "SSE_ERROR_TYPE",
    "SSE_HEARTBEAT_FRAME",
    "SSE_HEARTBEAT_INTERVAL",
    "SSE_TOKEN_TYPE",
    "ChatAgent",
    "MessageCoalescer",
    "RunSerializer",
    "_cleanup_session",
    "_get_session_id",
    "_get_subsession_registry",
    "_parse_and_validate_images",
    "_parse_json_body",
    "_resolve_subsession",
    "_sse_frame",
    "auth_callback_endpoint",
    "auth_login_endpoint",
    "cancel_queued_endpoint",
    "chat_endpoint",
    "chat_skill_endpoint",
    "config_deploy_get_endpoint",
    "config_get_endpoint",
    "config_rollback_endpoint",
    "config_save_endpoint",
    "config_version_diff_endpoint",
    "config_version_get_endpoint",
    "config_versions_endpoint",
    "diagnostics_create_endpoint",
    "diagnostics_list_endpoint",
    "disk_usage_endpoint",
    "draft_get_endpoint",
    "draft_save_endpoint",
    "events_endpoint",
    "github_actions_secret_endpoint",
    "github_actions_workflow_endpoint",
    "github_job_log_endpoint",
    "github_repo_create_endpoint",
    "github_settings_endpoint",
    "health_endpoint",
    "history_endpoint",
    "http_exception_handler",
    "memory_ingestion_structure_endpoint",
    "metrics_endpoint",
    "mill_events_endpoint",
    "mobile_token_endpoint",
    "models_list_endpoint",
    "not_found_handler",
    "notifications_read_endpoint",
    "notifications_unread_endpoint",
    "periodic_definitions_list_endpoint",
    "periodic_definitions_run_endpoint",
    "prune_endpoint",
    "server_error_handler",
    "session_model_set_endpoint",
    "sessions_close_endpoint",
    "sessions_create_endpoint",
    "sessions_delete_endpoint",
    "sessions_list_endpoint",
    "subsessions_close_endpoint",
    "subsessions_get_endpoint",
    "subsessions_list_endpoint",
    "subsessions_message_endpoint",
    "subsessions_transcript_endpoint",
    "ui_endpoint",
    "unhandled_exception_handler",
]

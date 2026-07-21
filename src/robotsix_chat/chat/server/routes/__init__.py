"""Route handlers package — each module holds a focused set of related endpoints."""

from ._shared import (
    _get_session_id,
    _parse_json_body,
    _sse_frame,
    health_endpoint,
    ui_endpoint,
)
from .chat import (
    ChatAgent,
    MessageCoalescer,
    RunSerializer,
    _parse_and_validate_images,
    cancel_queued_endpoint,
    chat_endpoint,
)
from .config import (
    config_get_endpoint,
    config_save_endpoint,
)
from .constants import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_HEARTBEAT_FRAME,
    SSE_HEARTBEAT_INTERVAL,
    SSE_TOKEN_TYPE,
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
    github_settings_endpoint,
)
from .sessions import (
    _cleanup_session,
    history_endpoint,
    sessions_approve_endpoint,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
    sessions_reject_endpoint,
    summary_endpoint,
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
    "cancel_queued_endpoint",
    "chat_endpoint",
    "config_get_endpoint",
    "config_save_endpoint",
    "events_endpoint",
    "github_actions_secret_endpoint",
    "github_actions_workflow_endpoint",
    "github_settings_endpoint",
    "health_endpoint",
    "history_endpoint",
    "http_exception_handler",
    "not_found_handler",
    "server_error_handler",
    "sessions_approve_endpoint",
    "sessions_close_endpoint",
    "sessions_create_endpoint",
    "sessions_delete_endpoint",
    "sessions_list_endpoint",
    "sessions_reject_endpoint",
    "subsessions_close_endpoint",
    "subsessions_get_endpoint",
    "subsessions_list_endpoint",
    "subsessions_message_endpoint",
    "subsessions_transcript_endpoint",
    "summary_endpoint",
    "ui_endpoint",
    "unhandled_exception_handler",
]

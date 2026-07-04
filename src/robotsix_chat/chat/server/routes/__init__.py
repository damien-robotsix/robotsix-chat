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
    RunSerializer,
    _parse_and_validate_images,
    chat_endpoint,
)
from .constants import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_HEARTBEAT_INTERVAL,
    SSE_TOKEN_TYPE,
    _SSE_HEARTBEAT_FRAME,
)
from .errors import (
    not_found_handler,
    server_error_handler,
)
from .events import (
    events_endpoint,
)
from .sessions import (
    _cleanup_session,
    history_endpoint,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
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
    "ChatAgent",
    "RunSerializer",
    "SSE_CONTENT_TYPE",
    "SSE_DONE_TYPE",
    "SSE_ERROR_TYPE",
    "SSE_HEARTBEAT_INTERVAL",
    "SSE_TOKEN_TYPE",
    "_SSE_HEARTBEAT_FRAME",
    "_cleanup_session",
    "_get_session_id",
    "_get_subsession_registry",
    "_parse_and_validate_images",
    "_parse_json_body",
    "_resolve_subsession",
    "_sse_frame",
    "chat_endpoint",
    "events_endpoint",
    "health_endpoint",
    "history_endpoint",
    "not_found_handler",
    "server_error_handler",
    "sessions_close_endpoint",
    "sessions_create_endpoint",
    "sessions_delete_endpoint",
    "sessions_list_endpoint",
    "subsessions_close_endpoint",
    "subsessions_get_endpoint",
    "subsessions_list_endpoint",
    "subsessions_message_endpoint",
    "subsessions_transcript_endpoint",
    "summary_endpoint",
    "ui_endpoint",
]

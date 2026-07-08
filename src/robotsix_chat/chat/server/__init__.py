"""Chat SSE server — HTTP + Server-Sent Events bridge for human-to-agent chat.

Re-exports the public API that used to live in the monolithic ``server.py``,
preserving backward compatibility for all existing import sites
(``tests/``, ``src/robotsix_chat/chat/__init__.py``, etc.).
"""

from .app import (
    _load_ui_html,
    _make_lifespan,
    create_agent_from_settings,
    create_app,
)
from .cli import (
    _setup_observability,
    run_server,
    run_server_from_config,
)
from .routes import (  # noqa: F401 — imports used via dynamic __all__ below
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_HEARTBEAT_FRAME,
    SSE_HEARTBEAT_INTERVAL,
    SSE_TOKEN_TYPE,
    ChatAgent,
    RunSerializer,
    _parse_and_validate_images,
    _sse_frame,
    chat_endpoint,
    events_endpoint,
    health_endpoint,
    history_endpoint,
    http_exception_handler,
    not_found_handler,
    server_error_handler,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
    subsessions_close_endpoint,
    subsessions_get_endpoint,
    subsessions_list_endpoint,
    subsessions_message_endpoint,
    subsessions_transcript_endpoint,
    summary_endpoint,
    ui_endpoint,
)

# Re-export the same symbols as routes.__all__ (plus app / cli symbols).
# We import routes.__all__ as the authoritative list and filter to only the
# symbols that were actually imported into this module — this avoids
# duplicating the endpoint-name list across two __init__.py files.
from .routes import __all__ as _routes_all

_routes_in_scope = [name for name in _routes_all if name in globals()]

__all__ = [
    "_load_ui_html",
    "_make_lifespan",
    "_setup_observability",
    "create_agent_from_settings",
    "create_app",
    "run_server",
    "run_server_from_config",
    *_routes_in_scope,
]

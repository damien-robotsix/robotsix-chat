"""Chat SSE server — HTTP + Server-Sent Events bridge for human-to-agent chat.

Re-exports the public API that used to live in the monolithic ``server.py``,
preserving backward compatibility for all existing import sites
(``tests/``, ``src/robotsix_chat/chat/__init__.py``, etc.).
"""

from .app import (
    _load_ui_html,
    create_agent_from_settings,
    create_app,
)
from .cli import (
    run_server,
    run_server_from_config,
)
from .routes import (  # noqa: F401 — imports used via dynamic __all__ below
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_TOKEN_TYPE,
    ChatAgent,
    RunSerializer,
    events_endpoint,
)

# Re-export the same symbols as routes.__all__ (plus app / cli symbols).
# We import routes.__all__ as the authoritative list and filter to only the
# symbols that were actually imported into this module — this avoids
# duplicating the endpoint-name list across two __init__.py files.
from .routes import __all__ as _routes_all

_routes_in_scope = [name for name in _routes_all if name in globals()]

__all__ = [
    "_load_ui_html",
    "create_agent_from_settings",
    "create_app",
    "run_server",
    "run_server_from_config",
    *_routes_in_scope,
]

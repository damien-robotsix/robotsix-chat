"""Chat SSE server — HTTP + Server-Sent Events bridge for human-to-agent chat.

Exposes an LLM agent (represented by the :class:`ChatAgent` protocol) via
``POST /chat`` (SSE stream), ``GET /health`` (liveness probe), ``GET /events``
(persistent background-task event stream), and ``GET /`` (browser chat UI).
Built on Starlette so it can be tested with ``httpx.ASGITransport`` without
binding a real port.
"""

from __future__ import annotations

from .events import (
    SSE_TASK_COMPLETED_TYPE,
    SSE_TASK_FAILED_TYPE,
    SSE_TASK_STARTED_TYPE,
    EventBus,
)
from .server import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_TOKEN_TYPE,
    ChatAgent,
    create_agent_from_settings,
    create_app,
    run_server,
    run_server_from_config,
)

__all__ = [
    "ChatAgent",
    "EventBus",
    "SSE_CONTENT_TYPE",
    "SSE_DONE_TYPE",
    "SSE_ERROR_TYPE",
    "SSE_TASK_COMPLETED_TYPE",
    "SSE_TASK_FAILED_TYPE",
    "SSE_TASK_STARTED_TYPE",
    "SSE_TOKEN_TYPE",
    "create_agent_from_settings",
    "create_app",
    "run_server",
    "run_server_from_config",
]

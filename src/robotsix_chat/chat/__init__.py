"""Chat SSE server — HTTP + Server-Sent Events bridge for human-to-agent chat.

Exposes an LLM agent (represented by the :class:`ChatAgent` protocol) via
``POST /chat`` (SSE stream), ``GET /health`` (liveness probe), and ``GET /``
(browser chat UI). Built on Starlette so it can be tested with
``httpx.ASGITransport`` without binding a real port.
"""

from __future__ import annotations

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
    "SSE_CONTENT_TYPE",
    "SSE_DONE_TYPE",
    "SSE_ERROR_TYPE",
    "SSE_TOKEN_TYPE",
    "create_agent_from_settings",
    "create_app",
    "run_server",
    "run_server_from_config",
]

"""robotsix-chat — browser + SSE chat server for an LLM agent.

Exposes an LLM agent (backed by ``robotsix-llmio``'s transport + model_level
factory) to human users over HTTP: ``POST /chat`` streams the response as
Server-Sent Events, ``GET /health`` is a liveness probe, and ``GET /`` serves a
self-contained browser chat UI. Built on Starlette so it can be tested with
``httpx.ASGITransport`` without binding a real port.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("robotsix-chat")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0.0.0+unknown"

PROJECT_TITLE = "robotsix-chat"

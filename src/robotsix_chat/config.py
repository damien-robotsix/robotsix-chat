"""Environment-based configuration via pydantic + python-dotenv.

Reads settings from environment variables, with optional ``.env`` file
support via ``python-dotenv``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class Settings(BaseModel):
    """Application settings loaded from environment variables.

    Attributes:
        llm_api_key: Provider API key (required, never empty).
        llm_model: Model name to use.
        llm_base_url: Optional custom base URL for the LLM provider.
        server_host: Host address the chat SSE server binds to.
        server_port: Port the chat SSE server listens on.
        log_level: Python logging level name.
        cors_allow_origins: Origins allowed to call /chat cross-origin
            (empty = none; ``["*"]`` = any). Only needed when the browser
            UI is hosted on a different origin than the server.
    """

    llm_api_key: str
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str | None = None
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    log_level: str = "INFO"
    cors_allow_origins: list[str] = []

    def model_post_init(self, __context: Any) -> None:
        """Validate fields that cannot be expressed via simple type annotations."""
        if not self.llm_api_key:
            raise ValueError(
                "LLM_API_KEY must be set — provide it via the environment "
                "variable LLM_API_KEY or a .env file"
            )

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> Settings:
        """Load settings from environment variables.

        Calls ``load_dotenv()`` first so a ``.env`` file in the working
        directory (or any parent directory) is picked up automatically.
        No error is raised when ``.env`` is absent — environment
        variables already exported in the shell take precedence.
        """
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:  # pragma: no cover — python-dotenv is a required dep
            logger.debug("python-dotenv not installed; skipping .env loading")

        raw: dict[str, Any] = {}

        raw["llm_api_key"] = os.getenv("LLM_API_KEY", "")
        raw["llm_model"] = os.getenv("LLM_MODEL", "gpt-4o-mini")

        base_url = os.getenv("LLM_BASE_URL")
        raw["llm_base_url"] = base_url if base_url else None

        raw["server_host"] = os.getenv("SERVER_HOST", "127.0.0.1")

        port_str = os.getenv("SERVER_PORT", "8000")
        try:
            raw["server_port"] = int(port_str)
        except ValueError:
            raise ValueError(
                f"SERVER_PORT must be an integer, got {port_str!r}"
            ) from None

        raw["log_level"] = os.getenv("LOG_LEVEL", "INFO")

        cors_raw = os.getenv("CORS_ALLOW_ORIGINS", "")
        raw["cors_allow_origins"] = [
            origin.strip() for origin in cors_raw.split(",") if origin.strip()
        ]

        return cls(**raw)

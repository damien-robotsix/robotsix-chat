"""Session endpoints — list, create, delete, close, and history."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import session_model_frame
from robotsix_chat.config.constants import (
    FRONTIER_MODEL_LEVEL,
    level_display_name,
    level_needs_api_key,
)
from robotsix_chat.periodic import PERIODIC_OWNER
from robotsix_chat.subsessions.registry import OWNER_CLOSED_REASON

from ._shared import _get_session_id, _parse_json_body
from .chat import ChatAgent

# Keep strong refs to fire-and-forget finalize tasks (GC guard).
_finalize_tasks: set[asyncio.Task[bool]] = set()

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from robotsix_chat.subsessions import SubsessionRegistry


def _app_tier_config(request: Request) -> Any:
    """Chat's own llmio tier config (incl. ``llmio_tier_overrides``).

    Every place that renders a model NAME for a level must resolve it
    against this config, not llmio's baked defaults — an operator override
    (e.g. binding fallback level 2 to a different snapshot) otherwise shows
    one model in the session badge while another actually serves the turn.
    Returns ``None`` when tier resolution is unavailable (test doubles) so
    ``level_display_name`` falls back to the defaults.
    """
    try:
        from robotsix_llmio.config import load_tier_config

        from robotsix_chat.llm.agent import _merge_tier_overrides

        overrides = getattr(request.app.state, "llmio_tier_overrides", None)
        return load_tier_config(overrides or {})
    except Exception:
        return None

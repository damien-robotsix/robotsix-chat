"""Direct board-API ticket poll tool — fallback when component_request is unavailable.

Provides ``ticket_poll(ticket_id)``, a dedicated tool that queries the mill
board API directly via HTTP (bypassing the component roster) and returns the
ticket's current state.  The tool is always available when
``board_api_base_url`` is configured, so periodic ticket monitors have a
reliable fallback when ``component_request`` is absent from the agent's tool
list.

Exposes :func:`build_ticket_poll_tools` — a factory returning the LLM tool.
Returns no tools when ``board_api_base_url`` is empty.  Also exposes
:func:`load_ticket_poll_skill` which returns the component skill markdown
for injection into the agent instruction.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from robotsix_chat.config import Settings

__all__ = ["build_ticket_poll_tools", "load_ticket_poll_skill"]

logger = logging.getLogger(__name__)


def load_ticket_poll_skill() -> str:
    """Return the ticket-poll component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.
    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_ticket_poll_tools(
    settings: Settings,
) -> list[Callable[..., Any]]:
    """Return the ``ticket_poll`` tool, or an empty list when unavailable.

    The tool is available whenever ``direct_repo.board_api_base_url`` is
    non-empty — it does NOT depend on ``central_deploy.url`` or the
    component roster, so it serves as a fallback when ``component_request``
    is absent.

    Args:
        settings: Full application settings.

    Returns:
        A single-element list containing the ``ticket_poll`` async callable,
        or ``[]`` when ``board_api_base_url`` is empty.

    """
    board_url = settings.direct_repo.board_api_base_url.strip()
    if not board_url:
        return []

    board_url = board_url.rstrip("/")
    board_token = settings.direct_repo.board_api_token.get_secret_value()
    timeout = settings.direct_repo.timeout

    async def ticket_poll(ticket_id: str) -> str:
        """Poll the mill board for a ticket's current state.

        Directly queries the board API (bypasses the component roster).
        Use this when ``component_request`` is unavailable or as an
        independent verification of ticket state.

        Args:
            ticket_id: The ticket identifier (e.g. "20250101T120000Z-my-ticket-a1b2").

        Returns:
            A JSON string with ``ticket_id``, ``state`` (or ``null`` when
            the field is absent), and ``error`` (empty on success).  On
            connectivity failure the ``state`` is ``null`` and ``error``
            contains a diagnostic message.

        """
        url = f"{board_url}/tickets/{ticket_id}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                try:
                    data: dict[str, Any] = response.json()
                except json.JSONDecodeError, TypeError:
                    return json.dumps(
                        {
                            "ticket_id": ticket_id,
                            "state": None,
                            "error": "Non-JSON response from board API",
                        },
                        ensure_ascii=False,
                    )
                state = data.get("state")
                return json.dumps(
                    {
                        "ticket_id": ticket_id,
                        "state": state,
                        "error": "",
                    },
                    ensure_ascii=False,
                )
        except httpx.HTTPStatusError as exc:
            return json.dumps(
                {
                    "ticket_id": ticket_id,
                    "state": None,
                    "error": f"Board API returned HTTP {exc.response.status_code}",
                },
                ensure_ascii=False,
            )
        except httpx.TimeoutException:
            return json.dumps(
                {
                    "ticket_id": ticket_id,
                    "state": None,
                    "error": f"Board API request timed out after {timeout}s",
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.warning("ticket_poll failed for %s: %s", ticket_id, exc)
            return json.dumps(
                {
                    "ticket_id": ticket_id,
                    "state": None,
                    "error": f"Board API request failed: {exc}",
                },
                ensure_ascii=False,
            )

    return [ticket_poll]

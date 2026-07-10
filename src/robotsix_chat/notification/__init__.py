"""Push notification tool — lets the agent alert the user proactively.

When a background subsession needs user awareness or action (a decision
escalation, a completed task, a blocking condition), the agent calls
``notify_user`` which pushes a concise alert — one-line summary +
optional link — to a delivery provider (ntfy by default).

Exposes :func:`build_notification_tools` — a factory returning the LLM
tool that pushes notifications.  Returns no tools when disabled, so the
chat runs exactly as before.  Also exposes :func:`load_notification_skill`
which returns the component skill markdown for injection into the agent
instruction.

Trigger points (per agreed notification strategy):
1. Subsession chat opens — a user_chat subsession was spawned and is
   waiting for the user's input.
2. Subsession completes or raises something — a task or periodic
   subsession finished, was blocked, or surfaced a condition the user
   must be informed of.
3. State/result requiring user awareness — anything blocking coherence
   or needing explicit user action.

The tool is safe to call from subsessions as well as the main
conversation (subsessions share the agent's tool surface).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from robotsix_chat.config import NotificationSettings

logger = logging.getLogger(__name__)

__all__ = ["build_notification_tools", "load_notification_skill"]


def load_notification_skill() -> str:
    """Return the notification component skill markdown.

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


def build_notification_tools(
    settings: NotificationSettings,
) -> list[Callable[..., Any]]:
    """Return the notification tool for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    ntfy_topic = settings.ntfy_topic
    ntfy_token = settings.ntfy_token.get_secret_value()
    ntfy_server = settings.ntfy_server.rstrip("/")
    timeout = settings.timeout
    publish_url = f"{ntfy_server}/{ntfy_topic}"
    headers: dict[str, str] = {}
    if ntfy_token:
        headers["Authorization"] = f"Bearer {ntfy_token}"

    async def notify_user(
        title: str,
        body: str,
        urgency: str = "default",
        link: str = "",
    ) -> str:
        """Push a concise notification to the user's device.

        Use this to proactively alert the user when something needs their
        awareness or action outside the active conversation flow.  Keep
        messages concise — a one-line summary with an optional link/reference
        (ticket id, PR URL, subsession id), not full-history dumps.

        **When to call this tool (only these three trigger classes, or
        explicit user request):**
        1. A ``user_chat`` subsession was spawned and is waiting for the
           user's input (a decision escalation).
        2. A task or periodic subsession finished, was blocked, or surfaced
           a condition the user must be informed of (e.g. "ticket approved
           and merged", "monitor found a failure", "decision needed").
        3. A state or result requires explicit user action (blocked
           subsession, capability gap filed as ticket, missing context).

        **Do NOT call for routine completions** — use the ``urgency`` field
        to distinguish routine from attention-required alerts (``"low"`` for
        routine completions, ``"default"`` for standard notifications,
        ``"high"`` for urgent attention).

        Args:
            title: One-line notification title (required, keep it short).
            body: The notification message body (required, concise summary
                of what happened and what action is needed).
            urgency: Severity level — ``"low"`` (routine), ``"default"``
                (standard), or ``"high"`` (urgent attention). Default is
                ``"default"``.
            link: Optional URL or reference (ticket id, PR URL, subsession
                id). Leave empty when no link is relevant.

        Returns:
            ``"Notification sent."`` on success, or an error message when
            the push failed.

        """
        if urgency not in ("low", "default", "high"):
            urgency = "default"

        payload: dict[str, str] = {
            "title": title,
            "message": body,
            "priority": urgency,
        }
        if link:
            payload["click"] = link

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    publish_url,
                    json=payload,
                    headers=headers,
                )
                if resp.is_success:
                    return "Notification sent."
                logger.warning(
                    "Notification push returned %s: %s",
                    resp.status_code,
                    resp.text[:500],
                )
                return f"Notification failed — server returned HTTP {resp.status_code}."
        except httpx.TimeoutException:
            logger.exception("Notification push timed out")
            return "Notification failed — request timed out."
        except Exception:
            logger.exception("Notification push error")
            return "Notification failed — unexpected error."

    return [notify_user]

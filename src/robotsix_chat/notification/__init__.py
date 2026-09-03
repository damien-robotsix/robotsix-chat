"""Push notification tool — lets the agent alert the user proactively.

When a background subsession needs user awareness or action (a decision
escalation, a completed task, a blocking condition), the agent calls
``notify_user`` which publishes a notification event to connected clients
over the existing SSE channel (EventBus).  The user's browser (or mobile
app in future) renders the event via the native Notifications API.

Delivery only reaches clients that are currently connected — notifications
are silently dropped when no browser is listening for the session.

Exposes :func:`build_notification_tools` — a factory returning the LLM
tool that publishes notifications.  Returns no tools when disabled, so the
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

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE

if TYPE_CHECKING:
    from robotsix_chat.chat.events import EventSink
    from robotsix_chat.config.models import NotificationSettings
    from robotsix_chat.notification.store import NotificationStore

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


def _has_active_subscribers(event_sink: EventSink, session_id: str) -> bool:
    """Return True when at least one SSE client is subscribed to *session_id*.

    The concrete :class:`~robotsix_chat.chat.events.EventBus` exposes
    :meth:`~robotsix_chat.chat.events.EventBus.subscriber_count`; sinks
    without subscriber visibility (e.g. test spies) are treated as having
    no live subscriber, so the record is stored undelivered and would be
    replayed to a later-connecting client.
    """
    count = getattr(event_sink, "subscriber_count", None)
    return count is not None and count(session_id) > 0


def build_notification_tools(
    settings: NotificationSettings,
    event_sink: EventSink,
    session_id: str,
    store: NotificationStore | None = None,
) -> list[Callable[..., Any]]:
    """Return the notification tool for the agent, or ``[]`` when disabled.

    Args:
        settings: Notification config (the ``enabled`` master switch).
        event_sink: The per-session SSE bus notifications are published to.
        session_id: The chat session the notification is scoped to.
        store: Optional shared :class:`NotificationStore` persisting each
            notification to chat-data so undelivered notifications survive
            a disconnected browser.  When ``None``, notifications are
            published but not persisted (legacy behaviour).

    """
    if not settings.enabled:
        return []

    async def notify_user(
        title: str,
        body: str,
        urgency: str = "default",
        link: str = "",
    ) -> str:
        """Push a concise notification to the user's connected browser.

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

        Delivery only reaches clients that are currently connected — when no
        browser is listening, the live SSE event is dropped but the
        notification is persisted so it can be replayed to the next
        connecting client.

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
            ``"Notification sent."`` on success.  Publication is always
            successful — frames are silently dropped when no client is
            connected, and a persistence failure is logged without
            breaking the publish.

        """
        if urgency not in ("low", "default", "high"):
            urgency = "default"

        frame: dict[str, object] = {
            "type": SSE_NOTIFICATION_TYPE,
            "title": title,
            "body": body,
            "urgency": urgency,
            "link": link,
        }
        # Snapshot delivery state at publish time.  There is no ``await``
        # between this check and the publish below, so the subscriber count
        # cannot change mid-call.
        delivered = _has_active_subscribers(event_sink, session_id)
        if store is not None and settings.store_and_forward:
            try:
                record = store.append(title=title, body=body, source_session=session_id)
                if delivered:
                    store.mark_delivered([record.id])
            except Exception:
                logger.exception(
                    "Failed to persist notification (session=%s) — continuing",
                    session_id,
                )
        event_sink.publish(session_id, frame)
        return "Notification sent."

    return [notify_user]

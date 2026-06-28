"""Talk to robotsix-calendar-agent over the agent-comm broker.

Creates a :class:`~robotsix_agent_comm.sdk.BrokeredAgent` and uses its
``send_request`` method to forward natural-language calendar/task requests
directly to the calendar agent.  Adds a TTL cache for query results so that
repeated ``query_calendar`` / ``query_tasks`` calls within a check-loop
tick cycle avoid redundant broker round-trips.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from robotsix_chat.broker_client import BrokerUnavailableError, _is_broker_unavailable

if TYPE_CHECKING:
    from robotsix_chat.config import CalendarSettings

logger = logging.getLogger(__name__)


class CalendarClient:
    """Forwards natural-language calendar/task requests to the calendar agent.

    Uses a :class:`~robotsix_agent_comm.sdk.BrokeredAgent` started in pull
    (mailbox) mode so replies from the pull-mode calendar agent are received
    reliably.  The agent is started once at construction time and lives for
    the process lifetime.
    """

    # robotsix-calendar-agent reads the request text from the ``"instruction"``
    # key (not the board-manager's ``"message"``); see its agent.process().
    _request_key = "instruction"

    def __init__(self, settings: CalendarSettings) -> None:
        """Create a BrokeredAgent, start it, and initialise the query cache."""
        from robotsix_agent_comm.sdk import BrokeredAgent  # noqa: I001

        self._target_agent_id = settings.calendar_agent_id
        self._agent = BrokeredAgent(
            settings.agent_id,
            broker_host=settings.broker_host,
            broker_port=settings.broker_port,
            broker_scheme=settings.broker_scheme,
            broker_token=settings.broker_token,
            timeout=settings.timeout,
        )
        try:
            self._agent.start()
        except Exception:
            logger.warning(
                "Failed to start calendar BrokeredAgent (agent_id=%s) — "
                "the broker may already have a registration for this id. "
                "Outbound send_request calls may fail.",
                settings.agent_id,
                exc_info=True,
            )
        self._cache_ttl = settings.cache_ttl
        self._cache: dict[str, tuple[float, str]] = {}

    def invalidate_cache(self, domain: str) -> None:
        """Clear cached query results for *domain* (``"calendar"`` or ``"tasks"``).

        Called by the ``manage_calendar`` / ``manage_tasks`` tools after a
        mutation so the next query fetches fresh data from the broker.
        """
        prefix = f"{domain}:"
        stale = [k for k in self._cache if k.startswith(prefix)]
        for k in stale:
            del self._cache[k]

    async def consult(
        self,
        request: str,
        *,
        empty_reply: str = "No request text provided.",
        **extra_payload: object,
    ) -> str:
        """Send *request* to the calendar agent under *domain* and return its reply.

        *domain* is ``"calendar"`` or ``"tasks"`` — passed in the payload so the
        calendar agent can route to the right CalDAV object type (``VEVENT`` vs
        ``VTODO``).

        Query results are cached for ``cache_ttl`` seconds (keyed by
        ``domain:request``).  Call :meth:`invalidate_cache` after a mutation to
        force a fresh fetch on the next query.

        May raise :class:`BrokerUnavailableError` when the broker cannot reach
        the calendar agent.  All other errors are caught and returned as text.
        """
        domain = str(extra_payload.get("domain", ""))

        # Check the query cache before hitting the broker.
        cache_key = f"{domain}:{request}"
        now = time.monotonic()
        if cache_key in self._cache:
            ts, cached = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                return cached

        if not request.strip():
            return empty_reply

        payload: dict[str, object] = {self._request_key: request, **extra_payload}
        try:
            reply_msg = await asyncio.to_thread(
                self._agent.send_request, self._target_agent_id, payload
            )
            result = self._extract_reply_text(reply_msg)
        except Exception as exc:  # noqa: BLE001 — surface as text, never crash chat
            if _is_broker_unavailable(exc):
                logger.warning(
                    "calendar (%s) consult failed (broker unavailable): %s",
                    domain,
                    exc,
                )
                raise BrokerUnavailableError(str(exc)) from exc
            logger.warning("calendar (%s) consult failed: %s", domain, exc)
            return f"The calendar ({domain}) request could not be completed: {exc}"

        # Cache successful results (only cache non-error responses — errors
        # are transient and should not poison the cache).
        is_error = (
            result.startswith("The calendar (")
            and "request could not be completed" in result
        )
        if not is_error:
            self._cache[cache_key] = (time.monotonic(), result)

        return result

    @staticmethod
    def _extract_reply_text(message: Any) -> str:
        """Extract the reply string from a broker ``Message`` (Response or Error).

        Tries ``body["reply"]`` first (the calendar agent's convention),
        then ``str(body)`` as a fallback.
        """
        body: dict[str, Any] = getattr(message, "body", None) or {}
        if isinstance(body, dict):
            reply = body.get("reply", "")
            if reply:
                return str(reply)
            return str(body)
        return str(body)

"""Talk to robotsix-calendar-agent over the agent-comm broker.

Thin subclass of :class:`~robotsix_chat.broker_client.BaseBrokeredClient` that
wires the calendar-specific target agent ID, default reply, and the ``domain``
payload key for CalDAV object routing.  Adds a TTL cache for query results so
that repeated ``query_calendar`` / ``query_tasks`` calls within a check-loop
tick cycle avoid redundant broker round-trips.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from robotsix_chat.broker_client import BaseBrokeredClient

if TYPE_CHECKING:
    from robotsix_chat.config import CalendarSettings


class CalendarClient(BaseBrokeredClient):
    """Forwards natural-language calendar/task requests to the calendar agent."""

    # robotsix-calendar-agent reads the request text from the ``"instruction"``
    # key (not the board-manager's ``"message"``); see its agent.process().
    _request_key = "instruction"

    def __init__(self, settings: CalendarSettings) -> None:
        """Store the calendar broker settings, build a brokered requester."""
        super().__init__(
            settings,
            target_agent_id=settings.calendar_agent_id,
            default_reply="The calendar agent returned no reply.",
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
        empty_reply: str = "",
        error_label: str = "",
        **extra_payload: object,
    ) -> str:
        """Send *request* to the calendar agent under *domain* and return its reply.

        *domain* is ``"calendar"`` or ``"tasks"`` — passed in the payload so the
        calendar agent can route to the right CalDAV object type (``VEVENT`` vs
        ``VTODO``).

        Query results are cached for ``cache_ttl`` seconds (keyed by
        ``domain:request``).  Call :meth:`invalidate_cache` after a mutation to
        force a fresh fetch on the next query.

        May raise :class:`BrokerUnavailableError` (propagated from
        :meth:`BaseBrokeredClient.consult`) when the broker cannot reach the
        calendar agent.  All other errors are caught and returned as text.
        """
        domain = str(extra_payload.get("domain", ""))

        # Check the query cache before hitting the broker.
        cache_key = f"{domain}:{request}"
        now = time.monotonic()
        if cache_key in self._cache:
            ts, cached = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                return cached

        # Pass through to the base implementation.
        result = await super().consult(
            request,
            empty_reply=f"No request was provided to send to the {domain} agent.",
            error_label=f"calendar ({domain})",
            domain=domain,
        )

        # Cache successful results (only cache non-error responses — errors
        # are transient and should not poison the cache).
        is_error = (
            result.startswith("The calendar (")
            and "request could not be completed" in result
        )
        if not is_error:
            self._cache[cache_key] = (time.monotonic(), result)

        return result

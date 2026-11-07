"""Ticket-scoped queries, dedup and mill-event routing (:class:`RegistryTickets`).

Split out of :mod:`robotsix_chat.subsessions.registry`; the parent
:class:`~.registry.SubsessionRegistry` keeps thin delegation stubs so
existing callers keep working.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .models import SubsessionInfo, SubsessionKind, SubsessionStatus
from .registry_checkpoint import user_chat_ticket_id
from .registry_constants import _TICKET_TERMINAL_CLOSE_REASONS, DECISION_GATE_STATES

logger = logging.getLogger(__name__)


class _RegistryCallbacks(Protocol):
    """The parent-registry surface :class:`RegistryTickets` calls back into.

    Structural type for :class:`~.registry.SubsessionRegistry`, declared
    here so this module never imports ``registry`` (avoids an import cycle).
    """

    def cancel_and_close(
        self,
        sub_id: str,
        *,
        reason: str,
        closed_by: str = ...,
        summary: str | None = ...,
    ) -> SubsessionInfo | None:
        """Cancel the running task for *sub_id* and close it."""

    def enqueue_message(self, sub_id: str, role: str, text: str) -> bool:
        """Queue a message for the subsession's next turn boundary."""

    def find_paused_periodic_by_ticket_id(self, ticket_id: str) -> list[SubsessionInfo]:
        """Return live ``PAUSED`` monitor subsessions tracking *ticket_id*."""


class RegistryTickets:
    """Ticket-scoped queries, dedup-key tracking and mill-event routing.

    Owns the ``_active_dedup_keys`` and ``_event_waiters`` indexes (shared
    refs created by the parent :class:`SubsessionRegistry`) and calls back
    into the parent for closing, enqueueing and paused-monitor lookups.
    """

    def __init__(
        self,
        *,
        subs: dict[str, SubsessionInfo],
        active_dedup_keys: dict[str, str],
        event_waiters: dict[str, set[str]],
        registry: _RegistryCallbacks,
    ) -> None:
        """All mappings are shared refs; *registry* is the parent."""
        self._subs = subs
        self._active_dedup_keys = active_dedup_keys
        self._event_waiters = event_waiters
        self._registry = registry

    def is_dedup_key_active(self, dedup_key: str) -> str | None:
        """Return the active subsession id for *dedup_key*, or ``None``.

        Subsessions of any kind with a matching dedup_key are tracked.
        Returns ``None`` when the key is unknown or the tracked subsession
        has become terminal (the close/fail path cleans up proactively,
        but this is a safety net for races).
        """
        sub_id = self._active_dedup_keys.get(dedup_key)
        if sub_id is None:
            return None
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            # Stale entry — clean up proactively.
            self._active_dedup_keys.pop(dedup_key, None)
            return None
        return sub_id

    def find_active_user_chat_by_ticket_id(self, ticket_id: str) -> str | None:
        """Return the id of an active USER_CHAT panel about *ticket_id*, else None.

        Matches on the panel's ``dedup_key`` when that is the ticket id, or on
        the first full ticket id in its title/prompt — so a second run that
        picked a different key (or none) still finds the operator's open panel.
        """
        for info in self._subs.values():
            if info.is_active and user_chat_ticket_id(info) == ticket_id:
                return info.id
        return None

    def close_user_chats_for_moved_ticket(
        self, ticket_id: str, old_state: str, new_state: str
    ) -> int:
        """Close open decision panels whose ticket left the human gates.

        A panel exists to collect ONE decision about a gated ticket; when the
        mill reports the ticket in a non-gated state the decision has been
        taken elsewhere (approved by the drain, resumed, closed) and the panel
        would otherwise sit in the operator's list until closed by hand.
        Returns the number of panels closed.
        """
        if not new_state or new_state.lower() in DECISION_GATE_STATES:
            return 0
        closed = 0
        for info in list(self._subs.values()):
            if not info.is_active or user_chat_ticket_id(info) != ticket_id:
                continue
            summary = (
                f"Decision panel closed automatically: ticket {ticket_id} moved "
                f"from '{old_state}' to '{new_state}', so the decision it asked "
                f"for is no longer pending."
            )
            if self._registry.cancel_and_close(
                info.id, reason="ticket_moved", closed_by="system", summary=summary
            ):
                closed += 1
                logger.info(
                    "Closed user_chat %s: ticket %s left the gate (%s -> %s).",
                    info.id,
                    ticket_id,
                    old_state,
                    new_state,
                )
        return closed

    def find_active_periodic_by_ticket_id(self, ticket_id: str) -> str | None:
        """Return the id of an active PERIODIC or WAIT_FOR_EVENT.

        subsession whose checkpoint carries *ticket_id*.

        Returns ``None`` when no match is found.

        This is a cross-reference complement to
        ``is_dedup_key_active``: a monitor may have been
        spawned without a dedup_key (the agent forgot), but
        after its first run the checkpoint records the
        watched ``ticket_id``.  When a new spawn arrives
        WITH a dedup_key, this method catches the match
        that ``is_dedup_key_active`` misses because the
        original dedup_key was never set.
        """
        for info in self._subs.values():
            if (
                info.kind
                not in (
                    SubsessionKind.PERIODIC,
                    SubsessionKind.WAIT_FOR_EVENT,
                )
                or not info.is_active
            ):
                continue
            cp = info.checkpoint
            if cp is None:
                continue
            cp_ticket_id = cp.get("ticket_id")
            if isinstance(cp_ticket_id, str) and cp_ticket_id == ticket_id:
                return info.id
        return None

    def register_event_waiter(self, sub_id: str, ticket_id: str) -> None:
        """Register *sub_id* as waiting for events on *ticket_id*."""
        self._event_waiters[ticket_id].add(sub_id)

    def unregister_event_waiter(self, sub_id: str, ticket_id: str) -> None:
        """Remove *sub_id* from the event-waiter set for *ticket_id*."""
        waiters = self._event_waiters.get(ticket_id)
        if waiters is not None:
            waiters.discard(sub_id)
            if not waiters:
                del self._event_waiters[ticket_id]

    def route_mill_event(self, ticket_id: str, event_payload: dict[str, object]) -> int:
        """Route an incoming mill state-change event to waiting monitors.

        Wakes both ``WAIT_FOR_EVENT`` monitors registered as event waiters
        and live ``PAUSED`` periodic monitors tracking the same ticket
        (auto-paused monitors whose worker is blocking on the inbox).
        """
        old_state = event_payload.get("old_state", "")
        new_state = event_payload.get("new_state", "")
        timestamp = event_payload.get("timestamp", "")
        event_text = (
            f"Mill event: ticket {ticket_id} state changed from "
            f"'{old_state}' to '{new_state}' at {timestamp}. "
            f"This event was pushed from the mill — you MUST verify "
            f"the current state via a live GET of the ticket API "
            f"before acting on it."
        )

        woken = 0
        waiters = self._event_waiters.get(ticket_id)
        if waiters:
            for sub_id in list(waiters):
                if self._registry.enqueue_message(sub_id, "system", event_text):
                    woken += 1
                else:
                    waiters.discard(sub_id)

            if not waiters:
                del self._event_waiters[ticket_id]

        # Auto-paused periodic monitors do not register as event waiters —
        # their worker blocks in ``_paused_wait_loop`` on the same inbox
        # wake mechanism, so enqueueing the event here re-arms them.
        for info in self._registry.find_paused_periodic_by_ticket_id(ticket_id):
            if self._registry.enqueue_message(info.id, "system", event_text):
                woken += 1
        # Operator decision panels about this ticket are moot once it has
        # left the human gates — close them instead of leaving them waiting.
        self.close_user_chats_for_moved_ticket(
            ticket_id, str(old_state or ""), str(new_state or "")
        )

        if woken:
            logger.info(
                "Routed mill event for ticket %s to %d monitor(s).",
                ticket_id,
                woken,
            )
        else:
            logger.debug(
                "Mill event for ticket %s had no active waiters — ignored.",
                ticket_id,
            )

        return woken

    def get_owner_session_ids_for_ticket(self, ticket_id: str) -> set[str]:
        """Return owner session ids for every active subsession tracking *ticket_id*.

        Covers WAIT_FOR_EVENT waiters, paused periodic monitors, and any
        other active PERIODIC or WAIT_FOR_EVENT subsession whose checkpoint
        carries *ticket_id*.  Used by the mill-events endpoint to target
        blocked-transition notifications to the right conversations.
        """
        sessions: set[str] = set()

        # 1) WAIT_FOR_EVENT waiters keyed directly by ticket_id.
        for sub_id in self._event_waiters.get(ticket_id, ()):
            info = self._subs.get(sub_id)
            if info is not None and info.owner_session_id:
                sessions.add(info.owner_session_id)

        # 2) Paused periodic monitors — covered by the general scan below,
        #    but included explicitly for clarity and to ensure they are
        #    never missed.
        for info in self._registry.find_paused_periodic_by_ticket_id(ticket_id):
            if info.owner_session_id:
                sessions.add(info.owner_session_id)

        # 3) Any other active PERIODIC / WAIT_FOR_EVENT subsessions whose
        #    checkpoint carries the ticket_id (catch-all).
        for info in self._subs.values():
            if (
                info.kind
                not in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT)
                or not info.is_active
            ):
                continue
            if info.owner_session_id in sessions:
                continue
            cp = info.checkpoint
            if cp is None:
                continue
            cp_ticket_id = cp.get("ticket_id")
            if isinstance(cp_ticket_id, str) and cp_ticket_id == ticket_id:
                sessions.add(info.owner_session_id)

        return sessions

    def is_duplicate_ticket_terminal(self, ticket_id: str, exclude_sub_id: str) -> bool:
        """Check for a duplicate terminal report for *ticket_id*.

        Returns ``True`` when a different (already-CLOSED) subsession has
        already reported the same ticket as terminal — when a prior monitor
        has already reported the ticket as closed/done, a second monitor's
        completion notice for that ticket is a duplicate and should be
        suppressed to avoid a redundant (and often verbose) reaction turn
        in the parent conversation.
        """
        for info in self._subs.values():
            if info.id == exclude_sub_id:
                continue
            if info.status is not SubsessionStatus.CLOSED:
                continue
            cp = info.checkpoint
            if cp is None:
                continue
            cp_ticket_id = cp.get("ticket_id")
            if not isinstance(cp_ticket_id, str) or cp_ticket_id != ticket_id:
                continue
            # Only count a prior subsession as a terminal reporter when
            # its close_reason indicates it actually reported the terminal
            # state (not a non-terminal close like pause / max_runs).
            if info.close_reason in ("ticket_terminal", "completed"):
                return True
        return False

    def has_terminal_report_for_ticket(self, ticket_id: str) -> bool:
        """Check whether a terminal monitor already reported *ticket_id*.

        Returns ``True`` when any CLOSED subsession with a matching
        ``checkpoint.ticket_id`` or ``dedup_key`` closed with a terminal
        reason (e.g. ``ticket_terminal``, ``ticket_terminal_without_pr``,
        ``ticket_terminal_on_resume``, or ``completed``).

        Used at spawn time to prevent redundant monitors for tickets
        that have already been tracked to completion.
        """
        terminal_reasons = _TICKET_TERMINAL_CLOSE_REASONS | {"completed"}
        for info in self._subs.values():
            if info.status is not SubsessionStatus.CLOSED:
                continue
            # Match via checkpoint.ticket_id (preferred) or dedup_key.
            cp = info.checkpoint
            cp_ticket_id = cp.get("ticket_id") if cp is not None else None
            matched = (
                isinstance(cp_ticket_id, str) and cp_ticket_id == ticket_id
            ) or info.dedup_key == ticket_id
            if not matched:
                continue
            if info.close_reason in terminal_reasons:
                return True
        return False

    def is_duplicate_auto_pause(self, ticket_id: str, exclude_sub_id: str) -> bool:
        """Check for a duplicate auto-pause / no-change report for *ticket_id*.

        Returns ``True`` when a different (already-PAUSED or CLOSED)
        subsession has already reported the same ticket as auto-paused,
        auto-stopped, or terminal — when a prior monitor has already
        notified the user that the ticket needs no attention, a second
        monitor's no-change notice for that ticket is a duplicate and
        should be suppressed to avoid a redundant (and often noisy)
        reaction turn in the parent conversation.
        """
        for info in self._subs.values():
            if info.id == exclude_sub_id:
                continue
            if info.status not in (
                SubsessionStatus.CLOSED,
                SubsessionStatus.PAUSED,
            ):
                continue
            cp = info.checkpoint
            if cp is None:
                continue
            cp_ticket_id = cp.get("ticket_id")
            if not isinstance(cp_ticket_id, str) or cp_ticket_id != ticket_id:
                continue
            if info.close_reason in (
                "paused",
                "no_change_auto_stop",
                "ticket_terminal",
                "completed",
            ):
                return True
        return False

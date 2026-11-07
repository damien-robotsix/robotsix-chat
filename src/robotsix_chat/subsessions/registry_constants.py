"""Module-level constants shared by the subsession registry collaborators.

Split out of :mod:`robotsix_chat.subsessions.registry` so the extracted
collaborator modules never import ``registry`` (avoids an import cycle);
that module re-exports these names so existing imports keep working.
"""

from __future__ import annotations

from robotsix_chat.ticket_poll.mill_states import HUMAN_WAIT_STATES

from .models import SubsessionKind

# Mill states in which a ticket is waiting on a human; a decision panel is
# only meaningful while its ticket sits in one of these. Once a mill event
# reports the ticket somewhere else (approved → ready, closed, re-running),
# the decision is moot and the panel is closed on the operator's behalf.
#
# This is an alias for the canonical ``HUMAN_WAIT_STATES`` set defined in
# ``robotsix_chat.ticket_poll.mill_states`` (the single source of truth for
# mill state-name sets). Aliasing — rather than re-typing the literal —
# guarantees the decision-gate logic stays in sync when a new human-wait
# state is added upstream.
DECISION_GATE_STATES = HUMAN_WAIT_STATES

# Subsession kinds that are "monitors" for slot-budget purposes: they
# persist across turns watching a ticket and occupy a per-conversation
# slot while active or paused.  One-shot task/user_chat subsessions are
# transient work, not monitors, and never occupy a slot.
_MONITOR_KINDS: frozenset[SubsessionKind] = frozenset(
    {
        SubsessionKind.PERIODIC,
        SubsessionKind.WAIT_FOR_EVENT,
    }
)

# Close reasons that indicate the monitored ticket reached a terminal
# (resolved / completed) state — the prerequisite is done, so dependents
# should NOT be paused.
_TICKET_TERMINAL_CLOSE_REASONS: frozenset[str] = frozenset(
    {
        "ticket_terminal",
        "ticket_terminal_without_pr",
        "ticket_terminal_on_resume",
        "completed",
    }
)

# Close reason used when a whole conversation (owner) is torn down.  The
# slot-budget manager treats it specially: pending monitor requests are
# discarded instead of being drained into a dead session.
OWNER_CLOSED_REASON = "session closed"


# Cap on persisted (turn_input, reply) pairs per subsession — must match
# worker._MAX_WORKER_HISTORY_TURNS (the replay window the worker actually
# feeds the agent); capping here too bounds what's kept in the JSON store.
_MAX_TURN_HISTORY_ENTRIES = 20


__all__ = [
    "DECISION_GATE_STATES",
    "OWNER_CLOSED_REASON",
    "_MAX_TURN_HISTORY_ENTRIES",
    "_MONITOR_KINDS",
    "_TICKET_TERMINAL_CLOSE_REASONS",
]

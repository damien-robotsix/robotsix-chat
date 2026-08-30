"""The robotsix-mill ticket state machine, as seen by the chat tools.

Single source of truth for the state names the mill board API returns
(``robotsix_mill.core.states.State``).  Every chat-side check that reasons
about a ticket's lifecycle — "was this ticket ever worked on?", "is it
terminal?", "is it waiting on a human?" — must use these sets instead of
re-typing state names, so a renamed or invented state cannot silently turn
every delivered ticket into a false alarm.

Normal delivery path::

    draft → (human_issue_approval) → ready → code_review → documenting
          → deliverable → implement_complete
          → (human_mr_approval | waiting_auto_merge | fixing_ci | rebasing
             | addressing_review)
          → done → closed

State strings are compared case-insensitively via :func:`normalize_state`.
"""

from __future__ import annotations

__all__ = [
    "ACTIVE_WORK_STATES",
    "ALL_STATES",
    "HUMAN_WAIT_STATES",
    "MERGE_STATES",
    "OPEN_STATES",
    "PRE_WORK_STATES",
    "TERMINAL_STATES",
    "normalize_state",
]

#: States before any agent has touched the ticket.  A ticket that goes
#: straight from one of these to ``closed`` was never implemented.
PRE_WORK_STATES: frozenset[str] = frozenset({"draft", "human_issue_approval", "ready"})

#: States that prove an agent picked the ticket up and produced work
#: (implementation, review, documentation, or a PR under repair).
ACTIVE_WORK_STATES: frozenset[str] = frozenset(
    {
        "code_review",
        "documenting",
        "deliverable",
        "implement_complete",
        "rebasing",
        "fixing_ci",
        "addressing_review",
        "blocked",
        "errored",
        "awaiting_user_reply",
    }
)

#: States in which a PR/MR exists and is on its way to (or has reached) the
#: default branch.  Any of these in a ticket's history means the work was
#: delivered as a PR.
MERGE_STATES: frozenset[str] = frozenset(
    {
        "implement_complete",
        "human_mr_approval",
        "waiting_auto_merge",
        "done",
    }
)

#: States where the pipeline is parked on a human decision.
HUMAN_WAIT_STATES: frozenset[str] = frozenset(
    {
        "human_issue_approval",
        "human_mr_approval",
        "awaiting_user_reply",
        "blocked",
    }
)

#: Terminal outcomes.  ``done`` = PR merged, awaiting retrospect;
#: ``closed`` = retrospected, pipeline complete.
TERMINAL_STATES: frozenset[str] = frozenset(
    {"done", "closed", "answered", "epic_closed"}
)

#: Every state the mill can report (superset of the sets above).
ALL_STATES: frozenset[str] = (
    PRE_WORK_STATES
    | ACTIVE_WORK_STATES
    | MERGE_STATES
    | HUMAN_WAIT_STATES
    | TERMINAL_STATES
    | frozenset({"asked", "epic_open"})
)

#: States that count as "still open" for queue-wide sweeps.
OPEN_STATES: frozenset[str] = ALL_STATES - TERMINAL_STATES


def normalize_state(value: object) -> str:
    """Return *value* as a lower-case state string (``""`` when not a string)."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower()

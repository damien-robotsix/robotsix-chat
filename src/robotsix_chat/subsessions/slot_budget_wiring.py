"""Slot-budget wiring between a :class:`SubsessionEnv` and its registry.

:func:`attach_slot_budget` installs the per-conversation
:class:`~robotsix_chat.subsessions.slot_budget.SlotBudget` on an env and
hooks the registry so a freed slot drains the oldest queued spawn request.
"""

from __future__ import annotations

import logging

from .models import SubsessionInfo
from .registry import OWNER_CLOSED_REASON
from .slot_budget import SLOT_BUDGET_QUEUED, SlotBudget
from .subsession_types import SubsessionEnv

logger = logging.getLogger(__name__)


def attach_slot_budget(env: SubsessionEnv) -> SlotBudget | None:
    """Create and wire the per-conversation slot-budget manager into *env*.

    Returns ``None`` (and wires nothing) when slot budgeting is disabled
    by config (``monitor_slot_budget <= 0``).  Otherwise sets
    ``env.slot_budget`` and registers a registry close callback so the
    pending queue drains whenever a monitor terminates and a slot frees.
    """
    cfg = env.settings.subsessions
    budget = getattr(cfg, "monitor_slot_budget", 0)
    if budget <= 0:
        return None
    slot_budget = SlotBudget(
        budget=budget,
        queue_max=getattr(cfg, "monitor_slot_queue_max", 32),
    )
    env.slot_budget = slot_budget

    def _on_monitor_closed(info: SubsessionInfo) -> None:
        if info.close_reason == "slot_reclaimed":
            # The freed slot is being repurposed by the in-flight spawn
            # that triggered the reclamation — draining here would admit
            # a second monitor into the same slot.
            return
        if info.close_reason == OWNER_CLOSED_REASON:
            # The conversation itself is being torn down — drop any
            # pending monitor requests rather than spawning work for a
            # dead session.
            slot_budget.discard(info.owner_session_id)
            return
        _drain_slot_budget_queue(env, info.owner_session_id)

    env.registry.add_close_callback(_on_monitor_closed)
    return slot_budget


def _drain_slot_budget_queue(env: SubsessionEnv, owner_session_id: str) -> None:
    """Dequeue and spawn pending monitor requests while slots are free.

    Called after a monitor terminates (its occupied slot frees).  Spawns
    the oldest pending request per iteration and repeats until the queue
    is empty or the conversation is back at budget.
    """
    # Lazy import: ``worker`` imports this module at load time.
    from .worker import spawn_subsession

    slot_budget = env.slot_budget
    if slot_budget is None or not slot_budget.enabled:
        return
    while env.registry.count_occupied_for_owner(owner_session_id) < slot_budget.budget:
        request = slot_budget.pop_next(owner_session_id)
        if request is None:
            return
        try:
            sub_id = spawn_subsession(env=env, **request)
        except Exception:
            logger.exception(
                "slot budget: failed to drain queued monitor request "
                "for conversation %s — request re-queued",
                owner_session_id,
            )
            # Keep the request's FIFO position and stop: retrying the
            # whole queue here would risk a failure cascade.
            slot_budget.requeue_front(owner_session_id, request)
            return
        if sub_id == SLOT_BUDGET_QUEUED:
            # Defensive: the freed slot was consumed concurrently and the
            # request re-queued itself — stop draining to avoid a loop.
            return
        logger.info(
            "slot budget: drained queued %s monitor request for "
            "conversation %s into freed slot (%s)",
            request.get("kind"),
            owner_session_id,
            sub_id,
        )

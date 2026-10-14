"""Subsession spawn pre-authorization, turn-budget, and model-level checks."""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

from .models import SubsessionKind, SubsessionLevelError

if TYPE_CHECKING:
    from robotsix_chat.config import KindTurnBudget, TurnBudgetSettings


def _is_ticket_pre_authorized(
    ticket_id: str,
    patterns: list[str],
) -> bool:
    """Return ``True`` if *ticket_id* matches any glob pattern in *patterns*.

    Uses :func:`fnmatch.fnmatch` for case-sensitive glob matching.
    An empty *patterns* list always returns ``False``.
    """
    if not patterns:
        return False
    if not ticket_id:
        return False
    return any(fnmatch.fnmatch(ticket_id, p) for p in patterns)


def _get_kind_turn_budget(
    budgets: TurnBudgetSettings,
    kind: SubsessionKind,
) -> KindTurnBudget | None:
    """Return the :class:`KindTurnBudget` for *kind*, or ``None``.

    ``WAIT_FOR_EVENT`` reuses the ``periodic`` budget since it is a
    variant of periodic monitoring.
    """
    if kind is SubsessionKind.TASK:
        return budgets.task
    if kind is SubsessionKind.PERIODIC or kind is SubsessionKind.WAIT_FOR_EVENT:
        return budgets.periodic
    if kind is SubsessionKind.USER_CHAT:
        return budgets.user_chat
    if kind is SubsessionKind.ON_CLOSE:
        return budgets.on_close
    return None


def _validate_model_level(model_level: int) -> None:
    """Reject invalid levels; key availability is not a spawn concern.

    Every level is served by the keyless Claude SDK default slot; the
    OpenRouter key only matters when llmio's provider failover routes a
    call to the keyed fallback slot, and a missing key there surfaces as
    a normal run failure, not a spawn error.
    """
    from robotsix_chat.config import VALID_MODEL_LEVELS

    if model_level not in VALID_MODEL_LEVELS:
        raise SubsessionLevelError(
            f"model_level must be one of {sorted(VALID_MODEL_LEVELS)}"
        )

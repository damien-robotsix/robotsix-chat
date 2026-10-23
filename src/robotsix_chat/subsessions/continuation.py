"""Per-kind continuation step for the subsession worker loop.

After each turn, :func:`_handle_kind_continuation` decides whether (and on
what input) the worker runs another turn for its subsession kind.
"""

from __future__ import annotations

from .models import (
    InboxMessage,
    SubsessionInfo,
    SubsessionKind,
)
from .subsession_types import SubsessionEnv
from .turn_runner import _run_task_turn, _run_user_chat_turn


async def _handle_kind_continuation(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    reply: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Dispatch kind-specific post-turn logic.

    Returns ``(pending, previous_result, consecutive_no_change)`` to
    continue, or ``None`` to stop (the subsession reached a terminal
    state).
    """
    if info.kind is SubsessionKind.TASK or info.kind is SubsessionKind.ON_CLOSE:
        pending = await _run_task_turn(env, sub_id, reply)
        if not pending:
            return None
        return (pending, None, 0)

    if info.kind is SubsessionKind.USER_CHAT:
        pending = await _run_user_chat_turn(env, sub_id)
        return (pending, None, 0)

    # PERIODIC
    if info.kind is SubsessionKind.PERIODIC:
        from .worker_periodic import _run_periodic_turn

        result = await _run_periodic_turn(
            env, info, sub_id, reply, previous_result, consecutive_no_change
        )
        if result is None:
            return None
        pending, previous_result, consecutive_no_change = result
        env.registry.reap_orphans()
        return (pending, previous_result, consecutive_no_change)

    # WAIT_FOR_EVENT
    if info.kind is SubsessionKind.WAIT_FOR_EVENT:
        from .subsession_waits import _run_wait_for_event_turn

        result = await _run_wait_for_event_turn(
            env, info, sub_id, reply, previous_result, consecutive_no_change
        )
        if result is None:
            return None
        pending, previous_result, consecutive_no_change = result
        env.registry.reap_orphans()
        return (pending, previous_result, consecutive_no_change)

    # Unknown kind: no continuation — stop the worker loop.
    return None

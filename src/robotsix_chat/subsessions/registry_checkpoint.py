"""Ticket-id and checkpoint-preservation helpers for the subsession registry.

Split out of :mod:`robotsix_chat.subsessions.registry`; that module
re-exports these names so existing imports keep working.
"""

from __future__ import annotations

import re

from .models import SubsessionInfo, SubsessionKind

# Full mill ticket id: ``<UTC stamp>-<slug>-<4 hex>``. Used to bind operator
# decision panels (user_chat) to the ticket they are about, whatever
# ``dedup_key`` the spawning agent chose (2026-09-08: the gate drain used
# run-scoped keys such as ``0d29-run-20260908T0745Z`` and re-opened a panel
# for every still-gated ticket every 4 h — 14 panels open, several per ticket).
TICKET_ID_RE = re.compile(r"\b\d{8}T\d{6}Z-[a-z0-9][a-z0-9-]*-[0-9a-f]{4}\b")


def user_chat_ticket_id(info: SubsessionInfo) -> str | None:
    """Return the mill ticket id a user_chat panel is about, if any.

    Prefers a ``dedup_key`` that is itself a ticket id; otherwise the first
    full ticket id mentioned in the title or prompt.
    """
    if info.kind is not SubsessionKind.USER_CHAT:
        return None
    if info.dedup_key and TICKET_ID_RE.fullmatch(info.dedup_key):
        return info.dedup_key
    m = TICKET_ID_RE.search(f"{info.title or ''}\n{info.prompt or ''}")
    return m.group(0) if m else None


# Subsession kinds that are "monitors" for slot-budget purposes: they


def _truncate(text: str, limit: int) -> str:
    """Clip *text* to *limit* characters with an ellipsis marker."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _checkpoint_ticket_id(checkpoint: dict[str, object] | None) -> str:
    """Return the ``ticket_id`` in *checkpoint* when it is a non-empty string."""
    raw = checkpoint.get("ticket_id") if checkpoint else None
    return raw if isinstance(raw, str) and raw else ""


def _preserve_event_ticket_id(
    info: SubsessionInfo, checkpoint: dict[str, object] | None
) -> dict[str, object] | None:
    """Keep the system-owned ``ticket_id`` when a WAIT_FOR_EVENT checkpoint is replaced.

    ``set_checkpoint`` (and any other checkpoint writer) replaces the whole
    dict; a replacement that omits ``ticket_id`` would leave an
    event-driven monitor unable to filter mill events after the next
    restart.  Recover the previous value from the current checkpoint or,
    failing that, the subsession's ``dedup_key`` (which for ticket
    monitors is always the ticket id).
    """
    if _checkpoint_ticket_id(checkpoint):
        return checkpoint
    ticket_id = _checkpoint_ticket_id(info.checkpoint) or info.dedup_key
    if not ticket_id:
        return checkpoint
    merged = dict(checkpoint or {})
    merged["ticket_id"] = ticket_id
    return merged


def _checkpoint_auto_stop_no_change_runs(
    checkpoint: dict[str, object] | None,
) -> int | None:
    """Return the valid per-spawn no-change threshold in *checkpoint*, if any.

    Mirrors the read in :func:`_run_periodic_turn`: only a positive
    ``int`` (excluding ``bool``, which is an ``int`` subclass) is a valid
    override — anything else is treated as absent.
    """
    raw = checkpoint.get("auto_stop_no_change_runs") if checkpoint else None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return raw


def _preserve_periodic_auto_stop_no_change_runs(
    info: SubsessionInfo, checkpoint: dict[str, object] | None
) -> dict[str, object] | None:
    """Keep a PERIODIC monitor's ``auto_stop_no_change_runs`` override on replacement.

    The override is seeded into the checkpoint at spawn time.  Because
    ``set_checkpoint`` (and other checkpoint writers) replace the whole
    dict, a replacement that omits ``auto_stop_no_change_runs`` would
    silently drop the override and let the monitor fall back to the global
    ``subsessions.auto_stop_no_change_runs`` default — re-triggering the
    premature auto-stop this override exists to prevent.  Recover the
    previous value from the current checkpoint when it is missing.
    """
    if _checkpoint_auto_stop_no_change_runs(checkpoint) is not None:
        return checkpoint
    previous = _checkpoint_auto_stop_no_change_runs(info.checkpoint)
    if previous is None:
        return checkpoint
    merged = dict(checkpoint or {})
    merged["auto_stop_no_change_runs"] = previous
    return merged


def _checkpoint_no_change_pause_count(
    checkpoint: dict[str, object] | None,
) -> int | None:
    """Return the no-change pause counter in *checkpoint*, if present.

    ``0`` is a meaningful value (progress was observed since the last
    pause), so — unlike the auto-stop override helper — this only
    distinguishes *presence* from absence and does not validate the
    magnitude.  A bool or non-int value is treated as absent.
    """
    if not checkpoint or "no_change_pause_count" not in checkpoint:
        return None
    raw = checkpoint["no_change_pause_count"]
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _preserve_periodic_no_change_pause_count(
    info: SubsessionInfo, checkpoint: dict[str, object] | None
) -> dict[str, object] | None:
    """Keep a PERIODIC monitor's no-change pause counter on replacement.

    The counter is seeded into the checkpoint by the periodic turn loop
    when the monitor auto-pauses.  ``set_checkpoint`` (and other
    checkpoint writers) replace the whole dict; a replacement that omits
    ``no_change_pause_count`` would silently reset the escalation budget
    and let a stuck monitor keep pausing forever.  Recover the previous
    value from the current checkpoint when it is missing.
    """
    if _checkpoint_no_change_pause_count(checkpoint) is not None:
        return checkpoint
    previous = _checkpoint_no_change_pause_count(info.checkpoint)
    if previous is None:
        return checkpoint
    merged = dict(checkpoint or {})
    merged["no_change_pause_count"] = previous
    return merged


def _preserve_periodic_progress_flags(
    info: SubsessionInfo, checkpoint: dict[str, object] | None
) -> dict[str, object] | None:
    """Keep a PERIODIC monitor's ``recent_progress_flags`` on replacement.

    The adaptive run-budget extension relies on the rolling progress
    window surviving ``set_checkpoint`` calls, which replace the whole
    dict.  Recover the previous window when a replacement omits it so a
    monitor that records state every run still counts progress across the
    configured ``max_runs_progress_window``.
    """
    if checkpoint is not None and isinstance(
        checkpoint.get("recent_progress_flags"), list
    ):
        return checkpoint
    previous = info.checkpoint or {}
    if not isinstance(previous.get("recent_progress_flags"), list):
        return checkpoint
    merged = dict(checkpoint or {})
    merged["recent_progress_flags"] = previous["recent_progress_flags"]
    return merged

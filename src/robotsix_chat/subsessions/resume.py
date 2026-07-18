"""Subsession resume — startup hook that respawns or interrupts persisted entries.

Called once at server startup.  Reads the registry persistence file,
respawns periodic entries, re-opens user_chat subsessions, marks
one-shot tasks as interrupted, and injects a restart notice into
each affected conversation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, TypedDict

from .models import (
    ACTIVE_STATUSES,
    SubsessionInfo,
    SubsessionKind,
    SubsessionStatus,
    TranscriptEntry,
)
from .registry import SubsessionRegistry
from .worker import spawn_subsession

if TYPE_CHECKING:
    from .worker import SubsessionEnv

logger = logging.getLogger(__name__)


# -- typed persistence accessors ------------------------------------------


def _entry_str(entry: Mapping[str, object], key: str, default: str = "") -> str:
    """Coerce a persisted-entry field to ``str`` (typed JSON accessor)."""
    value = entry.get(key, default)
    return value if isinstance(value, str) else default


def _entry_int(entry: Mapping[str, object], key: str, default: int = 0) -> int:
    """Coerce a persisted-entry field to ``int``."""
    value = entry.get(key)
    return int(value) if isinstance(value, (int, float)) else default


def _entry_float(entry: Mapping[str, object], key: str, default: float = 0.0) -> float:
    """Coerce a persisted-entry field to ``float``."""
    value = entry.get(key)
    return float(value) if isinstance(value, (int, float)) else default


def _entry_opt_int(entry: Mapping[str, object], key: str) -> int | None:
    """Coerce a persisted-entry field to ``int | None``."""
    value = entry.get(key)
    return int(value) if isinstance(value, (int, float)) else None


def _entry_opt_float(entry: Mapping[str, object], key: str) -> float | None:
    """Coerce a persisted-entry field to ``float | None``."""
    value = entry.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _entry_opt_str(entry: Mapping[str, object], key: str) -> str | None:
    """Coerce a persisted-entry field to ``str | None``."""
    value = entry.get(key)
    return value if isinstance(value, str) else None


# -- reconstruction helpers -----------------------------------------------


def _rebuild_completed_runs(entry: Mapping[str, object]) -> set[int]:
    """Reconstruct the ``completed_runs`` set from a persisted entry."""
    raw = entry.get("completed_runs")
    if isinstance(raw, list):
        return {int(v) for v in raw if isinstance(v, (int, float))}
    return set()


def _rebuild_turn_history(entry: Mapping[str, object]) -> list[tuple[str, str]]:
    """Reconstruct the ``turn_history`` replay window from a persisted entry."""
    raw = entry.get("turn_history")
    if not isinstance(raw, list):
        return []
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
        ):
            pairs.append((item[0], item[1]))
    return pairs


# -- typed dicts ----------------------------------------------------------


class _CommonEntryKwargs(TypedDict):
    """Typed dict for the common fields extracted from a persisted entry."""

    parent_id: str | None
    depth: int
    title: str
    prompt: str
    model_level: int
    interval_seconds: float | None
    include_previous_result: bool


class _ResumeFate(TypedDict):
    """Result of attempting to resume one persisted subsession entry."""

    owner_session_id: str
    sub_id: str
    kind: str
    title: str
    fate: str  # "resumed" | "interrupted"
    detail: str


# -- entry extraction helpers ---------------------------------------------


def _entry_to_common_kwargs(entry: Mapping[str, object]) -> _CommonEntryKwargs:
    """Extract common SubsessionInfo/spawn_subsession fields from a persisted entry."""
    return {
        "parent_id": _entry_opt_str(entry, "parent_id"),
        "depth": _entry_int(entry, "depth", 1),
        "title": _entry_str(entry, "title"),
        "prompt": _entry_str(entry, "prompt"),
        "model_level": _entry_int(entry, "model_level", 3),
        "interval_seconds": _entry_opt_float(entry, "interval_seconds"),
        "include_previous_result": bool(entry.get("include_previous_result")),
    }


def _entry_last_assistant_text(entry: Mapping[str, object]) -> str:
    """Extract the most recent assistant reply from a persisted entry's transcript.

    ``user_chat`` subsessions never write to ``last_result`` (only periodic
    does), but the transcript is always persisted.  This helper falls back
    through the transcript when the direct field is empty.
    """
    last_result = _entry_opt_str(entry, "last_result")
    if last_result:
        return last_result
    transcript_raw = entry.get("transcript")
    if isinstance(transcript_raw, list):
        for item in reversed(transcript_raw):
            if isinstance(item, dict) and item.get("role") == "assistant":
                text = item.get("text")
                if isinstance(text, str):
                    return text
    return ""


# -- kind-specific resume helpers -----------------------------------------


def _resume_periodic_entry(
    env: SubsessionEnv,
    entry: Mapping[str, object],
    sub_id: str,
    owner: str,
    title: str,
) -> _ResumeFate:
    """Respawn a periodic subsession under its original id."""
    completed_runs = _rebuild_completed_runs(entry)
    runs = max(completed_runs) if completed_runs else _entry_int(entry, "runs")
    spawn_subsession(
        env=env,
        kind=SubsessionKind.PERIODIC,
        owner_session_id=owner,
        **_entry_to_common_kwargs(entry),
        max_runs=_entry_opt_int(entry, "max_runs"),
        sub_id=sub_id,
        runs=runs,
        completed_runs=completed_runs,
        turn_history=_rebuild_turn_history(entry),
    )
    return _ResumeFate(
        owner_session_id=owner,
        sub_id=sub_id,
        kind="periodic",
        title=title,
        fate="resumed",
        detail="Will continue ticking on its normal schedule.",
    )


def _resume_user_chat_entry(
    env: SubsessionEnv,
    entry: Mapping[str, object],
    sub_id: str,
    owner: str,
    title: str,
) -> _ResumeFate:
    """Re-open a user_chat subsession under its original id."""
    common = _entry_to_common_kwargs(entry)
    last_text = _entry_last_assistant_text(entry)
    if last_text:
        common["prompt"] = (
            f"{common['prompt']}\n\n"
            f"[System note: this subsession was restarted after a "
            f"server restart. The assistant's last delivered state "
            f"was:]\n\n{last_text[:2000]}"
        )
    spawn_subsession(
        env=env,
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=owner,
        **common,
        sub_id=sub_id,
    )
    return _ResumeFate(
        owner_session_id=owner,
        sub_id=sub_id,
        kind="user_chat",
        title=title,
        fate="resumed",
        detail="Restarted — the conversation can continue.",
    )


def _resume_task_entry(
    env: SubsessionEnv,
    entry: Mapping[str, object],
    sub_id: str,
    owner: str,
    title: str,
) -> _ResumeFate:
    """Mark a one-shot task as interrupted — in-flight state is gone."""
    info = _restore_entry(env.registry, entry, force_active=True)
    if info is None:
        # Restore failed; return a minimal fate so the restart notice
        # still mentions this entry.
        return _ResumeFate(
            owner_session_id=owner,
            sub_id=sub_id,
            kind="task",
            title=title,
            fate="interrupted",
            detail="One-shot tasks cannot survive restarts.",
        )
    last = SubsessionRegistry.last_assistant_text(info)
    summary = "Interrupted by a server restart."
    if last:
        summary += f" Last state: {last[:500]}"
    env.registry.mark_interrupted(sub_id, summary=summary)
    interrupted_msg = (
        f"[Subsession {sub_id[:8]} ({SubsessionKind.TASK.value}) "
        f"'{info.title}' interrupted]"
    )
    env.conversation_store.record_for_session(
        owner,
        interrupted_msg,
        summary,
    )
    return _ResumeFate(
        owner_session_id=owner,
        sub_id=sub_id,
        kind="task",
        title=info.title,
        fate="interrupted",
        detail="One-shot tasks cannot survive restarts.",
    )


# -- resume entry ---------------------------------------------------------


def _resume_entry(
    env: SubsessionEnv, entry: Mapping[str, object]
) -> _ResumeFate | None:
    """Resume a single persisted registry entry (see resume_subsessions).

    Returns a :class:`_ResumeFate` describing what happened so the caller
    can build a per-conversation restart notice, or ``None`` for entries
    that were already terminal (no notice needed).
    """
    status = _entry_str(entry, "status")
    kind = SubsessionKind(_entry_str(entry, "kind", "task"))
    sub_id = _entry_str(entry, "subsession_id")
    owner = _entry_str(entry, "owner_session_id")
    if not sub_id or not owner:
        return None
    if status not in {s.value for s in ACTIVE_STATUSES}:
        _restore_entry(env.registry, entry)
        return None

    title = _entry_str(entry, "title")

    if kind is SubsessionKind.PERIODIC:
        return _resume_periodic_entry(env, entry, sub_id, owner, title)

    if kind is SubsessionKind.USER_CHAT:
        return _resume_user_chat_entry(env, entry, sub_id, owner, title)

    # task (and any future one-shot kinds): cannot be resumed.
    return _resume_task_entry(env, entry, sub_id, owner, title)


# -- restart notice injection ---------------------------------------------


def _inject_restart_notice(
    env: SubsessionEnv,
    owner_id: str,
    fates: list[_ResumeFate],
) -> None:
    """Inject a restart notice into the conversation for *owner_id*.

    Lists every affected subsession and whether it was resumed or lost,
    so the model can reconcile on its next turn (re-open unresumable
    tasks, rebuild owed decisions).
    """
    lines = [
        "[System notice: the chat service was restarted. "
        + "The following background tasks were affected:]",
        "",
    ]
    for fate in fates:
        short_id = fate["sub_id"][:8]
        kind_label = fate["kind"]
        display_title = fate["title"] or "(untitled)"
        verb = "resumed" if fate["fate"] == "resumed" else "interrupted"
        lines.append(
            f'- {kind_label.capitalize()} "{display_title}" ({short_id}): '
            f"{verb} — {fate['detail']}"
        )
    notice = "\n".join(lines)
    env.conversation_store.record_for_session(owner_id, notice, "")


# -- restore entry (re-register without launching a worker) ---------------


def _restore_entry(
    registry: SubsessionRegistry,
    entry: Mapping[str, object],
    *,
    force_active: bool = False,
) -> SubsessionInfo | None:
    """Re-register a persisted entry without launching a worker.

    Rebuilds the ``SubsessionInfo`` (including its transcript tail) via
    :meth:`SubsessionRegistry.restore`.  With *force_active* the entry is
    restored as RUNNING so a subsequent ``mark_interrupted`` transition
    is valid.
    """
    sub_id = _entry_str(entry, "subsession_id")
    if not sub_id or registry.get(sub_id) is not None:
        return None
    try:
        status = (
            SubsessionStatus.RUNNING
            if force_active
            else SubsessionStatus(_entry_str(entry, "status"))
        )
        info = SubsessionInfo(
            id=sub_id,
            kind=SubsessionKind(_entry_str(entry, "kind", "task")),
            owner_session_id=_entry_str(entry, "owner_session_id"),
            **_entry_to_common_kwargs(entry),
            status=status,
            created_at=_entry_float(entry, "created_at"),
            last_activity_at=_entry_float(entry, "last_activity_at"),
            runs=_entry_int(entry, "runs"),
            max_runs=_entry_opt_int(entry, "max_runs"),
            last_result=_entry_opt_str(entry, "last_result"),
            summary=_entry_opt_str(entry, "summary"),
            close_reason=_entry_opt_str(entry, "close_reason"),
            error=_entry_opt_str(entry, "error"),
            completed_runs=_rebuild_completed_runs(entry),
        )
    except ValueError:
        logger.warning("Skipping malformed persisted subsession %r", sub_id)
        return None
    transcript = entry.get("transcript")
    if isinstance(transcript, list):
        for item in transcript:
            if isinstance(item, dict):
                info.transcript.append(
                    TranscriptEntry(
                        role=_entry_str(item, "role"),
                        text=_entry_str(item, "text"),
                        timestamp=_entry_float(item, "timestamp"),
                    )
                )
    registry.restore(info)
    return info


# -- top-level resume hook ------------------------------------------------


def resume_subsessions(env: SubsessionEnv) -> None:
    """Startup hook: resume periodic subsessions, report interrupted ones.

    * ``periodic`` entries that were active at shutdown are respawned
      under their original id with the remaining run budget.
    * active ``task`` / ``user_chat`` entries are either resumed
      (``user_chat`` gets a fresh worker with an augmented prompt) or
      marked ``INTERRUPTED`` (``task`` — in-flight state is gone).
    * terminal entries are re-registered as-is so the UI keeps its
      recent-history view after a restart.

    After processing all persisted entries a **restart notice** is injected
    into every conversation that had live subsessions at shutdown, listing
    the affected subsessions and whether each was resumed or lost — the
    model sees this on its next turn and can reconcile (re-open unresumable
    tasks, rebuild owed decisions).
    """
    # Collect fate info per owner so we can inject one restart notice per
    # affected conversation.
    fate_by_owner: dict[str, list[_ResumeFate]] = {}
    for entry in env.registry.load_persisted():
        try:
            fate = _resume_entry(env, entry)
            if fate is not None:
                owner = fate["owner_session_id"]
                fate_by_owner.setdefault(owner, []).append(fate)
        except Exception:
            logger.exception("Could not resume subsession entry %r", entry)

    for owner_id, fates in fate_by_owner.items():
        try:
            _inject_restart_notice(env, owner_id, fates)
        except Exception:
            logger.exception("Failed to inject restart notice for owner %s", owner_id)

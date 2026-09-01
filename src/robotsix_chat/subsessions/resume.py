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
    InboxMessage,
    SubsessionInfo,
    SubsessionKind,
    SubsessionStatus,
    TranscriptEntry,
)
from .registry import SubsessionRegistry
from .worker import spawn_subsession
from .worker_mill import _TICKET_STATE_TERMINAL

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


def _entry_retry_count(entry: Mapping[str, object]) -> int:
    """Coerce a persisted-entry ``retry_count`` field to ``int``."""
    value = entry.get("retry_count")
    return int(value) if isinstance(value, (int, float)) else 0


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


def _rebuild_checkpoint(entry: Mapping[str, object]) -> dict[str, object] | None:
    """Reconstruct the ``checkpoint`` dict from a persisted entry."""
    raw = entry.get("checkpoint")
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    return None


def _rebuild_inbox(entry: Mapping[str, object]) -> list[InboxMessage]:
    """Reconstruct queued + in-flight inbox messages from a persisted entry.

    In-flight messages (drained but never completed) are returned first so
    the original delivery order is preserved across a restart.
    """
    messages: list[InboxMessage] = []
    for key in ("in_flight_inbox", "inbox"):
        raw = entry.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            text = item.get("text")
            timestamp = item.get("timestamp")
            if not isinstance(role, str) or not isinstance(text, str):
                continue
            messages.append(
                InboxMessage(
                    role=role,
                    text=text,
                    timestamp=(
                        float(timestamp) if isinstance(timestamp, (int, float)) else 0.0
                    ),
                )
            )
    return messages


def _restore_inbox(
    registry: SubsessionRegistry,
    sub_id: str,
    entry: Mapping[str, object],
) -> None:
    """Restore any persisted undelivered inbox messages for *sub_id*."""
    registry.restore_inbox(sub_id, _rebuild_inbox(entry))


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
    depends_on_ticket_id: str | None


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
        "model_level": _entry_int(entry, "model_level", 2),
        "interval_seconds": _entry_opt_float(entry, "interval_seconds"),
        "include_previous_result": bool(entry.get("include_previous_result")),
        "depends_on_ticket_id": _entry_opt_str(entry, "depends_on_ticket_id"),
    }


# Headers of the notes ``_resume_user_chat_entry`` appends to a user_chat
# prompt.  A later resume must strip them first, otherwise every restart
# stacks another copy onto the persisted prompt (observed 2026-08-22: three
# identical restart notes on one waiting user_chat, each re-driving a
# frontier-tier turn that only re-asked the operator the same question).
_RESTART_NOTE_HEADER = (
    "[System note: this subsession was restarted after a server restart. "
    "The assistant's last delivered state was:]"
)
_UNSEEN_MESSAGES_NOTE_HEADER = (
    "[System note: the following message(s) arrived after the last completed "
    "turn and may not have been seen by the assistant yet:]"
)


def _strip_restart_notes(prompt: str) -> str:
    """Return *prompt* without any resume-appended system notes.

    The notes are always appended after the original instructions, so
    cutting at the first note header restores the operator-authored prompt.
    """
    cut = len(prompt)
    for header in (_RESTART_NOTE_HEADER, _UNSEEN_MESSAGES_NOTE_HEADER):
        idx = prompt.find(header)
        if idx != -1:
            cut = min(cut, idx)
    return prompt[:cut].rstrip()


def _entry_last_assistant_text(entry: Mapping[str, object]) -> str:
    """Extract the most recent assistant reply from a persisted entry's transcript.

    ``user_chat`` subsessions never write to ``last_result`` (only periodic
    does), so this falls back through the transcript, and then through the
    replay window (``turn_history``).  The last hop matters: a resume
    re-creates the registry entry WITHOUT its transcript, so after a resume
    that ran no agent turn (a user_chat still waiting for the operator) the
    transcript is empty on the *next* restart — only ``turn_history``, which
    is carried across resumes, still knows the question was asked.
    """
    last_result = _entry_opt_str(entry, "last_result")
    if last_result:
        return last_result
    transcript_raw = entry.get("transcript")
    if isinstance(transcript_raw, list):
        for item in reversed(transcript_raw):
            if isinstance(item, dict) and item.get("role") == "assistant":
                text = item.get("text")
                if isinstance(text, str) and text:
                    return text
    turn_history = _rebuild_turn_history(entry)
    if turn_history:
        return turn_history[-1][1]
    return ""


def _entry_recent_user_texts(
    entry: Mapping[str, object],
    turn_history: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Return user/parent transcript entries posted after the last completed turn.

    ``user_chat`` subsessions transcript user messages at enqueue time but
    only append the assistant reply once a turn completes.  A restart that
    lands between those two events leaves a user message that never made it
    into ``turn_history`` — the resumed worker would otherwise never see it,
    so the resume note re-injects it.
    """
    transcript_raw = entry.get("transcript")
    if not isinstance(transcript_raw, list):
        return []

    last_reply = turn_history[-1][1] if turn_history else None
    cutoff = -1
    if last_reply:
        for idx in range(len(transcript_raw) - 1, -1, -1):
            item = transcript_raw[idx]
            if not isinstance(item, dict):
                continue
            if item.get("role") == "assistant" and item.get("text") == last_reply:
                cutoff = idx
                break

    recent: list[tuple[str, str]] = []
    for item in transcript_raw[cutoff + 1 :]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = item.get("text")
        if (
            isinstance(role, str)
            and role in {"user", "parent"}
            and isinstance(text, str)
            and text
        ):
            recent.append((role, text))
    return recent


# -- kind-specific resume helpers -----------------------------------------


# Close reasons that indicate the system auto-stopped the monitor
# (not the user or agent explicitly).  These monitors are re-spawned
# on restart so the worker can re-verify the ticket state — the
# underlying condition (no change / idle / pending approval) may have
# resolved during the outage.
_AUTO_CLOSE_REASONS: frozenset[str] = frozenset(
    {"no_change_auto_stop", "paused", "human_approval_timeout"}
)


def _handle_terminal_on_resume(
    env: SubsessionEnv,
    entry: Mapping[str, object],
    sub_id: str,
) -> bool:
    """If the ticket was already terminal, close the subsession and return True.

    Returns True when the subsession was closed (caller should return None),
    False when the subsession should proceed with resume.
    """
    checkpoint = _rebuild_checkpoint(entry)
    last_known = checkpoint.get("last_known_state") if checkpoint else None
    if isinstance(last_known, str) and last_known.lower() in _TICKET_STATE_TERMINAL:
        info = _restore_entry(env.registry, entry, force_active=True)
        if info is not None:
            env.registry.mark_closed(
                sub_id,
                summary=(
                    f"Ticket was already terminal "
                    f"('{last_known}') before restart — closed without "
                    f"resuming."
                ),
                reason="ticket_terminal_on_resume",
                closed_by="system",
            )
        return True
    return False


def _resume_periodic_entry(
    env: SubsessionEnv,
    entry: Mapping[str, object],
    sub_id: str,
    owner: str,
    title: str,
    *,
    original_status: str = "",
) -> _ResumeFate | None:
    """Respawn a periodic subsession under its original id.

    When the persisted checkpoint records a terminal ``last_known_state``
    the ticket was already finished before the restart — close the
    subsession without spawning a worker so it does not poll a ticket
    whose monitor had already been cleanly stopped.

    *original_status* (the persisted status string) is used to restore
    ``PAUSED`` monitors to their paused state after the worker is
    spawned, so the worker immediately enters the wait loop instead of
    running an agent turn.
    """
    if _handle_terminal_on_resume(env, entry, sub_id):
        return None

    checkpoint = _rebuild_checkpoint(entry)
    completed_runs = _rebuild_completed_runs(entry)
    runs = max(completed_runs) if completed_runs else _entry_int(entry, "runs")
    dedup_key = _entry_opt_str(entry, "dedup_key")
    retry_count = _entry_retry_count(entry)
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
        checkpoint=checkpoint,
        dedup_key=dedup_key,
        retry_count=retry_count,
        inbox=_rebuild_inbox(entry),
    )
    # Restore PAUSED state so the worker enters the wait loop instead of
    # running an agent turn on a subsession that was auto-paused.
    if original_status == "paused":
        info = env.registry.get(sub_id)
        if info is not None and info.status is SubsessionStatus.RUNNING:
            info.status = SubsessionStatus.PAUSED
            info.close_reason = _entry_opt_str(entry, "close_reason") or "paused"
            info.summary = _entry_opt_str(entry, "summary")
            env.registry.persist()
    return _ResumeFate(
        owner_session_id=owner,
        sub_id=sub_id,
        kind="periodic",
        title=title,
        fate="resumed",
        detail="Will continue ticking on its normal schedule.",
    )


def _resume_wait_for_event_entry(
    env: SubsessionEnv,
    entry: Mapping[str, object],
    sub_id: str,
    owner: str,
    title: str,
    *,
    original_status: str = "",  # noqa: ARG001
) -> _ResumeFate | None:
    """Respawn a wait_for_event subsession under its original id."""
    if _handle_terminal_on_resume(env, entry, sub_id):
        return None

    checkpoint = _rebuild_checkpoint(entry)
    completed_runs = _rebuild_completed_runs(entry)
    runs = max(completed_runs) if completed_runs else _entry_int(entry, "runs")
    dedup_key = _entry_opt_str(entry, "dedup_key")
    retry_count = _entry_retry_count(entry)
    event_timeout_seconds = _entry_opt_float(entry, "event_timeout_seconds")
    spawn_subsession(
        env=env,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=owner,
        **_entry_to_common_kwargs(entry),
        max_runs=_entry_opt_int(entry, "max_runs"),
        sub_id=sub_id,
        runs=runs,
        completed_runs=completed_runs,
        turn_history=_rebuild_turn_history(entry),
        checkpoint=checkpoint,
        dedup_key=dedup_key,
        retry_count=retry_count,
        event_timeout_seconds=event_timeout_seconds,
        inbox=_rebuild_inbox(entry),
    )
    return _ResumeFate(
        owner_session_id=owner,
        sub_id=sub_id,
        kind="wait_for_event",
        title=title,
        fate="resumed",
        detail="Will wait for the next ticket state-change event.",
    )


def _resume_user_chat_entry(
    env: SubsessionEnv,
    entry: Mapping[str, object],
    sub_id: str,
    owner: str,
    title: str,
) -> _ResumeFate | None:
    """Re-open a user_chat subsession under its original id."""
    if _handle_terminal_on_resume(env, entry, sub_id):
        return None

    checkpoint = _rebuild_checkpoint(entry)
    common = _entry_to_common_kwargs(entry)
    # Notes appended by an earlier resume are stale now — never stack them.
    common["prompt"] = _strip_restart_notes(common["prompt"])
    turn_history = _rebuild_turn_history(entry)
    recent_user_texts = _entry_recent_user_texts(entry, turn_history)
    last_text = _entry_last_assistant_text(entry)
    dedup_key = _entry_opt_str(entry, "dedup_key")
    retry_count = _entry_retry_count(entry)

    if last_text and not recent_user_texts:
        # The assistant already delivered its question and nobody has
        # answered yet.  Re-enter the wait for the operator's reply WITHOUT
        # an agent turn: re-driving the agent here made every server
        # restart re-ask the same question (one frontier-tier turn per
        # waiting user_chat per restart, each carrying the accumulated
        # context) while adding nothing the operator had not already seen.
        if not turn_history:
            # Give the resumed agent its own question as history so the
            # eventual reply is understood in context.
            turn_history = [(common["prompt"], last_text)]
        spawn_subsession(
            env=env,
            kind=SubsessionKind.USER_CHAT,
            owner_session_id=owner,
            **common,
            sub_id=sub_id,
            checkpoint=checkpoint,
            dedup_key=dedup_key,
            retry_count=retry_count,
            turn_history=turn_history,
            resume_waiting=True,
        )
        _restore_inbox(env.registry, sub_id, entry)
        return _ResumeFate(
            owner_session_id=owner,
            sub_id=sub_id,
            kind="user_chat",
            title=title,
            fate="resumed",
            detail=(
                "Restarted — still waiting for the operator's reply "
                "(question not re-asked)."
            ),
        )

    if recent_user_texts:
        logger.warning(
            "user_chat resume %s: %d transcript message(s) newer than the "
            "last completed turn will be re-injected into the first turn",
            sub_id,
            len(recent_user_texts),
        )
    if last_text:
        common["prompt"] = (
            f"{common['prompt']}\n\n{_RESTART_NOTE_HEADER}\n\n{last_text[:2000]}"
        )
    if recent_user_texts:
        joined = "\n\n".join(
            f"[{role}] {text[:2000]}" for role, text in recent_user_texts
        )
        common["prompt"] = (
            f"{common['prompt']}\n\n{_UNSEEN_MESSAGES_NOTE_HEADER}\n\n{joined}"
        )
    spawn_subsession(
        env=env,
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=owner,
        **common,
        sub_id=sub_id,
        checkpoint=checkpoint,
        dedup_key=dedup_key,
        retry_count=retry_count,
        turn_history=turn_history,
    )
    _restore_inbox(env.registry, sub_id, entry)
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
) -> _ResumeFate | None:
    """Re-spawn a one-shot task — it starts fresh but survives the restart.

    The task's checkpoint (if any) is carried forward so the agent can
    pick up where it left off.  The original prompt is augmented with a
    restart notice so the agent knows work may have been lost.
    """
    if _handle_terminal_on_resume(env, entry, sub_id):
        return None

    checkpoint = _rebuild_checkpoint(entry)
    common = _entry_to_common_kwargs(entry)
    common["prompt"] = (
        f"{common['prompt']}\n\n"
        f"[System note: this one-shot task was interrupted by a server "
        f"restart and has been re-enqueued.  Any in-progress work may "
        f"have been lost — verify the current state before proceeding.  "
        f"If you saved a checkpoint (via set_checkpoint) it has been "
        f"preserved and is available on resume.]"
    )
    dedup_key = _entry_opt_str(entry, "dedup_key")
    retry_count = _entry_retry_count(entry)
    spawn_subsession(
        env=env,
        kind=SubsessionKind.TASK,
        owner_session_id=owner,
        **common,
        sub_id=sub_id,
        checkpoint=checkpoint,
        dedup_key=dedup_key,
        retry_count=retry_count,
        inbox=_rebuild_inbox(entry),
    )
    return _ResumeFate(
        owner_session_id=owner,
        sub_id=sub_id,
        kind="task",
        title=title,
        fate="resumed",
        detail="Re-enqueued — the task will restart from its original prompt.",
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
    title = _entry_str(entry, "title")

    if kind is SubsessionKind.PERIODIC:
        if status not in {s.value for s in ACTIVE_STATUSES}:
            # CLOSED periodic subsessions: only re-spawn those that
            # were auto-closed (not explicitly closed by the user or
            # the agent).  The worker's _check_resume_status will
            # verify the ticket state on its first post-restart tick
            # and close immediately if conditions have not improved.
            if (
                status == "closed"
                and _entry_str(entry, "close_reason") in _AUTO_CLOSE_REASONS
            ):
                return _resume_periodic_entry(
                    env, entry, sub_id, owner, title, original_status=status
                )
            _restore_entry(env.registry, entry)
            return None
        return _resume_periodic_entry(
            env, entry, sub_id, owner, title, original_status=status
        )

    if kind is SubsessionKind.WAIT_FOR_EVENT:
        if status not in {s.value for s in ACTIVE_STATUSES}:
            # CLOSED wait_for_event monitors: only re-spawn those that
            # were auto-closed (not explicitly closed by the user or
            # the agent).  The worker's _check_resume_status will
            # verify the ticket state on its first post-restart tick
            # and close immediately if conditions have not improved.
            if (
                status == "closed"
                and _entry_str(entry, "close_reason") in _AUTO_CLOSE_REASONS
            ):
                return _resume_wait_for_event_entry(
                    env, entry, sub_id, owner, title, original_status=status
                )
            _restore_entry(env.registry, entry)
            return None
        return _resume_wait_for_event_entry(
            env, entry, sub_id, owner, title, original_status=status
        )

    # Non-periodic kinds: terminal entries are restored without a worker.
    if status not in {s.value for s in ACTIVE_STATUSES}:
        _restore_entry(env.registry, entry)
        return None

    if kind is SubsessionKind.USER_CHAT:
        return _resume_user_chat_entry(env, entry, sub_id, owner, title)

    # task (and any future one-shot kinds): re-spawned from original prompt.
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

    Duplicate fates (same kind, title, and detail) are collapsed into a
    single line with a count to avoid repetitive system notices.
    """
    lines = [
        "[System notice: the chat service was restarted. "
        + "The following background tasks were affected:]",
        "",
    ]
    # Group fates by (kind, title, fate, detail) to collapse duplicates.
    groups: dict[tuple[str, str, str, str], list[_ResumeFate]] = {}
    for fate in fates:
        key = (fate["kind"], fate["title"], fate["fate"], fate["detail"])
        groups.setdefault(key, []).append(fate)

    for (kind_label, title, fate_verb, detail), group in groups.items():
        short_ids = [f["sub_id"][:8] for f in group]
        display_title = title or "(untitled)"
        verb = "resumed" if fate_verb == "resumed" else "interrupted"

        if len(group) == 1:
            id_str = short_ids[0]
        else:
            id_str = f"{len(group)} instances: {', '.join(short_ids)}"

        lines.append(
            f'- {kind_label.capitalize()} "{display_title}" ({id_str}): '
            f"{verb} — {detail}"
        )
    notice = "\n".join(lines)

    # Suppress duplicate restart notices: if an identical notice already
    # appears anywhere in this conversation the background-task state is
    # unchanged — skip injection to avoid flooding the transcript with
    # noise across repeated restarts (the previous turn-only check missed
    # duplicates separated by intervening user/assistant messages).
    history = env.conversation_store.history(owner_id)
    if any(turn[0] == notice for turn in history):
        logger.debug(
            "Skipping duplicate restart notice for owner %s — "
            "identical notice already present in conversation.",
            owner_id,
        )
        return

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
            checkpoint=_rebuild_checkpoint(entry),
            dedup_key=_entry_opt_str(entry, "dedup_key"),
            consecutive_no_change=_entry_int(entry, "consecutive_no_change"),
            retry_count=_entry_retry_count(entry),
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
            # Periodic monitors resume silently — they are unattended
            # and report results via their normal delivery channels.
            # Including them in the restart notice would cause the
            # parent agent to take unnecessary action on every
            # restart, undermining the recovery guarantee.
            if fate is not None and fate["kind"] != "periodic":
                owner = fate["owner_session_id"]
                fate_by_owner.setdefault(owner, []).append(fate)
        except Exception:
            logger.exception("Could not resume subsession entry %r", entry)

    for owner_id, fates in fate_by_owner.items():
        try:
            _inject_restart_notice(env, owner_id, fates)
        except Exception:
            logger.exception("Failed to inject restart notice for owner %s", owner_id)

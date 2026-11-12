"""Terminal / pause / resume transitions (:class:`RegistryLifecycle`).

Split out of :mod:`robotsix_chat.subsessions.registry`; the parent
:class:`~.registry.SubsessionRegistry` keeps thin delegation stubs so
existing callers keep working.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from typing import Protocol

from robotsix_chat.chat.events import (
    subsession_closed_frame,
    subsession_failed_frame,
    subsession_updated_frame,
)

from .models import InboxMessage, SubsessionInfo, SubsessionKind, SubsessionStatus
from .registry_checkpoint import _checkpoint_ticket_id, _truncate
from .registry_constants import _TICKET_TERMINAL_CLOSE_REASONS
from .registry_store import RegistryStore

logger = logging.getLogger(__name__)


class _RegistryCallbacks(Protocol):
    """The parent-registry surface :class:`RegistryLifecycle` calls back into.

    Structural type for :class:`~.registry.SubsessionRegistry`, declared
    here so this module never imports ``registry`` (avoids an import cycle).
    """

    def now(self) -> float:
        """Return the registry's wall-clock reading."""

    def _publish(self, owner_session_id: str, frame: dict[str, object]) -> None:
        """Publish *frame* to the owner session's event stream."""

    def last_assistant_text(self, info: SubsessionInfo) -> str:
        """Return the most recent assistant transcript text, or ``""``."""


class RegistryLifecycle:
    """Terminal, pause and resume transitions for subsessions.

    Receives shared dict references from the parent
    :class:`SubsessionRegistry` and calls back into it for the clock and
    SSE publishing.
    """

    def __init__(
        self,
        *,
        subs: dict[str, SubsessionInfo],
        running: dict[str, asyncio.Task[None]],
        inboxes: dict[str, deque[InboxMessage]],
        in_flight: dict[str, list[InboxMessage]],
        active_dedup_keys: dict[str, str],
        close_callbacks: list[Callable[[SubsessionInfo], None]],
        store: RegistryStore,
        registry: _RegistryCallbacks,
    ) -> None:
        """All mappings/lists are shared refs; *registry* is the parent."""
        self._subs = subs
        self._running = running
        self._inboxes = inboxes
        self._in_flight = in_flight
        self._active_dedup_keys = active_dedup_keys
        self._close_callbacks = close_callbacks
        self._store = store
        self._registry = registry

    def _close_and_publish(
        self,
        info: SubsessionInfo,
        *,
        status: SubsessionStatus,
        summary: str,
        reason: str = "",
        closed_by: str = "agent",
        error: str | None = None,
    ) -> SubsessionInfo:
        """Set terminal state, publish frame, persist, and return *info*."""
        info.status = status
        info.summary = summary
        info.last_activity_at = self._registry.now()

        # Clean up the dedup key so a new side-chat for the same issue can
        # be spawned after this one closes.
        if info.dedup_key is not None:
            self._active_dedup_keys.pop(info.dedup_key, None)

        if status is SubsessionStatus.FAILED:
            info.error = error
            frame = subsession_failed_frame(
                info.id,
                kind=info.kind.value,
                title=info.title,
                error=error,  # type: ignore[arg-type]  # non-None when FAILED
                summary=summary,
                parent_id=info.parent_id,
            )
        else:
            info.close_reason = reason
            frame = subsession_closed_frame(
                info.id,
                kind=info.kind.value,
                title=info.title,
                reason=reason,
                summary=summary,
                closed_by=closed_by,
                parent_id=info.parent_id,
            )

        # Drop any queued / in-flight inbox messages: a terminal
        # subsession can no longer deliver them, and leaving them in the
        # store would resurrect stale messages if the record is reopened
        # or a future restart re-enqueues them.
        #
        # NOTE: the wake event is intentionally left in place — removing
        # it breaks ``reopen`` (which does not recreate it), causing
        # ``wait_for_inbox`` to spin in a tight loop instead of blocking.
        self._inboxes.pop(info.id, None)
        self._in_flight.pop(info.id, None)
        self._registry._publish(info.owner_session_id, frame)
        self._store.persist()
        for callback in self._close_callbacks:
            callback(info)
        self._pause_dependents(info)
        return info

    def mark_closed(
        self, sub_id: str, *, summary: str, reason: str, closed_by: str = "agent"
    ) -> SubsessionInfo | None:
        """Set terminal ``CLOSED`` state and publish ``subsession_closed``.

        The worker's own clean-close path — does NOT cancel the task.
        No-op (returns ``None``) when the subsession is unknown or already
        terminal, so an external close racing the worker wins exactly once.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return None
        return self._close_and_publish(
            info,
            status=SubsessionStatus.CLOSED,
            summary=summary,
            reason=reason,
            closed_by=closed_by,
        )

    def mark_paused(
        self, sub_id: str, *, summary: str, reason: str = "paused"
    ) -> SubsessionInfo | None:
        """Transition a live periodic subsession to ``PAUSED``.

        Sets ``PAUSED`` status and publishes a ``subsession_updated``
        frame — the worker stays alive and blocks on a resume signal
        instead of terminating.  No-op (returns ``None``) when the
        subsession is unknown or already terminal.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return None
        if info.status in (SubsessionStatus.CLOSED, SubsessionStatus.FAILED):
            return None
        info.status = SubsessionStatus.PAUSED
        info.summary = summary
        info.close_reason = reason
        info.last_activity_at = self._registry.now()
        self._registry._publish(
            info.owner_session_id,
            subsession_updated_frame(
                sub_id,
                status="paused",
                runs=info.runs,
                last_activity_at=info.last_activity_at,
                last_result=info.last_result,
            ),
        )
        self._store.persist()
        return info

    def resume(self, sub_id: str) -> SubsessionInfo | None:
        """Resume a paused periodic subsession — PAUSED → RUNNING.

        Clears *close_reason* and *summary*, publishes an update frame,
        and persists.  Returns ``None`` when the subsession is unknown or
        not currently ``PAUSED``.
        """
        info = self._subs.get(sub_id)
        if info is None:
            return None
        if info.status is not SubsessionStatus.PAUSED:
            return None
        info.status = SubsessionStatus.RUNNING
        info.last_activity_at = self._registry.now()
        info.close_reason = None
        info.summary = None
        # Reset the human_approval_since timestamp so the resumed
        # monitor does not immediately time out again.
        if info.checkpoint is not None:
            info.checkpoint.pop("human_approval_since", None)
        self._registry._publish(
            info.owner_session_id,
            subsession_updated_frame(
                sub_id,
                status="running",
                runs=info.runs,
                last_activity_at=info.last_activity_at,
                last_result=info.last_result,
            ),
        )
        self._store.persist()
        return info

    def reopen(self, sub_id: str) -> SubsessionInfo | None:
        """Reopen a terminal monitor subsession.

        Accepts records whose status is ``CLOSED`` or ``PAUSED``, kind is
        ``PERIODIC`` or ``WAIT_FOR_EVENT``, and ``close_reason`` is
        ``"paused"``, ``"human_approval_timeout"``,
        ``"pre_authorized_approval"``, ``"max_runs"``, or
        ``"waiting_for_prerequisite"``.
        Other records are left untouched.  Returns the updated record or
        ``None`` when the subsession is unknown, not in a reopenable
        state, or already active (excluding PAUSED).
        """
        info = self._subs.get(sub_id)
        if info is None:
            return None
        # PAUSED subsessions are "active" but their worker may be dead
        # after a restart — allow reopen for them.
        if info.is_active and info.status is not SubsessionStatus.PAUSED:
            return None
        if (
            info.status not in (SubsessionStatus.CLOSED, SubsessionStatus.PAUSED)
            or info.kind not in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT)
            or info.close_reason
            not in (
                "paused",
                "human_approval_timeout",
                "pre_authorized_approval",
                "max_runs",
                "waiting_for_prerequisite",
            )
        ):
            return None
        info.status = SubsessionStatus.RUNNING
        info.last_activity_at = self._registry.now()
        # When reopening a max_runs-exhausted monitor, reset the run
        # counter so the monitor gets a fresh budget rather than
        # immediately hitting the limit again.  Capture the reason
        # before we clear it below.
        _was_max_runs = info.close_reason == "max_runs"
        _has_escalation_count = (
            info.checkpoint is not None
            and info.checkpoint.get("max_runs_exhausted_count") is not None
        )
        info.close_reason = None
        info.summary = None
        if _was_max_runs or _has_escalation_count:
            info.runs = 0
        # Reset the human_approval_since timestamp so the reopened
        # monitor does not immediately time out again.
        if info.checkpoint is not None:
            info.checkpoint.pop("human_approval_since", None)
        self._registry._publish(
            info.owner_session_id,
            subsession_updated_frame(
                sub_id,
                status="running",
                runs=info.runs,
                last_activity_at=info.last_activity_at,
                last_result=info.last_result,
            ),
        )
        self._store.persist()
        return info

    def find_paused_periodic(self) -> list[SubsessionInfo]:
        """Return every paused monitor subsession waiting for a state change.

        Includes monitors of kind ``PERIODIC`` or ``WAIT_FOR_EVENT`` in
        ``PAUSED`` status (auto-paused by ``max_idle_runs`` — worker is
        alive, waiting on an inbox signal), and monitors closed with
        reason ``"paused"``, ``"human_approval_timeout"``,
        ``"pre_authorized_approval"``, ``"max_runs"``, or
        ``"waiting_for_prerequisite"`` (legacy records from before the
        ``PAUSED`` status existed, plus dependency-paused monitors).
        All are waiting for a ticket-state change — typically a PR merge
        or an operator action — before they can safely resume.
        """
        result: list[SubsessionInfo] = []
        for info in self._subs.values():
            if info.kind not in (
                SubsessionKind.PERIODIC,
                SubsessionKind.WAIT_FOR_EVENT,
            ):
                continue
            if info.status is SubsessionStatus.PAUSED or (
                info.status is SubsessionStatus.CLOSED
                and info.close_reason
                in (
                    "paused",
                    "human_approval_timeout",
                    "pre_authorized_approval",
                    "max_runs",
                    "waiting_for_prerequisite",
                )
            ):
                result.append(info)
        return result

    def find_paused_periodic_by_ticket_id(self, ticket_id: str) -> list[SubsessionInfo]:
        """Return live ``PAUSED`` monitor subsessions tracking *ticket_id*.

        Matches ``PERIODIC`` and ``WAIT_FOR_EVENT`` monitors.  Only live
        workers are returned (``PAUSED`` status — the worker is alive and
        blocking on an inbox signal).  Legacy closed records with a
        paused-style close reason are handled by the background watcher's
        reopen path, not by inbox wake.
        """
        result: list[SubsessionInfo] = []
        for info in self._subs.values():
            if info.kind not in (
                SubsessionKind.PERIODIC,
                SubsessionKind.WAIT_FOR_EVENT,
            ):
                continue
            if info.status is not SubsessionStatus.PAUSED:
                continue
            cp = info.checkpoint
            if cp is None:
                continue
            cp_ticket_id = cp.get("ticket_id")
            if isinstance(cp_ticket_id, str) and cp_ticket_id == ticket_id:
                result.append(info)
        return result

    def cancel_and_close(
        self,
        sub_id: str,
        *,
        reason: str,
        closed_by: str = "system",
        summary: str | None = None,
    ) -> SubsessionInfo | None:
        """Externally close a live subsession: cancel its worker, mark CLOSED.

        Builds a best-effort summary from the last assistant transcript
        entry.  Returns the closed record (so the caller can deliver the
        summary to the parent) or ``None`` when unknown / already terminal.
        Idempotent: a second call returns ``None``.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return None
        # Cancel FIRST so the worker cannot race us into mark_closed /
        # fail while we build the terminal state.
        task = self._running.get(sub_id)
        if task is not None and not task.done():
            task.cancel()
        last = self._registry.last_assistant_text(info)
        if summary is None:
            summary = f"{reason.capitalize()}."
            if last:
                summary += f" Last state: {_truncate(last, 500)}"
        return self._close_and_publish(
            info,
            status=SubsessionStatus.CLOSED,
            summary=summary,
            reason=reason,
            closed_by=closed_by,
        )

    def fail(self, sub_id: str, *, error: str) -> SubsessionInfo | None:
        """Set terminal ``FAILED`` state and publish ``subsession_failed``.

        Returns the failed record, or ``None`` when unknown / already
        terminal (e.g. an external close landed first).
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return None
        last = self._registry.last_assistant_text(info)
        summary = f"Failed: {_truncate(error, 300)}"
        if last:
            summary += f" Last state: {_truncate(last, 500)}"
        return self._close_and_publish(
            info,
            status=SubsessionStatus.FAILED,
            summary=summary,
            error=error,
        )

    def mark_interrupted(self, sub_id: str, *, summary: str) -> SubsessionInfo | None:
        """Set terminal ``INTERRUPTED`` state (startup resume path).

        Published as a ``subsession_closed`` frame with ``closed_by=
        "system"`` — the UI treats it like any other terminal close.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return None
        return self._close_and_publish(
            info,
            status=SubsessionStatus.INTERRUPTED,
            summary=summary,
            reason="interrupted",
            closed_by="system",
        )

    def claim_run(self, sub_id: str, run_n: int) -> bool:
        """Atomically claim a periodic run number.

        Returns ``True`` when *run_n* was claimed (not previously
        executed); ``False`` when it was already completed — the caller
        must skip the agent turn.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return False
        if run_n in info.completed_runs:
            return False
        info.completed_runs.add(run_n)
        self._store.persist()
        return True

    def update_periodic_config(
        self,
        sub_id: str,
        *,
        prompt: str | None = None,
        interval_seconds: float | None = None,
        max_runs: int | None = None,
    ) -> bool:
        """Update the run configuration of an active periodic subsession.

        Only *prompt* (instructions), *interval_seconds*, and *max_runs*
        are accepted — the run counter is never reset, so self-update
        cannot bypass max-run limits.  Fields left at ``None`` are not
        touched.

        Returns ``True`` when the update was applied; ``False`` when the
        subsession is unknown or not an active periodic.
        """
        info = self._subs.get(sub_id)
        if info is None or not info.is_active:
            return False
        if info.kind is not SubsessionKind.PERIODIC:
            return False
        if prompt is not None:
            info.prompt = prompt
        if interval_seconds is not None:
            info.interval_seconds = interval_seconds
        if max_runs is not None:
            info.max_runs = max_runs
        self._store.persist()
        return True

    def _pause_dependents(self, closed: SubsessionInfo) -> None:
        """Pause monitors that depend on the ticket the *closed* monitor was watching.

        When a prerequisite ticket's monitor closes without the ticket
        reaching a terminal (resolved) state, every active monitor whose
        ``depends_on_ticket_id`` matches is cancelled and closed with
        reason ``waiting_for_prerequisite``.  The watcher polls the
        prerequisite ticket and reopens the dependent when the
        prerequisite reaches a terminal state.
        """
        ticket_id = _checkpoint_ticket_id(closed.checkpoint)
        if not ticket_id:
            return
        if closed.close_reason in _TICKET_TERMINAL_CLOSE_REASONS:
            return
        for info in list(self._subs.values()):
            if (
                info.id != closed.id
                and info.depends_on_ticket_id == ticket_id
                and info.is_active
                and info.status is not SubsessionStatus.PAUSED
            ):
                summary = (
                    f"Paused: prerequisite ticket {ticket_id} monitor "
                    f"closed ({closed.close_reason or 'unknown reason'}) "
                    f"before the prerequisite was resolved."
                )
                logger.info(
                    "Dependency pause: closing monitor %s (depends_on "
                    "ticket %s) because prerequisite monitor %s closed "
                    "with reason %r.",
                    info.id,
                    ticket_id,
                    closed.id,
                    closed.close_reason,
                )
                self.cancel_and_close(
                    info.id,
                    reason="waiting_for_prerequisite",
                    summary=summary,
                    closed_by="system",
                )

"""Periodic session scheduler — fire a preset, get an ordinary session.

The scheduler is deliberately small. On each tick it checks every enabled
preset; when one is due it

1. creates a NEW plain session under the ``periodic`` owner (title
   ``"<preset> — <UTC date>"``),
2. posts the preset's initial prompt (behind the shared preamble) through the
   SAME submit path an operator message takes, and
3. records the firing in its own state file.

That is all. There is no per-session execution state, no self-scheduled
continuation, no restart-resume: a chat restart mid-turn fails that turn the
way it would fail an operator's, and the next firing starts fresh. A human
typing into a periodic session later is just… using a session.

If a preset comes due while its previous session's turn is still being
processed, the firing is skipped with a log line (no queueing).

A run's session is closed once its turn COMPLETES (its report is delivered
and no live subsession is still working) through the injected
``close_completed`` callback — the same path as ``POST /sessions/{id}/close``
(subsessions cleaned up, feedback run, memory finalised) — so periodic runs
do not pile up as open sessions for the whole interval. A firing also
SUPERSEDES the preset's previous run: once the new session exists the
previous run's session is closed through the injected ``close_previous``
callback, a safety net for runs that crash without completing. Before that
close, the previous run's LIVE ``user_chat`` decision panels (questions the
operator has not answered yet) are handed over to the new session through
the injected ``carry_over_subsessions`` callback, so a pending decision
survives from firing to firing instead of being killed and re-asked.

Closed runs do not linger either: the scheduler remembers the last
``RETAINED_RUNS_PER_PRESET`` session ids of each preset and, after every
firing (and once at startup), hands every other closed periodic session to
the injected ``prune_closed_runs`` callback, which deletes it from the
conversation store. The sidebar therefore shows at most a handful of recent
runs per preset instead of every run since the install.

A run's session also gets a failsafe check to ensure a PARTIAL REPORT is
present in the transcript, even if the turn ended cleanly (without raising)
but the agent never emitted one — token exhaustion or a provider cutoff can
end a turn without an error. This ensures the next firing always has context
about what was attempted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robotsix_chat.config.periodic_models import PeriodicSessionDefinition
from robotsix_chat.subsessions.registry import OWNER_CLOSED_REASON

from .prompts import build_initial_message

logger = logging.getLogger(__name__)

#: The single owner id every periodic session lives under.
PERIODIC_OWNER = "periodic"

#: Where the scheduler persists per-preset firing state.
PERIODIC_SCHEDULER_PERSIST_PATH = "/data/periodic_scheduler_state.json"

#: How often the scheduler loop checks for due presets.
_TICK_SECONDS = 30.0

#: How many runs (open or closed) of each preset survive pruning. The
#: current run plus the previous ones — enough to read back what the last
#: couple of firings did, while the memory component keeps the long tail.
RETAINED_RUNS_PER_PRESET = 3

#: SubmitTurn posts *message* into *session_id* through the normal turn path
#: and returns when the turn has fully completed (or failed). The
#: ``model_level`` is the preset's override, ``None`` for the global default.
SubmitTurn = Callable[[str, str, int | None], Awaitable[None]]

#: IsBusy reports whether a session currently has a turn in flight.
IsBusy = Callable[[str], bool]

#: ClosePrevious closes the superseded previous run's session (best-effort;
#: exceptions are logged and never block the new firing).
ClosePrevious = Callable[[str], Awaitable[Any]]

#: CarryOverSubsessions moves the previous run's live ``user_chat`` panels
#: to the new run's session. Awaited with ``(previous_session_id,
#: new_session_id)`` BEFORE the previous session is closed (best-effort;
#: exceptions are logged and never block the firing).
CarryOverSubsessions = Callable[[str, str], Awaitable[Any]]

#: PruneClosedRuns deletes every CLOSED periodic session whose id is not in
#: the given keep-set. Awaited with the set of session ids to retain and
#: expected to return the number deleted (best-effort; exceptions are
#: logged).
PruneClosedRuns = Callable[[set[str]], Awaitable[Any]]

#: HasLiveSubsessions reports whether a session still has a working
#: subsession (e.g. ``wait_for_event`` / ``user_chat``) that must not be
#: closed while live.
HasLiveSubsessions = Callable[[str], bool]

#: ReportPartialResult emits a partial-failure report into a run's transcript
#: when its turn raises before completing. It is awaited with the run's
#: session id and a formatted error message (best-effort; exceptions are
#: logged and never propagate out of the turn task).
ReportPartialResult = Callable[[str, str], Awaitable[Any]]

#: EnsurePartialReportOnInterruption inspects a completed periodic run's
#: transcript and appends a PARTIAL REPORT if the turn ended abruptly without one.
#: This ensures the next firing inherits context about what was attempted, even if
#: token exhaustion or API errors interrupt the agent before it can emit its report.
EnsurePartialReportOnInterruption = Callable[[str], Awaitable[Any]]


def _format_partial_error_report(preset_name: str, exc: Exception) -> str:
    """Format an exception as a proper PARTIAL REPORT for error reporting.

    Follows the periodic preamble's PARTIAL REPORT structure: a section header
    with summary counts first, then detailed sections. This ensures the next
    run can inspect what was attempted and where it failed, rather than seeing
    a bare error line the drain prompt does not recognise as a report.
    """
    exc_type = type(exc).__name__
    exc_str = str(exc)
    tb_str = traceback.format_exc()

    return (
        "PARTIAL REPORT\n\n"
        f"Preset: {preset_name}\n"
        "Status: Turn failed before completing\n\n"
        "Done: 0 items (turn crashed before any work completed)\n"
        "Escalations: 0\n"
        "Held for next run: this entire run\n\n"
        "Reason for failure:\n"
        f"{exc_type}: {exc_str}\n\n"
        "Full diagnostic traceback:\n"
        f"{tb_str}"
    )


def format_failsafe_partial_report() -> str:
    """Format a minimal PARTIAL REPORT for a turn that ended without one.

    Used by the completed-run failsafe: the turn finished WITHOUT raising, but
    the agent never emitted its report — most likely token exhaustion or a
    provider cutoff ended the turn cleanly. Follows the periodic preamble's
    PARTIAL REPORT structure (section header with summary counts, then detail)
    so the next firing's drain prompt recognises it as a report and treats the
    prior work as unverified.
    """
    return (
        "PARTIAL REPORT\n\n"
        "Status: Turn ended without the agent emitting a report\n\n"
        "Done: unknown (no report was emitted)\n"
        "Escalations: 0\n"
        "Held for next run: all work from this run\n\n"
        "Reason for failure:\n"
        "The turn completed without the agent emitting a report. This most "
        "likely means token exhaustion or a provider cutoff ended the turn "
        "before the agent could summarise its work.\n\n"
        "Guidance for the next firing:\n"
        "Treat any work attempted during this run as unverified — the agent "
        "produced no report confirming what, if anything, was completed."
    )


def prune_closed_periodic_sessions(
    conversation_store: Any, keep: set[str], *, registry: Any = None
) -> int:
    """Delete every CLOSED ``periodic``-owned session not in *keep*.

    Open runs are never touched (a run that still has a live operator
    decision panel stays open, and so does a run whose turn failed — the
    supersede-close handles those). Sessions the operator also owns
    (``record`` registers a session under whoever sends a turn) drop out of
    the periodic list but keep their history under the operator. Any
    lingering subsession of a deleted run is closed through *registry*
    when one is given. Returns the number of sessions deleted.
    """
    sessions, _ = conversation_store.list_sessions(PERIODIC_OWNER, create_default=False)
    deleted = 0
    for meta in sessions:
        sid = meta.get("session_id")
        if not isinstance(sid, str) or sid in keep or not meta.get("closed"):
            continue
        if registry is not None:
            registry.close_all_for_owner(sid, reason=OWNER_CLOSED_REASON)
        result = conversation_store.delete_session(
            PERIODIC_OWNER, sid, create_replacement=False
        )
        if result.get("deleted"):
            deleted += 1
    return deleted


class PeriodicScheduler:
    """Create-and-seed scheduler for periodic session presets."""

    def __init__(
        self,
        *,
        definitions: list[PeriodicSessionDefinition],
        conversation_store: Any,
        submit_turn: SubmitTurn,
        is_busy: IsBusy,
        persist_path: str = PERIODIC_SCHEDULER_PERSIST_PATH,
        clock: Callable[[], float] = time.time,
        close_previous: ClosePrevious | None = None,
        close_completed: ClosePrevious | None = None,
        has_live_subsessions: HasLiveSubsessions | None = None,
        report_partial_result: ReportPartialResult | None = None,
        ensure_partial_report_on_interruption: EnsurePartialReportOnInterruption
        | None = None,
        carry_over_subsessions: CarryOverSubsessions | None = None,
        prune_closed_runs: PruneClosedRuns | None = None,
        retained_runs_per_preset: int = RETAINED_RUNS_PER_PRESET,
    ) -> None:
        """*conversation_store* needs ``create_session`` and ``set_title``.

        *close_previous*, when given, is awaited with the previous run's
        session id each time a preset fires again (``None`` keeps the old
        runs open — tests and callers without a session-close path).

        *close_completed*, when given, is awaited with a run's session id
        once that run's turn completes (its report is delivered), so the
        session is closed promptly instead of staying open until the next
        firing supersedes it. ``None`` keeps completed runs open.

        *has_live_subsessions*, when given, gates the completion close: a
        session whose turn completed but still has a working subsession
        (``wait_for_event`` / ``user_chat``) is left open until that
        subsession finishes. ``None`` treats every session as closable.

        *report_partial_result*, when given, is awaited with a run's session
        id and a formatted error message when that run's turn raises before
        completing, so a partial-failure report reaches the transcript and
        future runs can see what was attempted and where it failed. The run
        is NOT completion-closed after a failure (it is left for the
        supersede-close safety net). ``None`` keeps the previous behaviour of
        only logging the exception.

        *ensure_partial_report_on_interruption*, when given, is awaited with a
        run's session id after the turn completes WITHOUT raising, to detect
        and repair sessions that ended without a PARTIAL REPORT — a turn can
        end cleanly yet have the agent never emit its report (token exhaustion
        or a provider cutoff). The handler appends a minimal PARTIAL REPORT so
        the next firing has context. It is NOT invoked on the exception path
        (``report_partial_result`` already covers that). ``None`` skips this
        failsafe.

        *carry_over_subsessions*, when given, is awaited with the previous
        run's session id and the new run's session id each time a preset
        fires again, BEFORE the previous session is closed — so the previous
        run's live ``user_chat`` decision panels move to the new session
        instead of being killed by the supersede-close (and re-asked by the
        new run). ``None`` keeps the old behaviour.

        *prune_closed_runs*, when given, is awaited after every firing and
        once at startup with the set of session ids to KEEP — the last
        *retained_runs_per_preset* runs of every preset. It deletes every
        other closed periodic session. ``None`` keeps closed runs forever.
        """
        self._definitions = {d.name: d for d in definitions if d.enabled}
        self._store = conversation_store
        self._submit_turn = submit_turn
        self._is_busy = is_busy
        self._close_previous = close_previous
        self._close_completed = close_completed
        self._has_live_subsessions = has_live_subsessions
        self._report_partial_result = report_partial_result
        self._ensure_partial_report_on_interruption = (
            ensure_partial_report_on_interruption
        )
        self._carry_over_subsessions = carry_over_subsessions
        self._prune_closed_runs = prune_closed_runs
        self._retained_runs = max(1, int(retained_runs_per_preset))
        self._persist_path = Path(persist_path)
        self._clock = clock
        #: name -> {"last_fired_at": float, "last_session_id": str,
        #:          "runs": int, "recent_session_ids": [str, ...]}
        self._state: dict[str, dict[str, Any]] = self._load_state()
        self._task: asyncio.Task[None] | None = None
        #: In-flight turn tasks, keyed by preset, so is-busy also covers the
        #: window between submit and completion and tasks are not GC'd.
        self._turn_tasks: dict[str, asyncio.Task[None]] = {}

    # -- persistence --------------------------------------------------------

    def _load_state(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self._persist_path.read_text())
        except FileNotFoundError:
            return {}
        except OSError, ValueError:
            logger.warning(
                "Periodic scheduler state at %s unreadable — starting fresh",
                self._persist_path,
            )
            return {}
        if not isinstance(raw, dict):
            return {}
        # State written before run retention existed knows only the last
        # session id — seed the recent list from it so that run is kept.
        for entry in raw.values():
            if not isinstance(entry, dict):
                continue
            recent = entry.get("recent_session_ids")
            if not isinstance(recent, list):
                last = entry.get("last_session_id")
                entry["recent_session_ids"] = [last] if isinstance(last, str) else []
        return raw

    def _save_state(self) -> None:
        tmp = self._persist_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._state, indent=2) + "\n")
            tmp.replace(self._persist_path)  # atomic — never truncate in place
        except OSError:
            logger.exception("Failed to persist periodic scheduler state")

    # -- introspection (definitions endpoint) --------------------------------

    @property
    def definition_names(self) -> list[str]:
        """Names of the enabled presets."""
        return list(self._definitions)

    def get_definition(self, name: str) -> PeriodicSessionDefinition | None:
        """Return the enabled preset named *name*, or ``None``."""
        return self._definitions.get(name)

    def state_for(self, name: str) -> dict[str, Any]:
        """Return the firing state for *name* (empty when it never fired)."""
        return dict(self._state.get(name, {}))

    # -- scheduling ----------------------------------------------------------

    def _next_run(
        self, defn: PeriodicSessionDefinition, last_fired: float | None
    ) -> float:
        """Absolute clock time of *defn*'s next fire (``None`` = never fired).

        Unanchored presets keep the legacy cadence: a never-fired preset is
        immediately due, and every later run is spaced by
        ``schedule_interval_seconds`` from the previous firing.

        Anchored presets are pinned to a fixed UTC instant: they fire at the
        anchor and then every ``schedule_interval_seconds`` thereafter. The
        first run fires at the anchor (or, if the anchor has already passed,
        at the next occurrence on/after now) — an anchored preset never
        fires off its cadence.
        """
        interval = defn.schedule_interval_seconds
        if defn.anchor_utc is None:
            if last_fired is None:
                return 0.0
            return last_fired + interval
        anchor_ts = defn.anchor_utc.timestamp()
        if last_fired is None:
            now = self._clock()
            if now <= anchor_ts:
                return anchor_ts
            k = math.ceil((now - anchor_ts) / interval)
            return anchor_ts + k * interval
        k = math.floor((last_fired - anchor_ts) / interval) + 1
        return anchor_ts + k * interval

    def _due(self, defn: PeriodicSessionDefinition) -> bool:
        entry = self._state.get(defn.name)
        last = None if entry is None else float(entry.get("last_fired_at", 0.0))
        return self._clock() >= self._next_run(defn, last)

    def _previous_run_busy(self, name: str) -> bool:
        task = self._turn_tasks.get(name)
        if task is not None and not task.done():
            return True
        last_session = self._state.get(name, {}).get("last_session_id")
        return isinstance(last_session, str) and self._is_busy(last_session)

    async def fire(self, name: str, *, manual: bool = False) -> str | None:
        """Fire preset *name* now and return the new session id.

        Returns ``None`` when the firing is skipped because the previous
        run is still processing.
        """
        defn = self._definitions.get(name)
        if defn is None:
            raise KeyError(name)
        if self._previous_run_busy(name):
            logger.info(
                "Periodic preset %r is due but its previous session is still "
                "processing a turn — skipping this firing",
                name,
            )
            return None

        session = self._store.create_session(PERIODIC_OWNER)
        session_id = str(session["session_id"])
        now = datetime.now(UTC)
        self._store.set_title(
            session_id, f"{defn.name} — {now.strftime('%Y-%m-%d %H:%M')}"
        )

        message = build_initial_message(defn.initial_prompt, now=now)
        logger.info(
            "Periodic preset %r: injecting current date/time %s UTC into "
            "the initial message",
            name,
            now.strftime("%Y-%m-%d %H:%M"),
        )
        entry = self._state.setdefault(defn.name, {})
        previous_session = entry.get("last_session_id")
        entry["last_fired_at"] = self._clock()
        entry["last_session_id"] = session_id
        entry["runs"] = int(entry.get("runs", 0)) + 1
        recent = [
            sid
            for sid in entry.get("recent_session_ids", [])
            if isinstance(sid, str) and sid != session_id
        ]
        recent.append(session_id)
        entry["recent_session_ids"] = recent[-self._retained_runs :]
        self._save_state()

        superseded: str | None = (
            previous_session
            if isinstance(previous_session, str)
            and previous_session
            and previous_session != session_id
            else None
        )

        # Pending operator decisions follow the preset: move the previous
        # run's live user_chat panels to the new session BEFORE closing it,
        # or the close kills them and the new run re-asks the same thing.
        if self._carry_over_subsessions is not None and superseded is not None:
            try:
                moved = await self._carry_over_subsessions(superseded, session_id)
                if moved:
                    logger.info(
                        "Periodic preset %r: carried %s live user_chat panel(s) "
                        "over from superseded session %s to %s",
                        name,
                        moved,
                        superseded,
                        session_id,
                    )
            except Exception:
                logger.exception(
                    "Periodic preset %r: carrying user_chat panels over from "
                    "session %s failed — continuing with the new firing",
                    name,
                    superseded,
                )

        # The new run supersedes the previous one: close its session so
        # periodic runs never accumulate as open sessions.
        if self._close_previous is not None and superseded is not None:
            try:
                await self._close_previous(superseded)
                logger.info(
                    "Periodic preset %r: closed superseded previous session %s",
                    name,
                    superseded,
                )
            except Exception:
                logger.exception(
                    "Periodic preset %r: closing previous session %s failed — "
                    "continuing with the new firing",
                    name,
                    superseded,
                )

        logger.info(
            "Periodic preset %r fired%s — session %s",
            name,
            " (manual)" if manual else "",
            session_id,
        )

        # Closed runs beyond the retention window are deleted, not kept
        # around as closed sidebar entries.
        await self.prune()

        async def _run() -> None:
            try:
                await self._submit_turn(session_id, message, defn.model_level)
            except Exception as exc:
                logger.exception(
                    "Periodic preset %r: initial turn failed (session %s)",
                    name,
                    session_id,
                )
                # Emit a partial-failure report into the transcript so future
                # runs can see what was attempted and where it failed. The
                # session is deliberately NOT completion-closed here — a
                # failed run is left for the supersede-close safety net.
                if self._report_partial_result is not None:
                    error_message = _format_partial_error_report(name, exc)
                    try:
                        await self._report_partial_result(session_id, error_message)
                    except Exception:
                        logger.exception(
                            "Periodic preset %r: emitting partial-failure "
                            "report for session %s failed",
                            name,
                            session_id,
                        )
            else:
                # The run completed (its report was delivered). Ensure a PARTIAL
                # REPORT is present first — a turn can end without raising yet
                # have the agent never emit one (token exhaustion / provider
                # cutoff), so the failsafe appends a minimal report so the next
                # firing has context. Then close the session so periodic runs
                # don't accumulate as open sessions, unless a live subsession
                # (wait_for_event / user_chat) is still working — such sessions
                # stay open until the subsession finishes. The supersede-close
                # at fire time remains as a safety net for runs that crash
                # without completing.
                if self._ensure_partial_report_on_interruption is not None:
                    try:
                        await self._ensure_partial_report_on_interruption(session_id)
                    except Exception:
                        logger.exception(
                            "Periodic preset %r: failsafe PARTIAL REPORT handler "
                            "for session %s failed",
                            name,
                            session_id,
                        )
                if self._close_completed is not None and not (
                    self._has_live_subsessions is not None
                    and self._has_live_subsessions(session_id)
                ):
                    try:
                        await self._close_completed(session_id)
                    except Exception:
                        logger.exception(
                            "Periodic preset %r: closing completed session %s failed",
                            name,
                            session_id,
                        )

        task = asyncio.create_task(_run())
        self._turn_tasks[name] = task

        def _forget(finished: asyncio.Task[None], preset: str = name) -> None:
            if self._turn_tasks.get(preset) is finished:
                self._turn_tasks.pop(preset, None)

        task.add_done_callback(_forget)
        return session_id

    def retained_session_ids(self) -> set[str]:
        """Session ids of the runs every preset keeps (never pruned)."""
        keep: set[str] = set()
        for entry in self._state.values():
            for sid in entry.get("recent_session_ids", []):
                if isinstance(sid, str) and sid:
                    keep.add(sid)
            last = entry.get("last_session_id")
            if isinstance(last, str) and last:
                keep.add(last)
        return keep

    async def prune(self) -> int:
        """Delete closed periodic runs outside the retention window.

        Best-effort through the injected ``prune_closed_runs`` callback;
        returns the number of sessions it reported deleted (``0`` when no
        callback is wired or it failed).
        """
        if self._prune_closed_runs is None:
            return 0
        try:
            deleted = await self._prune_closed_runs(self.retained_session_ids())
        except Exception:
            logger.exception("Pruning closed periodic sessions failed")
            return 0
        count = int(deleted) if isinstance(deleted, int) else 0
        if count:
            logger.info("Pruned %d closed periodic session(s)", count)
        return count

    async def tick(self) -> None:
        """Fire every enabled preset that is due."""
        for name, defn in self._definitions.items():
            if self._due(defn):
                try:
                    await self.fire(name)
                except Exception:
                    logger.exception("Periodic preset %r failed to fire", name)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the background tick loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return

        async def _loop() -> None:
            # Runs closed before this process started are pruned once up
            # front — an install upgraded across the retention change may
            # carry months of closed runs.
            await self.prune()
            while True:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Periodic scheduler tick failed")
                await asyncio.sleep(_TICK_SECONDS)

        self._task = asyncio.create_task(_loop())
        logger.info(
            "Periodic scheduler started (%d enabled preset(s))",
            len(self._definitions),
        )

    async def close(self) -> None:
        """Stop the tick loop; in-flight turns finish on their own."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

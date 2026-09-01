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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robotsix_chat.config.periodic_models import PeriodicSessionDefinition

from .prompts import build_initial_message

logger = logging.getLogger(__name__)

#: The single owner id every periodic session lives under.
PERIODIC_OWNER = "periodic"

#: Where the scheduler persists per-preset firing state.
PERIODIC_SCHEDULER_PERSIST_PATH = "/data/periodic_scheduler_state.json"

#: How often the scheduler loop checks for due presets.
_TICK_SECONDS = 30.0

#: SubmitTurn posts *message* into *session_id* through the normal turn path
#: and returns when the turn has fully completed (or failed). The
#: ``model_level`` is the preset's override, ``None`` for the global default.
SubmitTurn = Callable[[str, str, int | None], Awaitable[None]]

#: IsBusy reports whether a session currently has a turn in flight.
IsBusy = Callable[[str], bool]


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
    ) -> None:
        """*conversation_store* needs ``create_session`` and ``set_title``."""
        self._definitions = {d.name: d for d in definitions if d.enabled}
        self._store = conversation_store
        self._submit_turn = submit_turn
        self._is_busy = is_busy
        self._persist_path = Path(persist_path)
        self._clock = clock
        #: name -> {"last_fired_at": float, "last_session_id": str, "runs": int}
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
        return raw if isinstance(raw, dict) else {}

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

    def _due(self, defn: PeriodicSessionDefinition) -> bool:
        entry = self._state.get(defn.name)
        if entry is None:
            # Never fired: due immediately, so a fresh preset does not wait
            # out a full (possibly day-long) interval before its first run.
            return True
        last = float(entry.get("last_fired_at", 0.0))
        return (self._clock() - last) >= defn.schedule_interval_seconds

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
        date = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        self._store.set_title(session_id, f"{defn.name} — {date}")

        message = build_initial_message(defn.initial_prompt)
        entry = self._state.setdefault(defn.name, {})
        entry["last_fired_at"] = self._clock()
        entry["last_session_id"] = session_id
        entry["runs"] = int(entry.get("runs", 0)) + 1
        self._save_state()

        logger.info(
            "Periodic preset %r fired%s — session %s",
            name,
            " (manual)" if manual else "",
            session_id,
        )

        async def _run() -> None:
            try:
                await self._submit_turn(session_id, message, defn.model_level)
            except Exception:
                logger.exception(
                    "Periodic preset %r: initial turn failed (session %s)",
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

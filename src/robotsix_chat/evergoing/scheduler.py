"""Periodic subject-aware trim scheduler for the evergoing session.

Runs on a configurable interval (default 1800 s / 30 min).  Each pass:

1. resolves the single evergoing session (skips if none exists);
2. calls :meth:`ConversationStore.has_new_input_since_trim` **first** and
   skips the whole pass — making **zero LLM calls** — when no new turns
   arrived since the last trim;
3. otherwise asks a cheap summary-tier agent whether the subject changed
   and how many finished leading turns to drop, then calls
   :meth:`ConversationStore.trim_session` with the decided index.

The in-flight turn is never trimmed (``keep_min_recent``).  Attached to
``app.state.evergoing_scheduler`` during startup — call ``start()`` to
begin the background loop and ``stop()`` to cancel it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.server.routes._shared import build_transcript
from robotsix_chat.chat.server.routes.chat import ChatAgent
from robotsix_chat.evergoing.decision import decide_trim

logger = logging.getLogger(__name__)


class EvergoingTrimScheduler:
    """Runs subject-aware trim passes on the evergoing session periodically.

    Attributes:
        interval_seconds: Seconds between scheduled trim passes.

    """

    def __init__(
        self,
        interval_seconds: float,
        store: ConversationStore,
        agent: ChatAgent,
        *,
        keep_min_recent: int = 2,
    ) -> None:
        """Create a scheduler bound to *store* and a cheap *agent*.

        Args:
            interval_seconds: Seconds between scheduled trim passes.
            store: The conversation store owning the evergoing session.
            agent: Cheap summary-tier agent used for the trim decision.
            keep_min_recent: Minimum recent turns never trimmed (in-flight
                safety).

        """
        self.interval_seconds = interval_seconds
        self._store = store
        self._agent = agent
        self._keep_min_recent = keep_min_recent
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the background loop (idempotent — no-op if already running)."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "evergoing trim scheduler started (interval=%ss)", self.interval_seconds
        )

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("evergoing trim scheduler stopped")

    # ------------------------------------------------------------------
    # run-once
    # ------------------------------------------------------------------

    async def run_once(self) -> dict[str, object]:
        """Execute one trim pass and return an audit dict describing it.

        Skips entirely (no LLM call) when there is no evergoing session or
        no new input since the last trim.
        """
        session_id = self._store.evergoing_session_id()
        if session_id is None:
            return {"trimmed": False, "reason": "no evergoing session"}

        # New-input gate — MUST run before any LLM call.
        if not self._store.has_new_input_since_trim(session_id):
            return {"trimmed": False, "reason": "no new input since last trim"}

        turns = self._store.history(session_id)
        visible_count = len(turns)
        max_drop = max(0, visible_count - self._keep_min_recent)
        if max_drop <= 0:
            # Too few turns to drop anything; advance the watermark (no LLM)
            # so the next no-input interval is correctly skipped.
            return self._store.trim_session(
                session_id,
                0,
                reason="too few turns to trim",
                decided_subject_change=None,
                keep_min_recent=self._keep_min_recent,
            )

        transcript = build_transcript(turns)
        decision = await decide_trim(
            self._agent,
            transcript,
            visible_count=visible_count,
            max_drop=max_drop,
        )

        session = self._store.get_session(session_id)
        current_trimmed = session.trimmed_turn_index if session is not None else 0
        new_trimmed_index = current_trimmed + decision.drop_leading

        return self._store.trim_session(
            session_id,
            new_trimmed_index,
            reason=decision.reason,
            decided_subject_change=decision.subject_changed,
            keep_min_recent=self._keep_min_recent,
        )

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Run trim passes forever, sleeping on the configured interval."""
        while True:
            try:
                await self.run_once()
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "evergoing trim scheduler loop iteration raised", exc_info=True
                )

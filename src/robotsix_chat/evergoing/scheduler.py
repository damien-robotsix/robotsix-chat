"""Periodic summarising compaction scheduler — the ONE way sessions shrink.

Runs on a configurable interval (default 1800 s / 30 min) over **every**
session.  The gate is fully deterministic — no LLM is consulted to decide
*whether* to compact (the previous subject-change decision needed an LLM
pass of its own and proved too aggressive).  Per session each pass:

1. calls :meth:`ConversationStore.has_new_input_since_trim` **first** and
   skips the session — making **zero LLM calls** — when no new turns
   arrived since the last compaction;
2. skips while at most ``keep_recent_runs`` fresh (not-yet-summarised)
   runs exist — a run is one completed (operator message, assistant final
   answer) pair — so short conversations never churn the summariser;
3. otherwise summarises everything **before** the last ``keep_recent_runs``
   runs into the session's compacted summary.  The recent runs are NOT
   shown to the summariser and stay in the replay verbatim.

Combined with the pass interval this yields the intended trigger: a
session is compacted at most once per interval (30 min by default) and
only when more than ``keep_recent_runs`` runs accumulated beyond the
previous summary.  Nothing is ever physically dropped — the UI transcript
is untouched and the agent replay keeps summary + recent runs.

Attached to ``app.state.evergoing_scheduler`` during startup — call
``start()`` to begin the background loop and ``stop()`` to cancel it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from robotsix_chat.chat.conversation import ConversationStore, Session
from robotsix_chat.chat.server.routes.chat import ChatAgent
from robotsix_chat.chat.summarize import generate_idle_summary

logger = logging.getLogger(__name__)


class EvergoingSummaryScheduler:
    """Runs summarising compaction passes over all sessions periodically.

    Attributes:
        interval_seconds: Seconds between scheduled compaction passes.

    """

    def __init__(
        self,
        interval_seconds: float,
        store: ConversationStore,
        agent: ChatAgent,
        *,
        keep_recent_runs: int = 5,
    ) -> None:
        """Create a scheduler bound to *store* and a cheap *agent*.

        Args:
            interval_seconds: Seconds between scheduled compaction passes.
            store: The conversation store owning the sessions.
            agent: Cheap summary-tier agent used to write the summary.
            keep_recent_runs: Number of most-recent completed runs kept
                verbatim in the replay and excluded from the summariser
                input.  A session is only compacted when MORE than this
                many fresh runs exist beyond the previous summary.

        """
        self.interval_seconds = interval_seconds
        self._store = store
        self._agent = agent
        self._keep_recent_runs = max(1, keep_recent_runs)
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
            "evergoing summary scheduler started (interval=%ss, keep_recent_runs=%d)",
            self.interval_seconds,
            self._keep_recent_runs,
        )

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("evergoing summary scheduler stopped")

    # ------------------------------------------------------------------
    # run-once
    # ------------------------------------------------------------------

    async def run_once(self) -> dict[str, object]:
        """Execute one compaction pass over every session; return an audit dict.

        Per session the pass makes zero LLM calls unless new input arrived
        since the last compaction AND more than ``keep_recent_runs`` fresh
        runs accumulated.  The below-gate skip does NOT advance the
        watermark, so short exchanges keep accumulating until the gate
        opens.
        """
        audits: list[dict[str, object]] = []
        for session_id in self._store.all_session_ids():
            audit = await self._run_once_session(session_id)
            if audit is not None:
                audit["session_id"] = session_id
                audits.append(audit)
        return {"sessions": audits}

    async def _run_once_session(self, session_id: str) -> dict[str, object] | None:
        """Compaction pass for one session; ``None`` when skipped with no work."""
        session = self._store.get_session(session_id)
        if session is None:
            return None

        # Self-heal first: repair a session whose compaction advanced its
        # index but never persisted a summary.  This runs BEFORE the
        # new-input gate because a corrupted session may have no new input
        # since the last pass yet still need its missing summary backfilled.
        repair = await self._maybe_repair_summary(session_id, session)

        # New-input gate — MUST run before the (further) compaction LLM call.
        if not self._store.has_new_input_since_trim(session_id):
            return repair

        # Carry the self-heal outcome into the compaction audit so a pass
        # that both repaired and compacted reports both.
        repaired = bool(repair and repair.get("repaired"))

        keep = self._keep_recent_runs
        start = max(session.compacted_turn_index, session.trimmed_turn_index)
        fresh_runs = len(session.turns) - start

        # Deterministic gate: only compact when MORE than `keep` completed
        # runs accumulated beyond the previous summary.  Deliberately does
        # NOT advance the watermark, so runs keep accumulating.
        if fresh_runs <= keep:
            return {
                "compacted": False,
                "reason": "at most keep_recent_runs fresh",
                "repaired": repaired,
            }

        # Snapshot the fold window BEFORE the (slow) summariser call so
        # turns recorded while it runs are never covered by a summary that
        # did not see them.  The agent-facing history starts at `start` and
        # is prefixed with the previous summary as a synthetic turn, so the
        # old summary is folded into the new one naturally.
        history = self._store.agent_history(session_id)
        actions = self._store.agent_history_actions(session_id)
        fold_turns = history[:-keep]
        fold_actions = actions[:-keep]
        cover_until = len(session.turns) - keep

        summary = await generate_idle_summary(
            self._run_summary, fold_turns, fold_actions
        )
        if not summary:
            logger.warning(
                "summary generation failed for session %s — will retry next pass",
                session_id,
            )
            return {
                "compacted": False,
                "reason": "summary generation failed",
                "repaired": repaired,
            }

        self._store.compact_session(
            "",
            session_id,
            summary,
            cover_until_index=cover_until,
        )
        folded = len(fold_turns)
        logger.info(
            "compacted session %s: %d turn(s) folded into summary, "
            "last %d run(s) kept verbatim",
            session_id,
            folded,
            keep,
        )
        return {
            "compacted": True,
            "turns_folded": folded,
            "kept_recent_runs": keep,
            "reason": "over keep_recent_runs fresh runs",
            "repaired": repaired,
        }

    async def _maybe_repair_summary(
        self, session_id: str, session: Session
    ) -> dict[str, object] | None:
        """Self-heal a session missing its compaction summary.

        Repairs the state where a previous compaction advanced
        ``compacted_turn_index`` past ``trimmed_turn_index`` but never
        persisted a ``compacted_summary`` (index moved, summary dropped).
        The summary is regenerated from the stored covered turns
        (``turns[:compacted_turn_index]``) and backfilled WITHOUT advancing
        any index — the covered window is unchanged and no turn is folded or
        dropped by the repair itself.

        Returns an audit dict when a repair was attempted (``repaired`` True
        on success, False when regeneration failed), or ``None`` when no
        repair was needed.  Idempotent: once a non-empty summary is present a
        subsequent pass regenerates nothing (guarded here and in
        :meth:`ConversationStore.backfill_compacted_summary`).
        """
        # Idempotency + scope guards mirror backfill_compacted_summary so no
        # LLM call is made when the session does not need a repair.
        if session.compacted_summary:
            return None
        if session.compacted_turn_index <= session.trimmed_turn_index:
            return None

        turns, actions = self._store.compacted_covered_turns(session_id)
        if not turns:
            return None

        summary = await generate_idle_summary(self._run_summary, turns, actions)
        if not summary:
            logger.warning(
                "self-heal summary regeneration failed for session %s "
                "(compacted_turn_index=%d, trimmed_turn_index=%d) — will "
                "retry next pass",
                session_id,
                session.compacted_turn_index,
                session.trimmed_turn_index,
            )
            return {
                "repaired": False,
                "reason": "self-heal summary generation failed",
            }

        healed = self._store.backfill_compacted_summary(session_id, summary)
        if not healed:
            # Lost a race (another writer filled it) — nothing to report.
            return None

        logger.info(
            "self-healed session %s: regenerated missing compaction summary "
            "over %d covered turn(s) (compacted_turn_index=%d, "
            "trimmed_turn_index=%d); indexes unchanged",
            session_id,
            len(turns),
            session.compacted_turn_index,
            session.trimmed_turn_index,
        )
        return {
            "repaired": True,
            "covered_turns": len(turns),
            "reason": "regenerated missing compaction summary",
        }

    async def _run_summary(self, prompt: str) -> str:
        """One summariser call: prompt → text (``""`` on failure)."""
        parts: list[str] = []
        try:
            async for token in self._agent.stream(
                prompt,
                history=None,
                session_id=None,
                client_id=None,
                trace_name="evergoing-compaction-summary",
            ):
                parts.append(token)
        except Exception:
            logger.exception("Compaction summary call failed")
            return ""
        return "".join(parts)

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Run compaction passes forever, sleeping on the configured interval."""
        while True:
            try:
                await self.run_once()
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "evergoing summary scheduler loop iteration raised", exc_info=True
                )

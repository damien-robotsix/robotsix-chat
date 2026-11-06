"""Feedback runner — analyses a session and files improvement tickets."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from robotsix_chat.feedback.allowed_repos import (
    _DEFAULT_DEPLOY_BASE_URL,
    _do_resolve_allowed_repos,
    _repo_cache,
    _resolve_allowed_repos,
)
from robotsix_chat.feedback.finding_state import FindingStateMixin
from robotsix_chat.feedback.prompts import (
    FEEDBACK_SYSTEM_PROMPT,
    _build_feedback_prompt,
    _build_investigation_panel_prompt,
)
from robotsix_chat.feedback.ticket_filing import TicketFilingMixin

try:
    from robotsix_llmio.core.tracing import get_recording_span, start_trace
except ImportError:  # pragma: no cover — tracing extra absent in minimal installs
    start_trace = None  # type: ignore[assignment]
    get_recording_span = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.config.models import FeedbackSettings
    from robotsix_chat.knowledge.store import KnowledgeStore
    from robotsix_chat.llm import LlmioChatAgent
    from robotsix_chat.subsessions import SubsessionRegistry

__all__ = [
    "FEEDBACK_SYSTEM_PROMPT",
    "_DEFAULT_DEPLOY_BASE_URL",
    "FeedbackRunner",
    "_build_feedback_prompt",
    "_build_investigation_panel_prompt",
    "_do_resolve_allowed_repos",
    "_repo_cache",
    "_resolve_allowed_repos",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feedback runner
# ---------------------------------------------------------------------------


class FeedbackRunner(TicketFilingMixin, FindingStateMixin):
    """Runs feedback analysis at compaction and session-end boundaries.

    The analysis is performed as a background task — it never blocks the
    triggering request. When the LLM surfaces actionable improvements,
    tickets are filed via ``POST /tickets/ingest`` on the configured board.
    """

    def __init__(
        self,
        settings: FeedbackSettings,
        feedback_agent: LlmioChatAgent,
        *,
        subsession_registry: SubsessionRegistry | None = None,
        subsession_spawner: Callable[..., str | None] | None = None,
        conversation_store: ConversationStore | None = None,
        knowledge_store: KnowledgeStore | None = None,
        deploy_base_url: str = "",
        deploy_api_key: str = "",
    ) -> None:
        """*feedback_agent* is a bare ``LlmioChatAgent`` (no tools, no memory).

        *subsession_spawner* routes an actionable finding to a chat
        subsession agent when the board rejects the ingest with its
        admission policy (mill #3093 permanently removed the
        investigation-ticket capability — ``POST /tickets/ingest`` now
        answers HTTP 400 for ``source_tag=robotsix-chat-feedback`` and
        instructs callers to run the investigation as a chat subsession
        agent instead).  It is called as
        ``spawner(owner_session_id=..., title=..., prompt=...)`` and
        returns the spawned subsession id (or ``None``).  Left unset, a
        policy-blocked finding is logged and dropped rather than filed.

        *deploy_base_url* and *deploy_api_key* are the canonical
        ``central_deploy.url`` and ``central_deploy.deploy_api_key`` — the
        address and credential this deployment already knows reach the deploy
        server. Left empty, the roster lookup falls back to
        :data:`_DEFAULT_DEPLOY_BASE_URL` with no auth header.
        """
        self._settings = settings
        self._agent = feedback_agent
        self._registry = subsession_registry
        self._spawner = subsession_spawner
        self._conversation_store = conversation_store
        self._knowledge_store = knowledge_store
        self._deploy_base_url = deploy_base_url
        self._deploy_api_key = deploy_api_key
        self._board_url = settings.board_url.rstrip("/") if settings.board_url else ""
        self._board_token = settings.board_api_token.get_secret_value()
        self._timeout = settings.timeout
        self._max_tickets_per_run = settings.max_tickets_per_run
        self._dedup_window = settings.dedup_window_seconds
        self._ingest_max_retries = settings.ingest_max_retries
        #: Bounds concurrent runs — a burst of session closes must not fan
        #: out into dozens of analysis agents at once (see
        #: ``FeedbackSettings.max_concurrent_runs``).
        self._max_concurrent_runs = settings.max_concurrent_runs
        self._run_slots = asyncio.Semaphore(self._max_concurrent_runs)

        # In-process dedup caches: (key → monotonic timestamp of last event).
        # Shared across ALL sessions — a single FeedbackRunner instance
        # services the whole server lifetime, so these naturally debounce
        # both intra- and inter-session duplicates.
        self._last_run_at: dict[str, float] = {}
        self._last_filed_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public entry points — schedule the run as a background task
    # ------------------------------------------------------------------

    def schedule(
        self,
        trigger_type: str,
        session_id: str,
        turns: list[tuple[str, str]],
    ) -> None:
        """Schedule a feedback run as a fire-and-forget background task.

        *trigger_type* is ``"compaction"`` or ``"session_end"``.
        Errors are logged; the task is never awaited by the caller.

        If a feedback run for *session_id* was already scheduled within
        ``dedup_window_seconds``, the new run is silently skipped to
        prevent duplicate ticket creation from near-simultaneous
        compactions or session-end triggers.
        """
        if not self._board_url:
            logger.warning(
                "Feedback run skipped — no board_url configured (session=%s)",
                session_id,
            )
            return

        now = time.monotonic()
        last = self._last_run_at.get(session_id)
        if last is not None and (now - last) < self._dedup_window:
            logger.debug(
                "Feedback run skipped — dedup (session=%s, last=%.1fs ago,"
                " window=%.1fs)",
                session_id,
                now - last,
                self._dedup_window,
            )
            return
        self._last_run_at[session_id] = now

        task = asyncio.create_task(
            self._run(trigger_type, session_id, turns),
            name=f"feedback-{trigger_type}-{session_id[:8]}",
        )
        # Keep a strong reference so the task isn't GC'd mid-flight.
        self._background_tasks: set[asyncio.Task[None]] = getattr(
            self, "_background_tasks", set()
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(
        self,
        trigger_type: str,
        session_id: str,
        turns: list[tuple[str, str]],
    ) -> None:
        """Run the cycle under the concurrency cap (background task).

        Runs beyond ``max_concurrent_runs`` wait for a slot instead of
        starting; nothing is dropped, only serialised.
        """
        if self._run_slots.locked():
            logger.info(
                "Feedback run queued: trigger=%s session=%s — %d run(s) already "
                "in flight (cap max_concurrent_runs=%d)",
                trigger_type,
                session_id,
                self._max_concurrent_runs,
                self._max_concurrent_runs,
            )
        async with self._run_slots:
            await self._execute(trigger_type, session_id, turns)

    async def _execute(
        self,
        trigger_type: str,
        session_id: str,
        turns: list[tuple[str, str]],
    ) -> None:
        """Execute the full feedback analysis cycle."""
        logger.info(
            "Feedback run starting: trigger=%s session=%s turns=%d",
            trigger_type,
            session_id,
            len(turns),
        )
        trace_name = f"feedback-{trigger_type}"
        trace_metadata: dict[str, str] = {
            "trigger_type": trigger_type,
            "session_id": session_id,
        }
        try:
            with self._trace_context(trace_name, session_id):
                self._stamp_tags(trigger_type)

                # 1. Collect subsession summaries.
                subsession_summaries = self._collect_subsession_summaries(session_id)

                # 2. Resolve allowed target repos dynamically.
                allowed_repos = await _resolve_allowed_repos(
                    self._deploy_api_key,
                    self._deploy_base_url,
                )

                # 3. Build prompt and call the feedback agent.
                prompt = _build_feedback_prompt(
                    trigger_type,
                    session_id,
                    turns,
                    subsession_summaries,
                    allowed_repos,
                )
                analysis = await self._call_agent(
                    prompt, session_id=session_id, trace_metadata=trace_metadata
                )
                if analysis is None:
                    return

                # 4. Parse the JSON response.
                tickets = self._parse_tickets(analysis, repo_ids=allowed_repos)
                if not tickets:
                    logger.info(
                        "Feedback run: no actionable tickets (session=%s)", session_id
                    )
                    return

                # 5. File each ticket.
                filed, failed, routed, dropped = await self._file_tickets(
                    tickets, trigger_type=trigger_type, session_id=session_id
                )
                logger.info(
                    "Feedback run complete: trigger=%s session=%s filed=%d/%d"
                    " failed=%d routed=%d dropped=%d",
                    trigger_type,
                    session_id,
                    filed,
                    len(tickets),
                    failed,
                    routed,
                    dropped,
                )

                # 6. Stamp outcome metadata on the trace root span.
                self._stamp_outcome(
                    filed, len(tickets), failed=failed, routed=routed, dropped=dropped
                )
        except Exception:
            logger.exception(
                "Feedback run failed: trigger=%s session=%s",
                trigger_type,
                session_id,
            )

    def _collect_subsession_summaries(self, session_id: str) -> list[dict[str, Any]]:
        """Collect summary info for every subsession owned by *session_id*."""
        if self._registry is None:
            return []
        result: list[dict[str, Any]] = []
        try:
            for info in self._registry.list_for_owner(session_id):
                result.append(
                    {
                        "id": info.id,
                        "kind": info.kind.value if info.kind else "unknown",
                        "status": info.status.value if info.status else "unknown",
                        "summary": info.summary,
                        "close_reason": info.close_reason,
                    }
                )
        except Exception:
            logger.exception(
                "Failed to collect subsession summaries for session=%s", session_id
            )
        return result

    async def _call_agent(
        self,
        prompt: str,
        *,
        session_id: str,
        trace_metadata: dict[str, str] | None = None,
    ) -> str | None:
        """Call the feedback agent with *prompt*; return the full reply text.

        *session_id* groups the agent run under the originating chat session
        in Langfuse.  *trace_metadata* is stamped as span attributes for
        observability.
        """
        reply_parts: list[str] = []
        try:
            async for token in self._agent.stream(
                prompt,
                history=None,
                session_id=session_id,
                client_id=None,
                trace_metadata=trace_metadata,
            ):
                reply_parts.append(token)
        except Exception:
            logger.exception("Feedback agent call failed")
            return None
        return "".join(reply_parts).strip()

    # ------------------------------------------------------------------
    # Trace helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _trace_context(
        trace_name: str,
        session_id: str,
    ) -> Any:  # contextlib.AbstractContextManager[Any]
        """Return a context manager that wraps the run in a named Langfuse trace.

        Returns a no-op ``nullcontext`` when the ``tracing`` extra is absent.
        When active the trace is named *trace_name* and grouped under
        *session_id*.  Tags must be stamped separately via :meth:`_stamp_tags`
        inside the context so they land on the active root span.
        """
        if start_trace is None:
            return contextlib.nullcontext()
        return start_trace(trace_name, session_id=session_id)

    @staticmethod
    def _stamp_tags(trigger_type: str) -> None:
        """Stamp Langfuse trace tags on the current recording span.

        No-op when OTel is absent.
        """
        if get_recording_span is None:
            return
        span = get_recording_span()
        if span is not None:
            span.set_attribute(
                "langfuse.trace.tags", json.dumps(["feedback", trigger_type])
            )

    @staticmethod
    def _stamp_outcome(
        filed: int, total: int, *, failed: int = 0, routed: int = 0, dropped: int = 0
    ) -> None:
        """Stamp feedback outcome metadata on the current recording span."""
        if get_recording_span is None:
            return
        span = get_recording_span()
        if span is not None:
            span.set_attribute("feedback.filed_tickets", filed)
            span.set_attribute("feedback.failed_tickets", failed)
            span.set_attribute("feedback.routed_tickets", routed)
            span.set_attribute("feedback.dropped_tickets", dropped)
            span.set_attribute("feedback.total_tickets", total)

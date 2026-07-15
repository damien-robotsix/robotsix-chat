"""Feedback runner — analyses a session and files improvement tickets."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import httpx

try:
    from robotsix_llmio.core.tracing import get_recording_span, start_trace
except ImportError:  # pragma: no cover — tracing extra absent in minimal installs
    start_trace = None  # type: ignore[assignment]
    get_recording_span = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from robotsix_chat.config.models import FeedbackSettings
    from robotsix_chat.llm import LlmioChatAgent
    from robotsix_chat.subsessions import SubsessionRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

FEEDBACK_SYSTEM_PROMPT = """\
You are a session analysis agent for an LLM-powered chat assistant. \
Your job is to review a conversation session and identify concrete, \
actionable improvements. Output ONLY a JSON object — no markdown \
fences, no preamble, no commentary outside the JSON.

The JSON must have exactly this structure:
{
  "analysis": "Brief prose analysis of the session (2-4 sentences).",
  "tickets": [
    {
      "title": "Short, specific title",
      "description": "Detailed description with context — include what happened, \
why it matters, and a concrete suggestion.",
      "kind": "prompt"
    }
  ]
}

``kind`` must be one of: ``prompt``, ``tool``, ``config``, ``code``.

Rules:
- Only include a ticket when there is a **concrete, actionable** \
improvement — something a developer could implement.
- Do NOT file tickets for one-off flukes, transient API errors, or \
user typos. Focus on patterns: repeated failures, missing capabilities, \
unclear guidance, slow paths, config gaps.
- For uneventful sessions where nothing went wrong and no capability \
gaps were exposed, return an empty ``tickets`` list.
- The ``description`` must be self-contained and actionable — someone \
reading it later should understand the problem and have a clear idea \
of what to change."""


def _build_feedback_prompt(
    trigger_type: str,
    session_id: str,
    turns: list[tuple[str, str]],
    subsession_summaries: list[dict[str, Any]],
) -> str:
    """Build the feedback analysis prompt from session data."""
    transcript_parts: list[str] = []
    for user_msg, asst_msg in turns:
        transcript_parts.append(f"User: {user_msg}")
        if asst_msg:
            truncated = asst_msg[:3000] + "\u2026" if len(asst_msg) > 3000 else asst_msg
            transcript_parts.append(f"Assistant: {truncated}")
    transcript = "\n".join(transcript_parts) if transcript_parts else "(empty)"

    subsession_text = ""
    if subsession_summaries:
        parts: list[str] = []
        for i, s in enumerate(subsession_summaries):
            kind = s.get("kind", "unknown")
            summary = s.get("summary", "") or "(no summary)"
            status = s.get("status", "unknown")
            parts.append(f"  [{i}] kind={kind} status={status}\n      {summary}")
        subsession_text = "Subsession summaries:\n" + "\n".join(parts)
    else:
        subsession_text = "Subsession summaries: (none)"

    return (
        f"Trigger: {trigger_type}\n"
        f"Session ID: {session_id}\n\n"
        f"Conversation transcript:\n{transcript}\n\n"
        f"{subsession_text}\n\n"
        "Output the JSON analysis now."
    )


# ---------------------------------------------------------------------------
# Feedback runner
# ---------------------------------------------------------------------------


class FeedbackRunner:
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
    ) -> None:
        """*feedback_agent* is a bare ``LlmioChatAgent`` (no tools, no memory)."""
        self._settings = settings
        self._agent = feedback_agent
        self._registry = subsession_registry
        self._board_url = settings.board_url.rstrip("/") if settings.board_url else ""
        self._board_token = settings.board_api_token.get_secret_value()
        self._timeout = settings.timeout

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
        """
        if not self._board_url:
            logger.debug(
                "Feedback run skipped — no board_url configured (session=%s)",
                session_id,
            )
            return

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
        """Execute the full feedback analysis cycle (background task)."""
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

                # 2. Build prompt and call the feedback agent.
                prompt = _build_feedback_prompt(
                    trigger_type, session_id, turns, subsession_summaries
                )
                analysis = await self._call_agent(
                    prompt, session_id=session_id, trace_metadata=trace_metadata
                )
                if analysis is None:
                    return

                # 3. Parse the JSON response.
                tickets = self._parse_tickets(analysis)
                if not tickets:
                    logger.info(
                        "Feedback run: no actionable tickets (session=%s)", session_id
                    )
                    return

                # 4. File each ticket.
                filed = await self._file_tickets(
                    tickets, trigger_type=trigger_type, session_id=session_id
                )
                logger.info(
                    "Feedback run complete: trigger=%s session=%s filed=%d/%d",
                    trigger_type,
                    session_id,
                    filed,
                    len(tickets),
                )

                # 5. Stamp outcome metadata on the trace root span.
                self._stamp_outcome(filed, len(tickets))
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
            import contextlib

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
    def _stamp_outcome(filed: int, total: int) -> None:
        """Stamp feedback outcome metadata on the current recording span."""
        if get_recording_span is None:
            return
        span = get_recording_span()
        if span is not None:
            span.set_attribute("feedback.filed_tickets", filed)
            span.set_attribute("feedback.total_tickets", total)

    @staticmethod
    def _parse_tickets(analysis_text: str) -> list[dict[str, Any]]:
        """Parse the agent's JSON output; return the ``tickets`` list.

        Returns an empty list on any parse failure (logged).
        """
        # Strip markdown fences if present.
        text = analysis_text.strip()
        if text.startswith("```"):
            # Remove opening fence line.
            newline = text.find("\n")
            if newline != -1:
                text = text[newline + 1 :]
            # Remove closing fence.
            if text.endswith("```"):
                text = text[:-3].strip()
            elif text.endswith("```\n"):
                text = text[:-4].strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(
                "Feedback agent returned non-JSON output: %.200s...", analysis_text
            )
            return []

        if not isinstance(parsed, dict):
            logger.warning("Feedback agent output is not a JSON object")
            return []

        tickets = parsed.get("tickets", [])
        if not isinstance(tickets, list):
            logger.warning("Feedback agent 'tickets' field is not a list")
            return []

        valid_kinds = frozenset({"prompt", "tool", "config", "code"})
        result: list[dict[str, Any]] = []
        for t in tickets:
            if not isinstance(t, dict):
                continue
            title = t.get("title", "")
            description = t.get("description", "")
            kind = t.get("kind", "")
            if not title or not description or kind not in valid_kinds:
                logger.debug("Skipping invalid ticket entry: %s", t)
                continue
            result.append({"title": title, "description": description, "kind": kind})
        return result

    async def _file_tickets(
        self,
        tickets: list[dict[str, Any]],
        *,
        trigger_type: str,
        session_id: str,
    ) -> int:
        """POST each ticket to ``/tickets/ingest``; return the count filed."""
        if not self._board_url:
            return 0

        ingest_url = f"{self._board_url}/tickets/ingest"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._board_token:
            headers["Authorization"] = f"Bearer {self._board_token}"

        filed = 0
        for ticket in tickets:
            payload: dict[str, Any] = {
                "title": ticket["title"],
                "description": ticket["description"],
                "kind": ticket["kind"],
                "source_tag": "robotsix-chat-feedback",
                "source_session_id": session_id,
                "trigger_type": trigger_type,
            }
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(ingest_url, headers=headers, json=payload)
                if 200 <= resp.status_code < 300:
                    filed += 1
                    logger.debug(
                        "Feedback ticket filed: %s (HTTP %d)",
                        ticket["title"],
                        resp.status_code,
                    )
                else:
                    logger.warning(
                        "Feedback ticket ingest returned %d for %r: %s",
                        resp.status_code,
                        ticket["title"],
                        resp.text[:200],
                    )
            except Exception:
                logger.exception("Failed to file feedback ticket: %s", ticket["title"])
        return filed

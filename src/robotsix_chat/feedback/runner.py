"""Feedback runner — analyses a session and files improvement tickets."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import httpx

from robotsix_chat.common.http import safe_http_request

try:
    from robotsix_llmio.core.tracing import (
        GEN_AI_TOOL_NAME,
        OP_EXECUTE_TOOL,
        get_recording_span,
        get_tracer,
        start_span,
        start_trace,
    )
except ImportError:  # pragma: no cover — tracing extra absent in minimal installs
    start_trace = None  # type: ignore[assignment]
    get_recording_span = None  # type: ignore[assignment]
    start_span = None  # type: ignore[assignment]
    get_tracer = None  # type: ignore[assignment]
    GEN_AI_TOOL_NAME = None  # type: ignore[assignment]
    OP_EXECUTE_TOOL = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from robotsix_chat.config.models import FeedbackSettings
    from robotsix_chat.llm import LlmioChatAgent
    from robotsix_chat.subsessions import SubsessionRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed-repo resolution (dynamic — no static config)
# ---------------------------------------------------------------------------

# In-memory cache: (fetched_at_monotonic, list_of_repo_ids).
_repo_cache: tuple[float, list[str]] | None = None
_REPO_CACHE_TTL: float = 60.0  # seconds — short enough to pick up access changes


async def _resolve_allowed_repos() -> list[str]:
    """Resolve the set of allowed feedback target repos dynamically.

    Queries the deploy server's chat-component roster and the mill board's
    repo registry, then intersects the two on component/repo id.  The result
    is cached briefly (``_REPO_CACHE_TTL``) to avoid hammering deploy on
    every feedback run.

    Falls back to ``["robotsix-chat"]`` when deploy is unreachable and logs
    a warning.
    """
    global _repo_cache
    now = time.monotonic()
    if _repo_cache is not None and (now - _repo_cache[0]) < _REPO_CACHE_TTL:
        return _repo_cache[1]

    deploy_api_key = os.environ.get("DEPLOY_API_KEY", "")

    # 1. Fetch components from deploy.
    deploy_url = "http://central-deploy:8100/chat/components"
    deploy_headers: dict[str, str] = {}
    if deploy_api_key:
        deploy_headers["X-API-Key"] = deploy_api_key

    deploy_result = await safe_http_request(
        "GET", deploy_url, headers=deploy_headers, label="Deploy roster"
    )
    if deploy_result.error:
        logger.warning(
            "Deploy roster unreachable (%s) — falling back to [robotsix-chat] only",
            deploy_result.error,
        )
        _repo_cache = (now, ["robotsix-chat"])
        return ["robotsix-chat"]

    try:
        deploy_entries: list[dict[str, Any]] = json.loads(deploy_result.text or "[]")
    except json.JSONDecodeError:
        logger.warning("Deploy roster response is not valid JSON — falling back")
        _repo_cache = (now, ["robotsix-chat"])
        return ["robotsix-chat"]

    deploy_ids: set[str] = {
        e["id"] for e in deploy_entries if isinstance(e, dict) and "id" in e
    }
    if not deploy_ids:
        logger.warning("Deploy roster is empty — falling back to [robotsix-chat] only")
        _repo_cache = (now, ["robotsix-chat"])
        return ["robotsix-chat"]

    # 2. Fetch repos from mill board.
    mill_url = "http://mill:8077/repos"
    mill_result = await safe_http_request("GET", mill_url, label="Mill repos")
    if mill_result.error:
        logger.warning(
            "Mill repos unreachable (%s) — falling back to [robotsix-chat] only",
            mill_result.error,
        )
        _repo_cache = (now, ["robotsix-chat"])
        return ["robotsix-chat"]

    try:
        mill_repos: list[dict[str, Any]] = json.loads(mill_result.text or "[]")
    except json.JSONDecodeError:
        logger.warning("Mill repos response is not valid JSON — falling back")
        _repo_cache = (now, ["robotsix-chat"])
        return ["robotsix-chat"]

    mill_ids: set[str] = {
        r["id"] for r in mill_repos if isinstance(r, dict) and "id" in r
    }

    # 3. Intersect — only repos that are both in the deploy roster AND
    #    registered on the mill board are valid targets.
    allowed = sorted(deploy_ids & mill_ids)
    if not allowed:
        logger.warning(
            "No repos in deploy/mill intersection (deploy=%s, mill=%s) — "
            "falling back to [robotsix-chat] only",
            sorted(deploy_ids),
            sorted(mill_ids),
        )
        allowed = ["robotsix-chat"]

    _repo_cache = (now, allowed)
    return allowed


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
      "kind": "prompt",
      "target_repo": "robotsix-chat"
    }
  ]
}

``kind`` must be one of: ``prompt``, ``tool``, ``config``, ``code``.
``target_repo`` must be one of the valid target repos listed in the prompt.

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
of what to change.
- Choose ``target_repo`` based on which codebase the improvement \
concerns — if the issue is about the chat system itself, use the chat \
repo; if it is about a downstream component, use that component's repo."""


def _build_feedback_prompt(
    trigger_type: str,
    session_id: str,
    turns: list[tuple[str, str]],
    subsession_summaries: list[dict[str, Any]],
    repo_ids: list[str],
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

    valid_repos = ", ".join(repo_ids)

    return (
        f"Trigger: {trigger_type}\n"
        f"Session ID: {session_id}\n\n"
        f"Conversation transcript:\n{transcript}\n\n"
        f"{subsession_text}\n\n"
        f"Valid target repos: {valid_repos}\n\n"
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
            logger.warning(
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

                # 2. Resolve allowed target repos dynamically.
                allowed_repos = await _resolve_allowed_repos()

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

                # 6. Stamp outcome metadata on the trace root span.
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
    def _parse_tickets(
        analysis_text: str,
        *,
        repo_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Parse the agent's JSON output; return the ``tickets`` list.

        Returns an empty list on any parse failure (logged).
        When *repo_ids* is provided, each ticket's ``target_repo`` is
        validated against it; invalid or missing values are logged and
        the ticket is skipped.
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
        valid_repos = frozenset(repo_ids) if repo_ids else None
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
            target_repo = t.get("target_repo", "")
            if valid_repos is not None:
                if not target_repo:
                    logger.warning(
                        "Skipping ticket %r — missing target_repo; valid repos: %s",
                        title,
                        sorted(valid_repos),
                    )
                    continue
                if target_repo not in valid_repos:
                    logger.warning(
                        "Skipping ticket %r — target_repo %r not in "
                        "configured repo_ids %s",
                        title,
                        target_repo,
                        sorted(valid_repos),
                    )
                    continue
            result.append(
                {
                    "title": title,
                    "description": description,
                    "kind": kind,
                    "target_repo": target_repo,
                }
            )
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

        _tracer = (
            get_tracer("robotsix-chat.feedback") if get_tracer is not None else None
        )
        _span_name = OP_EXECUTE_TOOL if OP_EXECUTE_TOOL is not None else "mill_ingest"

        filed = 0
        for ticket in tickets:
            # Fold runner-level metadata into the body so it survives
            # the mill ingest round-trip even though mill's TicketIngest
            # only carries repo_id / title / body / source_tag.
            body_lines: list[str] = [ticket["description"]]
            body_lines.append("")
            body_lines.append(
                "---"
                f" kind: {ticket['kind']}"
                f" | session: {session_id}"
                f" | trigger: {trigger_type}"
                f" | origin: robotsix-chat"
            )
            payload: dict[str, Any] = {
                "repo_id": ticket["target_repo"],
                "title": ticket["title"],
                "body": "\n".join(body_lines),
                "source_tag": "robotsix-chat-feedback",
            }
            _span_attrs: dict[str, Any] = {
                "http.method": "POST",
                "http.url": ingest_url,
            }
            if GEN_AI_TOOL_NAME is not None:
                _span_attrs[GEN_AI_TOOL_NAME] = "mill_ingest"

            if start_span is not None:
                _span_ctx = start_span(_tracer, _span_name, _span_attrs)
            else:
                import contextlib

                _span_ctx = contextlib.nullcontext()

            try:
                with _span_ctx as _span:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        resp = await client.post(
                            ingest_url, headers=headers, json=payload
                        )
                    if _span is not None:
                        _span.set_attribute("http.status_code", resp.status_code)
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

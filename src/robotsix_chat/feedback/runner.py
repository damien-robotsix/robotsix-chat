"""Feedback runner — analyses a session and files improvement tickets."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
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

try:
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # pragma: no cover — tracing extra absent in minimal installs
    Status = None  # type: ignore[assignment, misc]
    StatusCode = None  # type: ignore[assignment, misc]

if TYPE_CHECKING:
    from robotsix_chat.config.models import FeedbackSettings
    from robotsix_chat.llm import LlmioChatAgent
    from robotsix_chat.subsessions import SubsessionRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed-repo resolution (dynamic — no static config)
# ---------------------------------------------------------------------------

# In-memory cache keyed by "repos": (fetched_at_monotonic, list_of_repo_ids).
_repo_cache: dict[str, tuple[float, list[str]]] = {}
_REPO_CACHE_TTL: float = 60.0  # seconds — short enough to pick up access changes

#: Base delay (seconds) for exponential backoff between idempotent
#: ``/tickets/ingest`` retries.  Attempt *n* waits ``base * 2**n``.
_INGEST_RETRY_BACKOFF_BASE: float = 1.0


#: Used when no ``central_deploy.url`` is configured. The ``central-deploy``
#: hostname only resolves on the deploy stack's *internal* compose network;
#: a component attached solely to ``central-deploy-proxy`` (which is how chat
#: runs) cannot resolve it and gets "Name or service not known". Keeping it as
#: the fallback preserves behaviour for deployments where it does resolve,
#: but any real deployment should set ``central_deploy.url``.
_DEFAULT_DEPLOY_BASE_URL = "http://central-deploy:8100"


async def _resolve_allowed_repos(
    deploy_api_key: str, deploy_base_url: str = ""
) -> list[str]:
    """Resolve the set of allowed feedback target repos dynamically.

    Queries the deploy server's chat-component roster and the mill board's
    repo registry, then intersects the two on component/repo id.  The result
    is cached briefly (``_REPO_CACHE_TTL``) to avoid hammering deploy on
    every feedback run.

    *deploy_base_url* should be ``central_deploy.url`` — the address this
    deployment already knows reaches the deploy server. Empty falls back to
    :data:`_DEFAULT_DEPLOY_BASE_URL`.

    Falls back to ``["robotsix-chat"]`` when deploy is unreachable and logs
    a warning.
    """
    now = time.monotonic()
    entry = _repo_cache.get("repos")
    if entry is not None and (now - entry[0]) < _REPO_CACHE_TTL:
        return entry[1]

    result = await _do_resolve_allowed_repos(deploy_api_key, deploy_base_url)
    _repo_cache["repos"] = (now, result)
    return result


async def _do_resolve_allowed_repos(
    deploy_api_key: str, deploy_base_url: str = ""
) -> list[str]:
    """Resolve allowed repos by querying deploy and mill (no caching)."""
    # 1. Fetch components from deploy.
    base = (deploy_base_url or _DEFAULT_DEPLOY_BASE_URL).rstrip("/")
    deploy_url = f"{base}/chat/components"
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
        return ["robotsix-chat"]

    try:
        deploy_entries: list[dict[str, Any]] = json.loads(deploy_result.text or "[]")
    except json.JSONDecodeError:
        logger.warning("Deploy roster response is not valid JSON — falling back")
        return ["robotsix-chat"]

    deploy_ids: set[str] = {
        e["id"] for e in deploy_entries if isinstance(e, dict) and "id" in e
    }
    if not deploy_ids:
        logger.warning("Deploy roster is empty — falling back to [robotsix-chat] only")
        return ["robotsix-chat"]

    # 2. Fetch repos from mill board.
    mill_url = "http://mill:8077/repos"
    mill_result = await safe_http_request("GET", mill_url, label="Mill repos")
    if mill_result.error:
        logger.warning(
            "Mill repos unreachable (%s) — falling back to [robotsix-chat] only",
            mill_result.error,
        )
        return ["robotsix-chat"]

    try:
        mill_repos: list[dict[str, Any]] = json.loads(mill_result.text or "[]")
    except json.JSONDecodeError:
        logger.warning("Mill repos response is not valid JSON — falling back")
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
repo; if it is about a downstream component, use that component's repo.
- **CI failure visibility:** If the session contains evidence of a CI \
failure after a merge (e.g. CI status mentioned in subsession summaries, \
metadata, or internal notes) but the assistant never proactively informed \
the user about the regression in the main conversation, file a ``prompt`` \
ticket.  The operator's goal is to keep main green — CI regressions that \
are silently buried in internal metadata defeat that goal.  The ticket \
should describe the specific failure, note that the assistant only \
reported it internally, and recommend that the periodic prompt \
instruct the assistant to surface CI failures proactively in the \
main conversation."""


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
        subsession_text = (
            "=== INTERNAL METADATA — NOT part of the conversation, NEVER shown "
            "to the user ===\n"
            "Subsession summaries:\n"
            + "\n".join(parts)
            + "\n=== END INTERNAL METADATA ==="
        )
    else:
        subsession_text = (
            "=== INTERNAL METADATA — NOT part of the conversation ===\n"
            "Subsession summaries: (none)\n"
            "=== END INTERNAL METADATA ==="
        )

    valid_repos = ", ".join(repo_ids)

    return (
        f"Trigger: {trigger_type}\n"
        f"Session ID: {session_id}\n\n"
        f"Conversation transcript:\n{transcript}\n"
        f"=== TRANSCRIPT END ===\n\n"
        f"{subsession_text}\n\n"
        f"Valid target repos: {valid_repos}\n\n"
        "METADATA RULE: The `=== INTERNAL METADATA` block above was assembled "
        "by the feedback system, NOT by the assistant.  It was never printed "
        "to the user.  Do NOT file tickets claiming the assistant emitted raw "
        "subsession identifiers, `kind=… status=…` lines, or metadata headers "
        "unless those patterns appear inside the `Conversation transcript` "
        "section itself.\n\n"
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

    #: Minimum description length (in characters) for a ticket to be
    #: considered actionable.  Shorter descriptions are treated as
    #: boilerplate / low-value noise and filtered out before filing.
    _MIN_DESCRIPTION_LENGTH: int = 10

    def __init__(
        self,
        settings: FeedbackSettings,
        feedback_agent: LlmioChatAgent,
        *,
        subsession_registry: SubsessionRegistry | None = None,
        deploy_base_url: str = "",
        deploy_api_key: str = "",
    ) -> None:
        """*feedback_agent* is a bare ``LlmioChatAgent`` (no tools, no memory).

        *deploy_base_url* and *deploy_api_key* are the canonical
        ``central_deploy.url`` and ``central_deploy.deploy_api_key`` — the
        address and credential this deployment already knows reach the deploy
        server. Left empty, the roster lookup falls back to
        :data:`_DEFAULT_DEPLOY_BASE_URL` with no auth header.
        """
        self._settings = settings
        self._agent = feedback_agent
        self._registry = subsession_registry
        self._deploy_base_url = deploy_base_url
        self._deploy_api_key = deploy_api_key
        self._board_url = settings.board_url.rstrip("/") if settings.board_url else ""
        self._board_token = settings.board_api_token.get_secret_value()
        self._timeout = settings.timeout
        self._max_tickets_per_run = settings.max_tickets_per_run
        self._dedup_window = settings.dedup_window_seconds
        self._ingest_max_retries = settings.ingest_max_retries

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
                filed, failed = await self._file_tickets(
                    tickets, trigger_type=trigger_type, session_id=session_id
                )
                logger.info(
                    "Feedback run complete: trigger=%s session=%s filed=%d/%d"
                    " failed=%d",
                    trigger_type,
                    session_id,
                    filed,
                    len(tickets),
                    failed,
                )

                # 6. Stamp outcome metadata on the trace root span.
                self._stamp_outcome(filed, len(tickets), failed=failed)
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
    def _stamp_outcome(filed: int, total: int, *, failed: int = 0) -> None:
        """Stamp feedback outcome metadata on the current recording span."""
        if get_recording_span is None:
            return
        span = get_recording_span()
        if span is not None:
            span.set_attribute("feedback.filed_tickets", filed)
            span.set_attribute("feedback.failed_tickets", failed)
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
            if len(description) < FeedbackRunner._MIN_DESCRIPTION_LENGTH:
                logger.debug(
                    "Skipping low-value ticket %r — description too short"
                    " (%d < %d chars)",
                    title,
                    len(description),
                    FeedbackRunner._MIN_DESCRIPTION_LENGTH,
                )
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
                        "Skipping ticket %r — target_repo %r not in allowed repos %s",
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

    async def _file_one_ticket(
        self,
        ticket: dict[str, Any],
        *,
        ingest_url: str,
        headers: dict[str, str],
        span_ctx_builder: Callable[[], contextlib.AbstractContextManager[Any]],
        session_id: str,
        trigger_type: str,
        client: httpx.AsyncClient,
    ) -> bool:
        """POST a single ticket; return True when the server responds 2xx."""
        # Pre-flight board-API dedup: query GET /tickets on the board
        # and check whether an open ticket with the same title already
        # exists.  This catches duplicates the in-process in-memory
        # dedup cannot guard against — tickets filed before a server
        # restart, or identical titles filed from different subsessions
        # that race past the session-level debounce.
        if await self._check_existing_open_tickets(
            ticket_title=ticket["title"],
            target_repo=ticket.get("target_repo", ""),
            client=client,
        ):
            return True  # intentionally skipped, not a failure

        # Title-level dedup: skip when the same normalized title was
        # filed within the dedup window (catches cross-session duplicates
        # that the session-level debounce in schedule() cannot guard).
        title_key = ticket["title"].strip().lower()
        now = time.monotonic()
        last = self._last_filed_at.get(title_key)
        if last is not None and (now - last) < self._dedup_window:
            logger.debug(
                "Feedback ticket skipped — title dedup (title=%r,"
                " last=%.1fs ago, window=%.1fs)",
                ticket["title"],
                now - last,
                self._dedup_window,
            )
            return True  # intentionally skipped, not a failure

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

        _span: Any = None
        max_attempts = self._ingest_max_retries + 1
        for attempt in range(max_attempts):
            try:
                with span_ctx_builder() as _span:
                    resp = await client.post(ingest_url, headers=headers, json=payload)
                    if _span is not None:
                        _span.set_attribute("http.status_code", resp.status_code)
                if 200 <= resp.status_code < 300:
                    self._last_filed_at[title_key] = time.monotonic()
                    logger.debug(
                        "Feedback ticket filed: %s (HTTP %d)",
                        ticket["title"],
                        resp.status_code,
                    )
                    # Verify persistence: the ingest endpoint may accept the
                    # payload but never actually create a retrievable ticket.
                    # Immediately GET the returned ID to confirm it exists.
                    await self._verify_ingested_ticket(
                        resp=resp,
                        ticket_title=ticket["title"],
                        client=client,
                    )
                    return True
                else:
                    logger.warning(
                        "Feedback ticket ingest returned %d for %r: %s",
                        resp.status_code,
                        ticket["title"],
                        resp.text[:200],
                    )
                    if _span is not None and StatusCode is not None:
                        _span.set_status(
                            Status(StatusCode.ERROR, f"HTTP {resp.status_code}")
                        )
                        _span.set_attribute("error.type", f"http_{resp.status_code}")
                    return False
            except httpx.TransportError as exc:
                # A read/connect timeout (or other transport-level error)
                # may have still created the ticket server-side before the
                # response was lost.  Idempotent self-heal: before retrying,
                # ask the board whether the ticket now exists so creation is
                # confirmed without filing a duplicate.
                logger.warning(
                    "Feedback ticket ingest timed out for %r (attempt %d/%d): %s",
                    ticket["title"],
                    attempt + 1,
                    max_attempts,
                    exc,
                )
                if _span is not None:
                    self._record_span_exception(_span, exc, ticket["title"])
                if await self._check_existing_open_tickets(
                    ticket_title=ticket["title"],
                    target_repo=ticket.get("target_repo", ""),
                    client=client,
                ):
                    logger.info(
                        "Feedback ticket %r confirmed on board despite ingest "
                        "timeout — treating as filed, no retry",
                        ticket["title"],
                    )
                    self._last_filed_at[title_key] = time.monotonic()
                    return True
                if attempt + 1 < max_attempts:
                    backoff = _INGEST_RETRY_BACKOFF_BASE * (2**attempt)
                    logger.debug(
                        "Retrying feedback ticket ingest for %r in %.1fs",
                        ticket["title"],
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                logger.error(
                    "Failed to file feedback ticket after %d attempt(s): %s",
                    max_attempts,
                    ticket["title"],
                )
                return False
            except Exception as exc:
                logger.exception("Failed to file feedback ticket: %s", ticket["title"])
                if _span is not None:
                    self._record_span_exception(_span, exc, ticket["title"])
                return False
        return False

    @staticmethod
    def _record_span_exception(span: Any, exc: Exception, ticket_title: str) -> None:
        """Record *exc* on *span*, never letting instrumentation raise."""
        try:
            if StatusCode is not None:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
        except Exception:
            # Never let span instrumentation break the filing loop.
            logger.debug(
                "Span instrumentation failed for ticket %r: %s",
                ticket_title,
                exc,
                exc_info=True,
            )

    async def _verify_ingested_ticket(
        self,
        *,
        resp: httpx.Response,
        ticket_title: str,
        client: httpx.AsyncClient,
    ) -> None:
        """Immediately verify that an ingested ticket is retrievable.

        The board's ``/tickets/ingest`` endpoint may accept a payload
        but never actually persist the ticket (phantom ticket).  Parse
        the response for a returned ticket ID and GET it to confirm.
        A verification failure is logged as a warning — it does not
        change the filed/failed count since the server already
        acknowledged the ingest.
        """
        # Parse the response body for a ticket ID.
        try:
            body = resp.json()
        except json.JSONDecodeError, ValueError:
            body = {}
        if not isinstance(body, dict):
            return

        ticket_id: str | None = body.get("id") or body.get("ticket_id")
        if not ticket_id or not isinstance(ticket_id, str):
            return

        verify_url = f"{self._board_url}/tickets/{ticket_id}"
        verify_headers: dict[str, str] = {"Accept": "application/json"}
        if self._board_token:
            verify_headers["Authorization"] = f"Bearer {self._board_token}"

        try:
            verify_resp = await client.get(verify_url, headers=verify_headers)
            if verify_resp.status_code == 404:
                logger.warning(
                    "Feedback ticket %r filed but not retrievable "
                    "(HTTP 404 for %s) — phantom ticket may have "
                    "been created",
                    ticket_title,
                    ticket_id,
                )
            elif verify_resp.status_code >= 400:
                logger.warning(
                    "Feedback ticket %r filed but verification returned HTTP %d for %s",
                    ticket_title,
                    verify_resp.status_code,
                    ticket_id,
                )
            else:
                logger.debug(
                    "Feedback ticket %r verified retrievable at %s",
                    ticket_title,
                    ticket_id,
                )
        except Exception:
            logger.warning(
                "Feedback ticket %r filed but verification request failed for %s",
                ticket_title,
                ticket_id,
                exc_info=True,
            )

    async def _check_existing_open_tickets(
        self,
        *,
        ticket_title: str,
        target_repo: str,
        client: httpx.AsyncClient,
    ) -> bool:
        """Query the board API for an existing open ticket with the same title.

        Returns ``True`` when an open (non-terminal) ticket with a
        matching normalized title already exists for *target_repo* on
        the board — the new ticket should be skipped.

        The board API call is best-effort: any error (timeout, unreachable,
        non-JSON response) is logged and the method returns ``False`` so
        the filing proceeds rather than being blocked by a transient API
        issue.
        """
        if not self._board_url:
            return False
        # Normalise the candidate title the same way the in-process
        # dedup does so the comparison is consistent.
        norm_title = ticket_title.strip().lower()
        if not norm_title:
            return False

        list_url = f"{self._board_url}/tickets"
        req_headers: dict[str, str] = {"Accept": "application/json"}
        if self._board_token:
            req_headers["Authorization"] = f"Bearer {self._board_token}"

        try:
            resp = await client.get(list_url, headers=req_headers)
            if resp.status_code >= 400:
                logger.debug(
                    "Board ticket-list returned HTTP %d — "
                    "skipping pre-flight dedup check",
                    resp.status_code,
                )
                return False
            tickets_data = resp.json()
        except Exception:
            logger.debug(
                "Board ticket-list request failed — skipping pre-flight dedup check",
                exc_info=True,
            )
            return False

        # Normalise the response shape.
        if isinstance(tickets_data, list):
            ticket_objects: list[dict[str, Any]] = tickets_data
        elif isinstance(tickets_data, dict):
            ticket_objects = tickets_data.get("tickets", [])
            if not isinstance(ticket_objects, list):
                ticket_objects = []
        else:
            ticket_objects = []

        # Terminal ticket states — tickets in these states are "closed"
        # and should not block a new filing.
        _terminal_states = frozenset({"closed", "done"})

        for existing in ticket_objects:
            if not isinstance(existing, dict):
                continue
            existing_title = (
                existing.get("title", "")
                if isinstance(existing.get("title"), str)
                else ""
            )
            if existing_title.strip().lower() != norm_title:
                continue
            # Match repo_id when both sides carry one; skip the check
            # when the board ticket has no repo_id (older tickets).
            existing_repo = existing.get("repo_id")
            if (
                target_repo
                and isinstance(existing_repo, str)
                and existing_repo
                and existing_repo != target_repo
            ):
                continue
            existing_state = existing.get("state", "")
            if (
                isinstance(existing_state, str)
                and existing_state.lower() in _terminal_states
            ):
                continue
            logger.info(
                "Feedback ticket skipped — existing open ticket %r "
                "already on board (title=%r, repo=%r, state=%r)",
                existing.get("ticket_id", "?"),
                existing_title,
                existing_repo,
                existing_state,
            )
            return True

        return False

    def _apply_cap(
        self,
        tickets: list[dict[str, Any]],
        *,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """Trim *tickets* to ``max_tickets_per_run``.

        A feedback run fires at every compaction and session-end boundary
        and was previously unbounded. Unlike a mill periodic pass — whose
        dropped findings resurface on the next run — a chat session ends,
        so anything dropped here is gone. Each dropped title is therefore
        logged at WARNING rather than discarded silently.
        """
        cap = self._max_tickets_per_run
        if cap < 0 or len(tickets) <= cap:
            return tickets

        kept, dropped = tickets[:cap], tickets[cap:]
        logger.warning(
            "Feedback run for session %s produced %d ticket(s); filing %d "
            "(feedback.max_tickets_per_run=%d). Dropped: %s",
            session_id,
            len(tickets),
            len(kept),
            cap,
            "; ".join(repr(t.get("title", "?")) for t in dropped),
        )
        return kept

    async def _file_tickets(
        self,
        tickets: list[dict[str, Any]],
        *,
        trigger_type: str,
        session_id: str,
    ) -> tuple[int, int]:
        """POST each ticket to ``/tickets/ingest``; return (filed, failed)."""
        if not self._board_url:
            return (0, 0)

        tickets = self._apply_cap(tickets, session_id=session_id)
        if not tickets:
            return (0, 0)

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

        _span_attrs: dict[str, Any] = {
            "http.method": "POST",
            "http.url": ingest_url,
        }
        if GEN_AI_TOOL_NAME is not None:
            _span_attrs[GEN_AI_TOOL_NAME] = "mill_ingest"

        if start_span is not None:
            span_ctx_builder = lambda: start_span(_tracer, _span_name, _span_attrs)  # noqa: E731
        else:
            span_ctx_builder = contextlib.nullcontext

        filed = 0
        failed = 0
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for ticket in tickets:
                success = await self._file_one_ticket(
                    ticket,
                    ingest_url=ingest_url,
                    headers=headers,
                    span_ctx_builder=span_ctx_builder,
                    session_id=session_id,
                    trigger_type=trigger_type,
                    client=client,
                )
                if success:
                    filed += 1
                else:
                    failed += 1
        return (filed, failed)

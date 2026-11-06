"""Ticket parsing, filing and finding routing for the feedback runner."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from robotsix_chat.feedback.allowed_repos import (
    _CHAT_SELF_REPO,
    _is_admission_policy_block,
    _subject_is_chat_config,
)
from robotsix_chat.feedback.finding_state import FindingStateMixin
from robotsix_chat.feedback.prompts import (
    _build_chat_config_panel_prompt,
    _build_escalation_prompt,
    _build_investigation_panel_prompt,
)

try:
    from robotsix_llmio.core.tracing import (
        GEN_AI_TOOL_NAME,
        OP_EXECUTE_TOOL,
        get_tracer,
        start_span,
    )
except ImportError:  # pragma: no cover — tracing extra absent in minimal installs
    start_span = None  # type: ignore[assignment]
    get_tracer = None  # type: ignore[assignment]
    GEN_AI_TOOL_NAME = None  # type: ignore[assignment]
    OP_EXECUTE_TOOL = None  # type: ignore[assignment]

try:
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # pragma: no cover — tracing extra absent in minimal installs
    Status = None  # type: ignore[assignment, misc]
    StatusCode = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

#: Base delay (seconds) for exponential backoff between idempotent
#: ``/tickets/ingest`` retries.  Attempt *n* waits ``base * 2**n``.
_INGEST_RETRY_BACKOFF_BASE: float = 1.0


class TicketFilingMixin(FindingStateMixin):
    """Parse analysis output, file tickets and route policy-blocked findings.

    Mixed into :class:`~robotsix_chat.feedback.runner.FeedbackRunner`,
    which sets the instance attributes declared below.
    """

    _spawner: Callable[..., str | None] | None
    _board_url: str
    _board_token: str
    _timeout: float
    _max_tickets_per_run: int
    _dedup_window: float
    _ingest_max_retries: int
    _last_filed_at: dict[str, float]

    #: Minimum description length (in characters) for a ticket to be
    #: considered actionable.  Shorter descriptions are treated as
    #: boilerplate / low-value noise and filtered out before filing.
    _MIN_DESCRIPTION_LENGTH: int = 10

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
            if len(description) < TicketFilingMixin._MIN_DESCRIPTION_LENGTH:
                logger.debug(
                    "Skipping low-value ticket %r — description too short"
                    " (%d < %d chars)",
                    title,
                    len(description),
                    TicketFilingMixin._MIN_DESCRIPTION_LENGTH,
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
    ) -> str:
        """POST a single ticket; return the outcome for the run tally.

        Returns one of:

        * ``"filed"`` — the server accepted the ticket (2xx), or the
          filing was intentionally skipped as a duplicate.
        * ``"routed"`` — the board rejected the ingest with its admission
          policy (HTTP 400, investigation-tickets no longer accepted); the
          finding was handed to a chat subsession agent instead.  A policy
          outcome, *not* a failure.
        * ``"dropped"`` — a policy-blocked finding whose owner session was
          deleted; parked as a knowledge note instead of spawning an orphan
          investigation subsession.  A policy outcome, *not* a failure.
        * ``"failed"`` — a genuine failure (non-policy error, exhausted
          retries, or an unexpected exception).
        """
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
            return "filed"  # intentionally skipped, not a failure

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
            return "filed"  # intentionally skipped, not a failure

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
            "repo_id": ticket.get("target_repo", ""),
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
                    return "filed"
                elif _is_admission_policy_block(resp):
                    # Board admission policy (mill #3093): investigation
                    # tickets are no longer accepted.  This is a policy
                    # outcome, NOT a transient failure — route the finding
                    # to a chat subsession agent (the mechanism the mill
                    # 400 prescribes) instead of counting it as failed.
                    logger.info(
                        "Feedback finding %r blocked by board admission "
                        "policy (HTTP 400) — routing to chat subsession "
                        "agent instead of filing: %s",
                        ticket["title"],
                        resp.text[:200],
                    )
                    if _span is not None:
                        _span.set_attribute("feedback.admission_policy_block", True)
                    return self._route_finding_to_subsession(
                        ticket,
                        session_id=session_id,
                        trigger_type=trigger_type,
                    )
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
                    return "failed"
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
                    return "filed"
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
                return "failed"
            except Exception as exc:
                logger.exception("Failed to file feedback ticket: %s", ticket["title"])
                if _span is not None:
                    self._record_span_exception(_span, exc, ticket["title"])
                return "failed"
        return "failed"

    def _route_finding_to_subsession(
        self,
        ticket: dict[str, Any],
        *,
        session_id: str,
        trigger_type: str,
    ) -> str:
        """Hand a policy-blocked finding to a chat subsession agent.

        Returns the run-tally outcome: ``"routed"`` when the finding was
        handed to an investigation subsession (or a missing spawner / spawn
        error meant it was best-effort dropped in place), or ``"dropped"``
        when the owner session has been deleted from the conversation store
        — in that case an investigation would deliver its summary to a
        parent conversation that no longer exists, so instead the finding is
        parked as a knowledge note for a later periodic review to pick up.

        A finding about the chat system's own periodic/session configuration
        is always targeted at :data:`_CHAT_SELF_REPO` — never a component
        repo.  A finding whose target cannot be resolved with confidence is
        escalated to the operator as a ``user_chat`` panel instead of
        guessing.  Every routed finding is recorded as resolved (keyed on
        its title) so the same finding is not re-investigated on the next
        session-end run.
        """
        # Guard: never route an orphan investigation.  When the owner session
        # was deleted (e.g. DELETE /sessions/{id}) the spawned subsession
        # would run a full investigation and then deliver its summary to a
        # conversation that no longer exists — the result reaches nobody.
        if self._owner_session_deleted(session_id):
            logger.info(
                "Feedback finding not routed - owner session %s deleted",
                session_id,
            )
            self._record_orphan_finding(
                ticket, session_id=session_id, trigger_type=trigger_type
            )
            return "dropped"
        # Durability: a finding already handed to an investigation (or
        # escalated) is never re-investigated verbatim on a later session
        # end.  The board-admission ticket path that used to make this
        # durable is closed, so routing records its own resolution.
        if self._finding_resolved(ticket):
            logger.info(
                "Feedback finding already resolved — not re-investigated: %r",
                ticket["title"],
            )
            return "routed"
        if self._spawner is None:
            logger.warning(
                "Feedback finding %r blocked by board admission policy but no "
                "subsession spawner is configured — finding dropped",
                ticket["title"],
            )
            return "routed"
        if _subject_is_chat_config(ticket):
            # Chat's periodic presets / prompts live in the deployed config
            # volume, not in the repo: an investigation subsession can only
            # edit the shipped ``config/config.json`` template, which prod
            # never reads. 82 such PRs piled up on robotsix-chat between
            # 2026-09-05 and 2026-10-12 (3 to 9 a day, none mergeable into
            # anything live) and mill filed a tracking ticket for each. A
            # preset/prompt change is a policy decision — surface it to the
            # operator as a panel instead of opening a PR.
            prompt = _build_chat_config_panel_prompt(
                ticket, session_id=session_id, trigger_type=trigger_type
            )
            title = f"[feedback] chat config: {ticket['title']}"
            try:
                sub_id = self._spawner(
                    owner_session_id=session_id,
                    title=title,
                    prompt=prompt,
                    kind="user_chat",
                    dedup_key=self._subject_key(ticket),
                )
            except Exception:
                logger.exception(
                    "Failed to escalate chat-config finding %r to operator panel",
                    ticket["title"],
                )
                return "routed"
            self._record_resolved_finding(
                ticket, session_id=session_id, trigger_type=trigger_type
            )
            self._last_filed_at[ticket["title"].strip().lower()] = time.monotonic()
            logger.info(
                "Feedback chat-config finding escalated to operator panel: "
                "title=%r subsession=%s",
                ticket["title"],
                sub_id,
            )
            return "routed"
        target_repo = self._resolve_target_repo(ticket)
        if target_repo is None:
            # Cannot confidently resolve where the finding belongs — escalate
            # to the operator as a user-facing panel instead of guessing (a
            # guess is what produced the mis-targeted component PRs).
            prompt = _build_escalation_prompt(
                ticket, session_id=session_id, trigger_type=trigger_type
            )
            title = f"[feedback] assign target: {ticket['title']}"
            try:
                sub_id = self._spawner(
                    owner_session_id=session_id,
                    title=title,
                    prompt=prompt,
                    kind="user_chat",
                    dedup_key=self._subject_key(ticket),
                )
            except Exception:
                logger.exception(
                    "Failed to escalate feedback finding %r to operator panel",
                    ticket["title"],
                )
                return "routed"
            self._record_resolved_finding(
                ticket, session_id=session_id, trigger_type=trigger_type
            )
            self._last_filed_at[ticket["title"].strip().lower()] = time.monotonic()
            logger.info(
                "Feedback finding escalated to operator panel: title=%r subsession=%s",
                ticket["title"],
                sub_id,
            )
            return "routed"
        # Apply the resolved target (corrects mis-targeted chat-config
        # findings) so the investigation prompt never points at a component
        # repo for chat's own config.
        ticket["target_repo"] = target_repo
        prompt = _build_investigation_panel_prompt(
            ticket, session_id=session_id, trigger_type=trigger_type
        )
        try:
            # The investigation no longer opens a PR autonomously: it becomes
            # an operator approval panel (``user_chat``) that is read-only
            # until the operator approves.  Dedup on the normalised subject
            # key so the same finding across runs updates the existing panel
            # instead of opening a new one.
            sub_id = self._spawner(
                owner_session_id=session_id,
                title=ticket["title"],
                prompt=prompt,
                kind="user_chat",
                dedup_key=self._subject_key(ticket),
            )
        except Exception:
            logger.exception(
                "Failed to spawn investigation panel for feedback finding %r",
                ticket["title"],
            )
            return "routed"
        self._record_resolved_finding(
            ticket, session_id=session_id, trigger_type=trigger_type
        )
        self._last_filed_at[ticket["title"].strip().lower()] = time.monotonic()
        logger.info(
            "Feedback finding routed to chat subsession agent: title=%r "
            "target_repo=%r subsession=%s",
            ticket["title"],
            ticket.get("target_repo", ""),
            sub_id,
        )
        return "routed"

    def _resolve_target_repo(self, ticket: dict[str, Any]) -> str | None:
        """Return the effective target repo for an investigation, or ``None``.

        A finding about the chat system's own periodic presets, prompts, or
        session behaviour is owned by :data:`_CHAT_SELF_REPO` — it must
        never be routed to a component repo, whatever the analysis LLM
        guessed.  Otherwise the LLM-supplied ``target_repo`` is used when
        present.  Returns ``None`` when the target cannot be resolved with
        confidence — the caller should escalate to the operator rather than
        guess.
        """
        if _subject_is_chat_config(ticket):
            return _CHAT_SELF_REPO
        target = (ticket.get("target_repo") or "").strip()
        return target or None

    @staticmethod
    def _subject_key(ticket: dict[str, Any]) -> str:
        """Return a normalised dedup key for a finding's subject.

        Combines the normalised title with the resolved target repo so the
        same finding seen across successive ``session_end`` runs maps to ONE
        operator panel: the dedup guard in ``spawn_subsession`` returns the
        existing panel's id instead of opening a second one.  The same title
        about a different repo stays a distinct subject.
        """
        title = (ticket.get("title") or "").strip().lower()
        repo = (ticket.get("target_repo") or "").strip().lower()
        return f"feedback:{repo}:{title}" if repo else f"feedback:{title}"

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
        from robotsix_chat.ticket_poll.mill_states import MERGED_TERMINAL_STATES

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
                and existing_state.lower() in MERGED_TERMINAL_STATES
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
    ) -> tuple[int, int, int, int]:
        """POST each ticket; return ``(filed, failed, routed, dropped)``.

        ``routed`` counts findings the board rejected with its admission
        policy that were handed to a chat subsession agent instead — a
        policy outcome, not a failure.  ``dropped`` counts policy-blocked
        findings whose owner session was deleted (no parent conversation to
        deliver an investigation to): they are parked as a knowledge note
        instead of spawning an orphan subsession.
        """
        if not self._board_url:
            return (0, 0, 0, 0)

        tickets = self._apply_cap(tickets, session_id=session_id)
        if not tickets:
            return (0, 0, 0, 0)

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
        routed = 0
        dropped = 0
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for ticket in tickets:
                outcome = await self._file_one_ticket(
                    ticket,
                    ingest_url=ingest_url,
                    headers=headers,
                    span_ctx_builder=span_ctx_builder,
                    session_id=session_id,
                    trigger_type=trigger_type,
                    client=client,
                )
                if outcome == "filed":
                    filed += 1
                elif outcome == "routed":
                    routed += 1
                elif outcome == "dropped":
                    dropped += 1
                else:
                    failed += 1
        return (filed, failed, routed, dropped)

"""Board API client — talks to the mill board API for ticket-state verification.

Provides a standalone ``BoardClient`` that queries ticket state, fetches
ticket data, and resumes blocked tickets via the board's REST API.  It is
independent of the GitHub App authentication used by ``DirectRepoClient``
— board API calls use ``board_api_token`` bearer auth.

Used by both ``repo/direct`` (as a fallback path for ticket-state
verification) and ``ticket_poll`` (which already owns board-API concern).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

import httpx
from robotsix_http import ExternalHTTPError, RetryClient, RetryConfig

from robotsix_chat.common.http import safe_http_request

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

logger = logging.getLogger(__name__)

# Retry configuration for board API requests — transient network blips
# should not surface as "board API unreachable" to callers.
_BOARD_RETRY_CONFIG = RetryConfig(
    max_retries=2,
    backoff_base=1.0,
    backoff_cap=10.0,
    jitter_factor=0.5,
)


class BoardClient:
    """Mill board API client for ticket-state verification and lifecycle ops.

    Talks to the board's ``/tickets/{id}`` and ``/tickets/{id}/resume-blocked``
    endpoints using ``board_api_token`` bearer auth.  All errors are logged
    as warnings and surfaced as ``None`` / ``False`` returns — callers are
    responsible for formatting user-facing messages.
    """

    def __init__(self, settings: DirectRepoSettings) -> None:
        """Store settings; board API calls use ``board_api_token`` auth."""
        self._s = settings
        self._board_url = settings.board_api_base_url.rstrip("/")

    async def _fetch_ticket_field(
        self, ticket_id: str, label_suffix: str, field: str | None = None
    ) -> Any:
        """Fetch a ticket from the board API and return *field* or the full dict.

        When *field* is provided (e.g. ``"state"``), returns ``data.get(field)``;
        when ``None``, returns the full parsed JSON dict.  Returns ``None`` on
        any error (logged as a warning).

        Uses ``RetryClient`` so transient network blips don't surface as
        "board API unreachable" to callers (same retry policy as ticket_poll).
        """
        data, _reason = await self._fetch_ticket(ticket_id, label_suffix)
        if data is None:
            return None
        if field is not None:
            return data.get(field)
        return data

    async def _fetch_ticket(
        self, ticket_id: str, label_suffix: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Fetch a ticket, returning ``(data, None)`` or ``(None, reason)``.

        The *reason* string distinguishes the failure classes callers need
        to react to differently — most importantly a 404 (bad/paraphrased
        ticket ID) from an API outage. Every failure is also logged.
        """
        url = f"{self._board_url}/tickets/{ticket_id}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._s.board_api_token.get_secret_value():
            headers["Authorization"] = (
                f"Bearer {self._s.board_api_token.get_secret_value()}"
            )
        try:
            async with httpx.AsyncClient(timeout=self._s.timeout) as client:
                retry_client = RetryClient(client, config=_BOARD_RETRY_CONFIG)
                response = await retry_client.get(url, headers=headers)
                response.raise_for_status()
                text = response.text
        except (httpx.HTTPStatusError, ExternalHTTPError) as exc:
            # RetryClient re-raises HTTP errors that survived its retry
            # policy as ExternalHTTPError; direct raise_for_status yields
            # httpx.HTTPStatusError. Both carry the status code.
            code = (
                exc.status_code
                if isinstance(exc, ExternalHTTPError)
                else exc.response.status_code
            )
            if code == 404:
                logger.warning(
                    "Board API: ticket %r not found (404) when fetching %s. "
                    "This ID may have been derived from narrative text "
                    "rather than from a board API response — verify it "
                    "against GET /tickets on the board.",
                    ticket_id,
                    label_suffix,
                )
                return None, (
                    f"ticket not found (404): no ticket {ticket_id!r} on the "
                    "board — the ID may be paraphrased or stale; verify it "
                    "against the board ticket list"
                )
            logger.warning(
                "Board API request for ticket %s %s failed: HTTP %s",
                ticket_id,
                label_suffix,
                code,
            )
            return None, f"Board API error: HTTP {code}"
        except httpx.TimeoutException:
            logger.warning(
                "Board API request for ticket %s %s timed out after %ss",
                ticket_id,
                label_suffix,
                self._s.timeout,
            )
            return None, f"Board API timeout after {self._s.timeout}s"
        except Exception as exc:
            logger.warning(
                "Board API request for ticket %s %s failed: %s",
                ticket_id,
                label_suffix,
                exc,
            )
            return None, f"Board API unreachable: {exc}"
        try:
            data = json.loads(text)
        except json.JSONDecodeError, TypeError:
            logger.warning(
                "Non-JSON response for ticket %s: %s",
                ticket_id,
                text[:200],
            )
            return None, "Board API returned a non-JSON response"
        return data, None

    async def resolve_ticket_ids(
        self, candidate_ids: list[str]
    ) -> dict[str, str | None]:
        """Resolve candidate ticket IDs against the live board.

        Fetches ``GET /tickets`` and for each candidate ID tries, in order:

        1. **Exact match** — the candidate ID appears verbatim in the board.
        2. **Hash-suffix match** — the last 4 hex chars of the candidate
           uniquely match one ticket's hash suffix.
        3. **Slug-substring match** — the non-timestamp, non-hash portion
           of the candidate appears as a substring of exactly one ticket's
           full ID.

        Returns a dict mapping each candidate ID to its resolved full
        ticket ID, or ``None`` when the candidate could not be resolved.
        """
        import re

        if not candidate_ids:
            return {}

        if not self._board_url:
            return {cid: None for cid in candidate_ids}

        url = f"{self._board_url}/tickets"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._s.board_api_token.get_secret_value():
            headers["Authorization"] = (
                f"Bearer {self._s.board_api_token.get_secret_value()}"
            )

        try:
            async with httpx.AsyncClient(timeout=self._s.timeout) as client:
                retry_client = RetryClient(client, config=_BOARD_RETRY_CONFIG)
                response = await retry_client.get(url, headers=headers)
                response.raise_for_status()
                tickets_data = response.json()
        except Exception as exc:
            logger.warning(
                "BoardClient: failed to fetch ticket list for ID resolution: %s",
                exc,
            )
            return {cid: None for cid in candidate_ids}

        # Extract ticket IDs from the response.
        if isinstance(tickets_data, list):
            ticket_objects = tickets_data
        elif isinstance(tickets_data, dict):
            ticket_objects = tickets_data.get("tickets", [])
            if not isinstance(ticket_objects, list):
                logger.warning(
                    "BoardClient: unexpected GET /tickets response "
                    "format — expected list or {tickets: [...]}"
                )
                return {cid: None for cid in candidate_ids}
        else:
            logger.warning(
                "BoardClient: unexpected GET /tickets response type %s",
                type(tickets_data).__name__,
            )
            return {cid: None for cid in candidate_ids}

        all_ids: list[str] = [
            t["ticket_id"]
            for t in ticket_objects
            if isinstance(t, dict) and isinstance(t.get("ticket_id"), str)
        ]

        # Build a reverse index: hash-suffix → list of matching full IDs.
        suffix_index: dict[str, list[str]] = {}
        for tid in all_ids:
            m = re.search(r"-([0-9a-f]{4})$", tid)
            if m:
                suffix_index.setdefault(m.group(1), []).append(tid)

        result: dict[str, str | None] = {}
        for cid in candidate_ids:
            if not isinstance(cid, str) or not cid.strip():
                result[cid] = None
                continue

            # 1. Exact match.
            if cid in all_ids:
                result[cid] = cid
                continue

            # 2. Hash-suffix match.
            suffix_match = re.search(r"([0-9a-f]{4})$", cid)
            if suffix_match:
                suffix = suffix_match.group(1)
                matches = suffix_index.get(suffix, [])
                if len(matches) == 1:
                    resolved = matches[0]
                    logger.info(
                        "BoardClient: resolved %r → %r (hash suffix %r)",
                        cid,
                        resolved,
                        suffix,
                    )
                    result[cid] = resolved
                    continue
                if len(matches) > 1:
                    logger.warning(
                        "BoardClient: ambiguous hash suffix %r for %r — matches %s",
                        suffix,
                        cid,
                        matches,
                    )

            # 3. Slug-substring match.
            slug = re.sub(r"^\d{8}T\d{6}Z-", "", cid)
            slug = re.sub(r"-?[0-9a-f]{4}$", "", slug)
            if slug and len(slug) >= 4:
                slug_matches = [tid for tid in all_ids if slug.lower() in tid.lower()]
                if len(slug_matches) == 1:
                    resolved = slug_matches[0]
                    logger.info(
                        "BoardClient: resolved %r → %r (slug match %r)",
                        cid,
                        resolved,
                        slug,
                    )
                    result[cid] = resolved
                    continue
                if len(slug_matches) > 1:
                    logger.warning(
                        "BoardClient: ambiguous slug %r for %r — matches %s",
                        slug,
                        cid,
                        slug_matches,
                    )

            logger.warning(
                "BoardClient: could not resolve %r against "
                "the board (%d tickets listed)",
                cid,
                len(all_ids),
            )
            result[cid] = None

        return result

    async def get_ticket_state(self, ticket_id: str) -> str | None:
        """Return the ticket's state (e.g. ``"BLOCKED"``), or ``None`` on failure.

        Calls the board API directly — the same endpoint the browser UI uses.
        """
        return cast(
            "str | None",
            await self._fetch_ticket_field(ticket_id, "state", field="state"),
        )

    async def resume_blocked_ticket(
        self, ticket_id: str, justification: str
    ) -> tuple[bool, str | None]:
        """Resume a blocked ticket via the board API.

        Sends ``POST /tickets/{ticket_id}/resume-blocked`` with a JSON
        body containing *justification*.

        Returns ``(True, None)`` on success (HTTP 2xx).  On failure returns
        ``(False, reason)`` where *reason* is the board API's actionable
        diagnostic (status code + response-body excerpt, or the transport
        error) so callers can surface it instead of a generic "could not
        reset" message.  Every failure is also logged as a warning.
        """
        url = f"{self._board_url}/tickets/{ticket_id}/resume-blocked"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._s.board_api_token.get_secret_value():
            headers["Authorization"] = (
                f"Bearer {self._s.board_api_token.get_secret_value()}"
            )
        result = await safe_http_request(
            "POST",
            url,
            headers=headers,
            json_body={"justification": justification},
            timeout=self._s.timeout,
            label=f"Board API (resume-blocked {ticket_id})",
        )
        if result.error:
            logger.warning(
                "Failed to resume blocked ticket %s: %s",
                ticket_id,
                result.error,
            )
            return False, result.error
        return True, None

    async def get_ticket_data(self, ticket_id: str) -> dict[str, Any] | None:
        """Return the full ticket JSON from the board API, or None on failure.

        Calls ``GET /tickets/{ticket_id}`` on the board API and returns the
        parsed JSON body.  The response includes ``state``, ``events`` (state
        transitions), and other ticket metadata.
        """
        return cast(
            "dict[str, Any] | None",
            await self._fetch_ticket_field(ticket_id, "data"),
        )

    async def get_ticket_data_detailed(
        self, ticket_id: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Like :meth:`get_ticket_data`, but return ``(data, failure_reason)``.

        ``(data, None)`` on success. On failure ``(None, reason)``, where
        *reason* distinguishes a 404 (bad or paraphrased ticket ID) from an
        API outage — callers surface it verbatim instead of a generic
        "Board API request failed".
        """
        return await self._fetch_ticket(ticket_id, "data")

    async def find_ticket_by_pr_url(self, pr_url: str) -> dict[str, Any] | None:
        """Find a ticket by its linked PR URL.

        Calls ``GET /tickets`` (with a ``pr_url`` query parameter when the
        board API supports it) and returns the first ticket whose
        ``pr_url`` field matches *pr_url*, or ``None`` when no ticket is
        found or the board API is unreachable.

        This is a direct lookup — the caller gets the matching ticket in
        one API round-trip instead of enumerating all tickets and filtering
        client-side.
        """
        if not self._board_url:
            return None

        url = f"{self._board_url}/tickets"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._s.board_api_token.get_secret_value():
            headers["Authorization"] = (
                f"Bearer {self._s.board_api_token.get_secret_value()}"
            )

        # Try server-side filtering first — ?pr_url=<encoded>.
        params: dict[str, str] = {"pr_url": pr_url}
        try:
            async with httpx.AsyncClient(timeout=self._s.timeout) as client:
                retry_client = RetryClient(client, config=_BOARD_RETRY_CONFIG)
                response = await retry_client.get(url, headers=headers, params=params)
                response.raise_for_status()
                tickets_data = response.json()
        except Exception as exc:
            logger.warning(
                "BoardClient.find_ticket_by_pr_url: "
                "server-side filter failed: %s; falling back to full list",
                exc,
            )
            tickets_data = None

        # Normalise the response shape.
        ticket_objects: list[dict[str, Any]] = []
        if isinstance(tickets_data, list):
            ticket_objects = tickets_data
        elif isinstance(tickets_data, dict):
            ticket_objects = tickets_data.get("tickets", [])
            if not isinstance(ticket_objects, list):
                ticket_objects = []

        # If server-side filtering returned a match, use it.
        for t in ticket_objects:
            if isinstance(t, dict) and t.get("pr_url") == pr_url:
                return t

        # Server-side filtering didn't return a match (either the board
        # doesn't support ?pr_url= or the filter returned empty).  Fall
        # back to fetching the full list and filtering client-side.
        if ticket_objects:
            # We already have the full list from the first call — the
            # server ignored ?pr_url= and returned everything.  We already
            # scanned it above; no match.
            return None

        # The first call failed entirely — retry without ?pr_url=.
        try:
            async with httpx.AsyncClient(timeout=self._s.timeout) as client:
                retry_client = RetryClient(client, config=_BOARD_RETRY_CONFIG)
                response = await retry_client.get(url, headers=headers)
                response.raise_for_status()
                tickets_data = response.json()
        except Exception as exc:
            logger.warning(
                "BoardClient.find_ticket_by_pr_url: full-list fallback failed: %s",
                exc,
            )
            return None

        if isinstance(tickets_data, list):
            ticket_objects = tickets_data
        elif isinstance(tickets_data, dict):
            ticket_objects = tickets_data.get("tickets", [])
            if not isinstance(ticket_objects, list):
                return None

        for t in ticket_objects:
            if isinstance(t, dict) and t.get("pr_url") == pr_url:
                return t

        return None

    async def create_ticket(
        self,
        title: str,
        description: str = "",
        kind: str = "task",
        source: str = "agent",
        repo_id: str | None = None,
    ) -> str | None:
        """Create a new ticket via the board API.

        Sends ``POST /tickets`` with the ticket fields.  Returns the
        created ticket's ``id`` on success (HTTP 201), ``None`` on any
        error (logged as a warning).
        """
        url = f"{self._board_url}/tickets"
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._s.board_api_token.get_secret_value():
            headers["Authorization"] = (
                f"Bearer {self._s.board_api_token.get_secret_value()}"
            )
        body: dict[str, Any] = {
            "title": title,
            "description": description,
            "kind": kind,
            "source": source,
        }
        if repo_id is not None:
            body["repo_id"] = repo_id
        result = await safe_http_request(
            "POST",
            url,
            headers=headers,
            json_body=body,
            timeout=self._s.timeout,
            label=f"Board API (create-ticket {title[:60]})",
        )
        if result.error:
            logger.warning(
                "Failed to create ticket '%s': %s",
                title[:80],
                result.error,
            )
            return None
        if result.status_code and result.status_code >= 400:
            logger.warning(
                "Board API returned %d for create ticket '%s'",
                result.status_code,
                title[:80],
            )
            return None
        try:
            data = json.loads(result.text or "{}")
        except json.JSONDecodeError:
            logger.warning(
                "Non-JSON response for create ticket '%s': %s",
                title[:80],
                (result.text or "")[:200],
            )
            return None
        ticket_id = data.get("id")
        if isinstance(ticket_id, str) and ticket_id:
            logger.info(
                "BoardClient: created ticket %s (%s) — verifying persistence",
                ticket_id,
                title[:80],
            )
            # Immediately verify the ticket is retrievable — the board API
            # may return an ID that never actually persisted (phantom ticket).
            verify_data, verify_reason = await self._fetch_ticket(
                ticket_id, "create-verify"
            )
            if verify_data is None:
                logger.warning(
                    "BoardClient: created ticket %s but verification failed: "
                    "%s — the ticket may not have persisted; treating as "
                    "failure",
                    ticket_id,
                    verify_reason,
                )
                return None
            logger.info(
                "BoardClient: verified ticket %s is retrievable",
                ticket_id,
            )
            return ticket_id
        logger.warning(
            "Board API create ticket response missing 'id': %s",
            (result.text or "")[:200],
        )
        return None

    async def count_implement_cycles(self, ticket_id: str) -> int | None:
        """Return the number of implement cycles for *ticket_id*, or None on failure.

        Inspects the ticket's ``events`` array (from the board API) and counts
        events whose ``type`` or ``action`` field contains the substring
        ``"implement"`` (case-insensitive).  Falls back to counting state
        transitions through ``"implement_complete"`` if no events array is
        present.
        """
        from robotsix_chat.repo.direct.client import _count_cycles_from_data

        data = await self.get_ticket_data(ticket_id)
        if data is None:
            return None

        cycles = _count_cycles_from_data(data)
        if cycles == 0:
            logger.info(
                "Ticket %s has no events/history/cycle_count — "
                "assuming 0 implement cycles.",
                ticket_id,
            )
        return cycles

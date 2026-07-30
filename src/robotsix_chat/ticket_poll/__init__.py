"""Ticket poll tools for querying the mill board API.

Routes through ``component_request`` (roster-based connectivity) when
available, falling back to the direct ``board_api_base_url`` otherwise.

Provides ``ticket_poll(ticket_id)`` and ``ticket_poll_batch(ticket_ids)`` —
dedicated tools that return ticket state and full data for single-ticket
polling and bulk read-only triage respectively.

Exposes :func:`build_ticket_poll_tools` — a factory returning the LLM tools.
Returns no tools when neither ``component_request`` nor
``board_api_base_url`` are available.  Also exposes
:func:`load_ticket_poll_skill` which returns the component skill markdown
for injection into the agent instruction.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from robotsix_http import RetryClient, RetryConfig

if TYPE_CHECKING:
    from robotsix_chat.config import Settings

__all__ = ["build_ticket_poll_tools", "load_ticket_poll_skill"]

logger = logging.getLogger(__name__)

# Retry configuration for ticket poll requests — transient network blips
# should not surface as "board API unreachable" to the agent.
_TICKET_POLL_RETRY_CONFIG = RetryConfig(
    max_retries=2,
    backoff_base=1.0,
    backoff_cap=10.0,
    jitter_factor=0.5,
)


def _parse_json_body(body: str) -> tuple[dict[str, Any] | None, str]:
    """Parse *body* as JSON, returning ``(data, error)``.

    *error* is empty on success, or a diagnostic message on failure.
    Callers format the error into their own return shape.
    """
    try:
        return json.loads(body), ""
    except (json.JSONDecodeError, TypeError):
        return None, "Non-JSON response from board API"


def load_ticket_poll_skill() -> str:
    """Return the ticket-poll component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.
    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_ticket_poll_tools(
    settings: Settings,
    *,
    component_request: Callable[..., Any] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``ticket_poll`` and ``ticket_poll_batch`` tools.

    When *component_request* is available the tools route through the
    roster-based component connectivity (same path as ticket-state
    verification).  Otherwise they fall back to the direct
    ``board_api_base_url``.

    When *component_request* is provided, the tool uses the roster-based
    path as its primary connectivity method (resolving ``"mill"`` via the
    central-deploy roster or component fallbacks); on failure it falls
    back to the direct ``board_api_base_url`` path.  This ensures the
    chat container reaches the mill on its actual service hostname rather
    than a potentially misconfigured direct URL.

    Args:
        settings: Full application settings.
        component_request: The roster-based request callable, or ``None``
            when the component roster is unavailable.

    Returns:
        A two-element list containing the ``ticket_poll`` and
        ``ticket_poll_batch`` async callables, or ``[]`` when neither
        *component_request* nor ``board_api_base_url`` are available.

    """
    board_url = settings.direct_repo.board_api_base_url.strip()
    if not component_request and not board_url:
        return []

    board_url = board_url.rstrip("/") if board_url else ""
    board_token = settings.direct_repo.board_api_token.get_secret_value()
    timeout = settings.direct_repo.timeout

    async def _fetch_ticket_via_component(
        ticket_id: str,
    ) -> tuple[int, str | None, str]:
        """Fetch a ticket via *component_request*; return ``(status, body, error)``.

        Returns ``(status, body, "")`` on success, ``(status, None, error)``
        on failure.  *body* is the raw response body string.
        """
        assert component_request is not None  # type narrow for mypy
        resp = await component_request("mill", "GET", f"/tickets/{ticket_id}")
        if resp.startswith("Error:"):
            return (
                0,
                None,
                f"Board API request via component_request failed: {resp}",
            )
        try:
            newline = resp.index("\n")
            status_line = resp[:newline]
            body_str = resp[newline + 1 :]
        except ValueError:
            return (
                0,
                None,
                "Board API request via component_request failed: "
                "unexpected response format",
            )
        if not status_line.startswith("HTTP "):
            return (
                0,
                None,
                f"Board API request via component_request failed: {status_line}",
            )
        try:
            status_code = int(status_line.split()[1])
        except (IndexError, ValueError):
            return (
                0,
                None,
                f"Board API request via component_request failed: "
                f"unparsable status {status_line!r}",
            )
        return status_code, body_str, ""

    async def ticket_poll(ticket_id: str) -> str:
        """Poll the mill board for a ticket's current state.

        Routes through the component roster when available; falls back to
        the direct board API otherwise.

        Args:
            ticket_id: The ticket identifier (e.g. "20250101T120000Z-my-ticket-a1b2").

        Returns:
            A JSON string with ``ticket_id``, ``state`` (or ``null`` when
            the field is absent), and ``error`` (empty on success).

        """
        if component_request is not None:
            status, body, error = await _fetch_ticket_via_component(ticket_id)
            if error:
                return json.dumps(
                    {"ticket_id": ticket_id, "state": None, "error": error},
                    ensure_ascii=False,
                )
            if status >= 400:
                return json.dumps(
                    {
                        "ticket_id": ticket_id,
                        "state": None,
                        "error": f"Board API returned HTTP {status}",
                    },
                    ensure_ascii=False,
                )
            if body is None:
                return json.dumps(
                    {
                        "ticket_id": ticket_id,
                        "state": None,
                        "error": "Empty response body from board API",
                    },
                    ensure_ascii=False,
                )
            data, parse_error = _parse_json_body(body)
            if parse_error:
                return json.dumps(
                    {"ticket_id": ticket_id, "state": None, "error": parse_error},
                    ensure_ascii=False,
                )
            assert data is not None  # guarded by parse_error check above
            state = data.get("state")
            return json.dumps(
                {"ticket_id": ticket_id, "state": state, "error": ""},
                ensure_ascii=False,
            )
        return await _ticket_poll_direct(ticket_id)

    async def _ticket_poll_direct(ticket_id: str) -> str:
        """Poll the mill board for a ticket's current state.

        Directly queries the board API (bypasses the component roster).
        Use this when ``component_request`` is unavailable or as an
        independent verification of ticket state.
        """
        url = f"{board_url}/tickets/{ticket_id}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if board_token:
            headers["Authorization"] = f"Bearer {board_token}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                retry_client = RetryClient(client, config=_TICKET_POLL_RETRY_CONFIG)
                response = await retry_client.get(url, headers=headers)
                response.raise_for_status()
                data, parse_error = _parse_json_body(response.text)
                if parse_error:
                    return json.dumps(
                        {"ticket_id": ticket_id, "state": None, "error": parse_error},
                        ensure_ascii=False,
                    )
                assert data is not None  # guarded by parse_error check above
                state = data.get("state")
                return json.dumps(
                    {
                        "ticket_id": ticket_id,
                        "state": state,
                        "error": "",
                    },
                    ensure_ascii=False,
                )
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException:
            return json.dumps(
                {
                    "ticket_id": ticket_id,
                    "state": None,
                    "error": f"Board API request timed out after {timeout}s",
                },
                ensure_ascii=False,
            )
        except httpx.HTTPStatusError as exc:
            return json.dumps(
                {
                    "ticket_id": ticket_id,
                    "state": None,
                    "error": f"Board API returned HTTP {exc.response.status_code}",
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.warning("ticket_poll direct path failed for %s: %s", ticket_id, exc)
            return json.dumps(
                {
                    "ticket_id": ticket_id,
                    "state": None,
                    "error": f"Board API request failed: {exc}",
                },
                ensure_ascii=False,
            )

    async def ticket_poll_batch(ticket_ids: list[str]) -> str:
        """Fetch full ticket data for multiple tickets concurrently.

        Routes through the component roster when available; falls back to
        the direct board API otherwise.  Queries ``GET /tickets/{id}`` for
        every ticket in parallel (up to 10 concurrent requests).  Returns
        the complete API response for each ticket — including ``state``,
        ``events`` / history, comments, and cycle metadata — so you can
        classify blocked tickets by failure signature (e.g.
        "implement-loop/3of3", "git-failure", "capability-gap") without
        N sequential round-trips.

        This tool uses the direct board API path and does NOT go through
        the roster.  Use ``ticket_poll`` for roster-first connectivity.

        Args:
            ticket_ids: List of ticket identifiers to fetch.

        Returns:
            A JSON string with a ``tickets`` array.  Each element has:

            - ``ticket_id`` — the supplied identifier
            - ``state`` — the ticket's current state string (or ``null``)
            - ``data`` — the full JSON response from the board API
            - ``error`` — empty on success, or a diagnostic message on failure

        """
        sem = asyncio.Semaphore(10)

        if component_request is not None:

            async def _fetch_one_via_component(ticket_id: str) -> dict[str, Any]:
                async with sem:
                    status, body, error = await _fetch_ticket_via_component(ticket_id)
                    if error:
                        return {
                            "ticket_id": ticket_id,
                            "state": None,
                            "data": None,
                            "error": error,
                        }
                    if status >= 400:
                        return {
                            "ticket_id": ticket_id,
                            "state": None,
                            "data": None,
                            "error": f"Board API returned HTTP {status}",
                        }
                    if body is None:
                        return {
                            "ticket_id": ticket_id,
                            "state": None,
                            "data": None,
                            "error": "Empty response body from board API",
                        }
                    data, parse_error = _parse_json_body(body)
                    if parse_error:
                        return {
                            "ticket_id": ticket_id,
                            "state": None,
                            "data": None,
                            "error": parse_error,
                        }
                    assert data is not None  # guarded by parse_error check above
                    return {
                        "ticket_id": ticket_id,
                        "state": data.get("state"),
                        "data": data,
                        "error": "",
                    }

            gathered = await asyncio.gather(
                *(_fetch_one_via_component(tid) for tid in ticket_ids)
            )
            return json.dumps({"tickets": list(gathered)}, ensure_ascii=False)

        async def _fetch_one_direct(ticket_id: str) -> dict[str, Any]:
            async with sem:
                url = f"{board_url}/tickets/{ticket_id}"
                headers: dict[str, str] = {"Accept": "application/json"}
                if board_token:
                    headers["Authorization"] = f"Bearer {board_token}"

                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        retry_client = RetryClient(
                            client, config=_TICKET_POLL_RETRY_CONFIG
                        )
                        response = await retry_client.get(url, headers=headers)
                        response.raise_for_status()
                        data, parse_error = _parse_json_body(response.text)
                        if parse_error:
                            return {
                                "ticket_id": ticket_id,
                                "state": None,
                                "data": None,
                                "error": parse_error,
                            }
                        assert data is not None  # guarded by parse_error check above
                        return {
                            "ticket_id": ticket_id,
                            "state": data.get("state"),
                            "data": data,
                            "error": "",
                        }
                except httpx.HTTPStatusError as exc:
                    return {
                        "ticket_id": ticket_id,
                        "state": None,
                        "data": None,
                        "error": f"Board API returned HTTP {exc.response.status_code}",
                    }
                except httpx.TimeoutException:
                    return {
                        "ticket_id": ticket_id,
                        "state": None,
                        "data": None,
                        "error": f"Board API request timed out after {timeout}s",
                    }
                except Exception as exc:
                    logger.warning(
                        "ticket_poll_batch failed for %s: %s", ticket_id, exc
                    )
                    return {
                        "ticket_id": ticket_id,
                        "state": None,
                        "data": None,
                        "error": f"Board API request failed: {exc}",
                    }

        gathered = await asyncio.gather(*(_fetch_one_direct(tid) for tid in ticket_ids))
        return json.dumps({"tickets": list(gathered)}, ensure_ascii=False)

    return [ticket_poll, ticket_poll_batch]

"""Error handlers — consistent JSON error responses.

All handlers emit the same envelope shape so API clients never have to
branch on which key appeared.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
from asgi_correlation_id import correlation_id
from robotsix_llmio.claude_sdk import is_claude_sdk_usage_exhausted
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

#: Stable, machine-readable codes for the mid-stream SSE error frame. Clients
#: branch on these (retry vs. abort); the wording below may change freely, the
#: codes may not.
STREAM_ERROR_SERVER = "server_error"
STREAM_ERROR_TIMEOUT = "timeout"
STREAM_ERROR_RATE_LIMIT = "rate_limit_exceeded"
STREAM_ERROR_AUTH = "authentication_error"
STREAM_ERROR_INVALID_REQUEST = "invalid_request_error"
STREAM_ERROR_BUDGET_EXHAUSTED = "budget_exhausted"

#: Curated, client-safe wording per code. Deliberately free of exception
#: detail: ``str(exc)`` routinely embeds filesystem paths, upstream URLs and
#: provider error bodies, and the SSE error frame is broadcast to *every*
#: client watching the session.
_STREAM_ERROR_MESSAGES: dict[str, str] = {
    STREAM_ERROR_SERVER: (
        "The assistant hit an internal error and couldn't complete the response."
    ),
    STREAM_ERROR_TIMEOUT: ("The assistant took too long to respond. Please try again."),
    STREAM_ERROR_RATE_LIMIT: (
        "The assistant is rate limited right now. Please retry in a moment."
    ),
    STREAM_ERROR_AUTH: (
        "The assistant could not authenticate with its model provider."
    ),
    STREAM_ERROR_INVALID_REQUEST: ("The assistant could not process that request."),
    STREAM_ERROR_BUDGET_EXHAUSTED: (
        "The assistant's model budget is exhausted. Resume with a new message "
        "to continue, or switch to a different model level."
    ),
}

#: The Claude CLI reports a quota reset as plain text embedded in the
#: exhaustion error, e.g. ``resets 1am (UTC)`` / ``resets 11:10am (UTC)``.
#: We extract ONLY the structured clock time (digits + am/pm) — never the
#: surrounding free text — so the reset hint is safe to surface even though
#: raw ``str(exc)`` is not.
_RESET_HINT_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(a|p)m\s*\(utc\)",
    re.IGNORECASE,
)


def _iter_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc`` and its bounded cause/context chain.

    ``robotsix_llmio.is_claude_sdk_usage_exhausted`` only follows
    ``__cause__`` links; an exception raised while *handling* the exhaustion
    error (the tier-fallback walk failing too) carries the root only as
    ``__context__``. Walk both so the SSE error frame classifies the turn as
    ``budget_exhausted`` — with the reset-time message — rather than the
    generic internal error.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(seen) < 32:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _exception_chain_text(exc: BaseException) -> str:
    """Join the messages of ``exc`` and its bounded cause/context chain.

    ``ClaudeSDKUsageExhaustedError`` may reach the chat layer wrapped after a
    tier-fallback walk, so the reset hint can live on a cause rather than the
    outermost exception — mirror ``is_claude_sdk_usage_exhausted``'s chain
    walk (bounded to avoid a pathological cycle).
    """
    return " ".join(str(cur) for cur in _iter_chain(exc))


def claude_usage_reset_at(
    exc: BaseException, *, now: datetime | None = None
) -> datetime | None:
    """Parse the UTC quota-reset time from a Claude usage-exhaustion error.

    Returns the next UTC ``datetime`` at which the reported clock time occurs
    (today if still ahead of *now*, else tomorrow), or ``None`` when the error
    carries no parseable ``resets <time> (UTC)`` hint. *now* is injectable for
    deterministic tests; it defaults to the current UTC time.
    """
    match = _RESET_HINT_RE.search(_exception_chain_text(exc))
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    is_pm = match.group(3).lower() == "p"
    if is_pm and hour != 12:
        hour += 12
    elif not is_pm and hour == 12:
        hour = 0
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= reference:
        candidate += timedelta(days=1)
    return candidate


def _format_wait(reset_at: datetime, now: datetime) -> str:
    """Render the approximate wait until *reset_at* as ``in 56 min`` / ``in 2 h``."""
    minutes = max(0, int((reset_at - now).total_seconds() // 60))
    if minutes < 60:
        return f"in {minutes} min"
    hours, rem = divmod(minutes, 60)
    return f"in {hours} h {rem} min" if rem else f"in {hours} h"


def budget_exhausted_message(
    exc: BaseException,
    *,
    paid_fallback_enabled: bool = False,
    now: datetime | None = None,
) -> str:
    """Build the user-facing message for a Claude quota-exhaustion error.

    Names the UTC reset time and approximate wait when the error carries a
    reset hint, and — when paid fallback is disabled — appends a hint that it
    can be enabled. *now* is injectable for deterministic tests.
    """
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    reset_at = claude_usage_reset_at(exc, now=reference)
    if reset_at is not None:
        when = reset_at.strftime("%H:%M")
        wait = _format_wait(reset_at, reference)
        lead = f"Claude quota exhausted — resets at {when} UTC ({wait}). Retry then"
    else:
        lead = "Claude quota exhausted. Retry later"
    if paid_fallback_enabled:
        return f"{lead}."
    return (
        f"{lead} — or just resend your message: the paid backup model "
        "serves turns automatically while Claude is exhausted."
    )


def _is_token_limit_error(exc: BaseException) -> bool:
    """Return True when *exc* is a token-limit overflow.

    pydantic-ai raises ``UnexpectedModelBehavior`` when the combined prompt
    (system + history + tools + user turn) exceeds the model's context
    window before any response tokens are generated.  The message contains
    ``"token limit"`` and ``"exceeded"`` — match the chain so wrapped
    errors are caught too.
    """
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    for cur in _iter_chain(exc):
        if isinstance(cur, UnexpectedModelBehavior) and (
            "token limit" in str(cur).lower()
        ):
            return True
    return False


def stream_error_code(exc: BaseException) -> str:
    """Map ``exc`` to a stable, client-safe error code.

    Categories are derived from transport-level facts (timeout, HTTP status on
    an attached response) plus two semantic classifiers — a Claude SDK tier
    reporting exhausted usage credits maps to ``budget_exhausted``, and a
    pydantic-ai ``UnexpectedModelBehavior`` whose message indicates a token
    limit overflow maps to ``invalid_request_error`` (the prompt was too large
    for the model).  Anything unrecognised degrades to ``server_error``
    rather than guessing.
    """
    if any(is_claude_sdk_usage_exhausted(cur) for cur in _iter_chain(exc)):
        return STREAM_ERROR_BUDGET_EXHAUSTED
    if _is_token_limit_error(exc):
        return STREAM_ERROR_INVALID_REQUEST
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return STREAM_ERROR_TIMEOUT
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status == 429:
            return STREAM_ERROR_RATE_LIMIT
        if status in (401, 403):
            return STREAM_ERROR_AUTH
        if 400 <= status < 500:
            return STREAM_ERROR_INVALID_REQUEST
    return STREAM_ERROR_SERVER


def curated_stream_error(
    exc: BaseException,
    *,
    fallback_id: str = "",
    paid_fallback_enabled: bool = False,
) -> dict[str, str]:
    """Build the client-facing payload for a mid-stream failure.

    Returns a stable ``code``, curated ``message``, and the request's
    ``correlation_id`` so a user-reported error can be grepped straight to the
    server-side ``logger.exception`` line. Falls back to ``fallback_id`` (the
    turn id) when no correlation id is in context — the coalescer can outlive
    the request that spawned it.

    Quota exhaustion is a known, time-bounded condition, so instead of the
    generic static wording it gets an actionable message naming the UTC reset
    time and approximate wait (and, when *paid_fallback_enabled* is false, a
    hint that paid fallback can be turned on).
    """
    code = stream_error_code(exc)
    if code == STREAM_ERROR_BUDGET_EXHAUSTED:
        message = budget_exhausted_message(
            exc, paid_fallback_enabled=paid_fallback_enabled
        )
    elif code == STREAM_ERROR_INVALID_REQUEST and _is_token_limit_error(exc):
        message = (
            "The conversation history is too long for the backup model. "
            "Start a new session or shorten the conversation to continue."
        )
    else:
        message = _STREAM_ERROR_MESSAGES[code]
    return {
        "code": code,
        "message": message,
        "correlation_id": correlation_id.get() or fallback_id,
    }


def _error_body(detail: str) -> dict[str, str]:
    """Build the standardised error envelope."""
    return {
        "error": detail or "internal server error",
        "correlation_id": correlation_id.get() or "",
    }


async def http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for ``HTTPException`` instead of plain text."""
    if isinstance(exc, HTTPException):
        return JSONResponse(_error_body(str(exc.detail)), status_code=exc.status_code)
    return JSONResponse(_error_body(str(exc)), status_code=500)


async def not_found_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for unmatched routes instead of plain text.

    When an endpoint explicitly raises ``HTTPException(404)`` with a
    custom detail (e.g.  "unknown subsession 'xyz'"), forward that
    detail.  Starlette dispatches by status code before exception
    class, so this handler sees both genuine unmatched-route 404s and
    explicit raises.
    """
    if isinstance(exc, HTTPException) and str(exc.detail) != "Not Found":
        return JSONResponse(_error_body(str(exc.detail)), status_code=404)
    return JSONResponse(_error_body("not found"), status_code=404)


async def server_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
    """Return JSON for unhandled server errors.

    Logs the full traceback so operators can diagnose the root cause.
    """
    logger.exception("Unhandled server error")
    return JSONResponse(_error_body("internal server error"), status_code=500)


async def unhandled_exception_handler(
    _request: Request, _exc: Exception
) -> JSONResponse:
    """Catch-all handler for any exception not caught by more specific handlers.

    Logs the full traceback and returns a generic 500 so no raw
    traceback leaks to the client.
    """
    logger.exception("Unhandled exception")
    return JSONResponse(_error_body("internal server error"), status_code=500)

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
STREAM_ERROR_NO_IMAGE_SUPPORT = "no_image_support"

#: Curated, client-safe wording per code. Deliberately free of exception
#: detail: ``str(exc)`` routinely embeds filesystem paths, upstream URLs and
#: provider error bodies, and the SSE error frame is broadcast to *every*
#: client watching the session.
_STREAM_ERROR_MESSAGES: dict[str, str] = {
    STREAM_ERROR_SERVER: (
        "The assistant hit an internal error and couldn't complete the "
        "response. This has been logged for review. You can retry the "
        "message, or start a new conversation if this keeps happening."
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
    STREAM_ERROR_NO_IMAGE_SUPPORT: (
        "The current provider/model can't read images. Resend your message "
        "as plain text instead, or retry after the provider failover window "
        "ends."
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
    now: datetime | None = None,
) -> str:
    """Build the user-facing message for a Claude quota-exhaustion error.

    Seen only when BOTH provider slots failed the turn: llmio's provider
    failover retries an exhausted Claude turn on the OpenRouter fallback
    slot automatically, so reaching this message means the fallback could
    not serve it either (missing/invalid OpenRouter key, no credits, or an
    OpenRouter outage). Names the UTC reset time and approximate wait when
    the error carries a reset hint. *now* is injectable for deterministic
    tests.
    """
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    reset_at = claude_usage_reset_at(exc, now=reference)
    if reset_at is not None:
        when = reset_at.strftime("%H:%M")
        wait = _format_wait(reset_at, reference)
        lead = f"Claude quota exhausted — resets at {when} UTC ({wait})"
    else:
        lead = "Claude quota exhausted"
    return (
        f"{lead}, and the automatic OpenRouter failover could not serve "
        "this turn either. Check the OpenRouter key and credits "
        "(`llmio.api_key`), or retry after the reset."
    )


def no_image_support_message() -> str:
    """Build the user-facing message for a no-image-support turn.

    The resolved provider/model cannot read images, so the turn 404'd
    upstream (OpenRouter: *"No endpoints found that support image input"*).
    The user should resend as text or wait out the failover window; when a
    window is open, name ``failover_until`` from the live failover status so
    the retry time is concrete.
    """
    from robotsix_llmio.core.failover import get_failover_status

    failover = get_failover_status()
    until = failover.failover_until
    if failover.failover_active and until is not None:
        retry = (
            "after the provider failover window ends "
            f"(back to the default provider at {until:%H:%M} UTC)"
        )
    else:
        retry = "in a moment"
    return (
        "The current provider/model can't read images. Resend your message "
        f"as plain text instead, or retry {retry}."
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


#: Provider-side rejections of a turn that contains an image.  The observed
#: production shape is an OpenRouter 404 whose body/message reads "No
#: endpoints found that support image input" (2026-09-01, live incident); the
#: remaining entries cover the equivalent no-image-support shapes other
#: providers/models use.  Matched on the whole chain — the 404 body can live
#: on a wrapped ``HTTPStatusError`` rather than the outermost exception.
_NO_IMAGE_SUPPORT_PHRASES: tuple[str, ...] = (
    "no endpoints found that support image input",
    "does not support image input",
    "image input is not supported",
    "does not support images",
)


def _response_text(cur: BaseException) -> str:
    """Return the textual body of an exception's attached response, if any."""
    response = getattr(cur, "response", None)
    if response is None:
        return ""
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return ""


def _is_no_image_support_error(exc: BaseException) -> bool:
    """Return True when any exception in the chain is a no-image-support reply.

    OpenRouter rejects image-bearing turns on text-only models with an HTTP
    404 whose body names the missing capability; the rejection can surface
    in ``str(exc)`` or on an attached ``response`` at any depth of the
    cause/context chain.  The phrase is specific enough that matching it
    anywhere in the chain cannot be confused with the generic 4xx handling.
    """
    for cur in _iter_chain(exc):
        text = f"{cur!s} {_response_text(cur)}".lower()
        if any(phrase in text for phrase in _NO_IMAGE_SUPPORT_PHRASES):
            return True
    return False


def stream_error_code(exc: BaseException) -> str:
    """Map ``exc`` to a stable, client-safe error code.

    Categories are derived from transport-level facts (timeout, HTTP status on
    an attached response) plus three semantic classifiers — a Claude SDK tier
    reporting exhausted usage credits maps to ``budget_exhausted``, a
    pydantic-ai ``UnexpectedModelBehavior`` whose message indicates a token
    limit overflow maps to ``invalid_request_error`` (the prompt was too large
    for the model), and a provider rejection naming missing image support
    maps to ``no_image_support``.  Anything unrecognised degrades to
    ``server_error`` rather than guessing.
    """
    if any(is_claude_sdk_usage_exhausted(cur) for cur in _iter_chain(exc)):
        return STREAM_ERROR_BUDGET_EXHAUSTED
    if _is_token_limit_error(exc):
        return STREAM_ERROR_INVALID_REQUEST
    if _is_no_image_support_error(exc):
        return STREAM_ERROR_NO_IMAGE_SUPPORT
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
) -> dict[str, str]:
    """Build the client-facing payload for a mid-stream failure.

    Returns a stable ``code``, curated ``message``, and the request's
    ``correlation_id`` so a user-reported error can be grepped straight to the
    server-side ``logger.exception`` line. Falls back to ``fallback_id`` (the
    turn id) when no correlation id is in context — the coalescer can outlive
    the request that spawned it.

    Quota exhaustion is a known, time-bounded condition, so instead of the
    generic static wording it gets an actionable message naming the UTC
    reset time and approximate wait (reaching it at all means the automatic
    provider failover also failed — see :func:`budget_exhausted_message`).
    """
    code = stream_error_code(exc)
    if code == STREAM_ERROR_BUDGET_EXHAUSTED:
        message = budget_exhausted_message(exc)
    elif code == STREAM_ERROR_NO_IMAGE_SUPPORT:
        message = no_image_support_message()
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

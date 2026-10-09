"""Worker error detection, formatting, and reply-sentinel helpers."""

from __future__ import annotations

# The Claude Agent SDK's wording when it collapses a self-contradictory
# ``is_error=True`` / ``errors=[]`` / ``subtype="success"`` frame into a
# bare message — a known transient bug, not a real tool failure.
_DEGENERATE_SUCCESS_SIGNATURE = "returned an error result: success"

# The Claude CLI's wording when a tier's usage credits are exhausted.
_USAGE_EXHAUSTED_SIGNATURE = "out of usage credits"

# HTTP status used by model providers (OpenRouter, etc.) when the
# requested model is not available at the configured price ceiling.
# The model exists but cannot be reached through the current routing
# — falling back to a different tier usually resolves it.
_MODEL_TIER_NOT_FOUND_STATUS = 404


def _is_model_tier_not_found(exc: BaseException) -> bool:
    """Return ``True`` when *exc* indicates the requested model tier is not available.

    Currently matches HTTP 404 on the exception or anywhere in its cause
    chain — the common signature when an OpenRouter model cannot be routed
    at the configured price ceiling.
    """
    from robotsix_http.retry import _status

    return _status(exc) == _MODEL_TIER_NOT_FOUND_STATUS


def _format_worker_error(exc: BaseException) -> str:
    """Translate known Claude SDK error patterns into clear human-readable messages.

    When *exc* is a :class:`claude_agent_sdk.ProcessError` (the CLI
    subprocess exited non-zero), the message includes the exit code and
    stderr output so the operator can diagnose the tool failure without
    digging through logs.

    For unrecognised exceptions the exception type name is always included
    so the message is actionable even when the SDK wording is opaque.
    """
    msg = str(exc)
    exc_type_name = type(exc).__name__

    # Degenerate success frame — a known transient Claude SDK bug that
    # can persist across retries.  Not a real tool failure.
    if _DEGENERATE_SUCCESS_SIGNATURE in msg.lower():
        return (
            "The Claude agent encountered a transient internal SDK error "
            "(degenerate success frame — the SDK reported an error result "
            "whose subtype is 'success', a self-contradictory frame that "
            "could not be cleared by retry). This is a known Claude SDK "
            "bug and does not indicate a real tool failure. "
            f"Original SDK message: {msg}"
        )

    # Usage-exhaustion — the tier has no credits left.
    if _USAGE_EXHAUSTED_SIGNATURE in msg.lower():
        return (
            "The Claude agent's usage credits for this tier are exhausted. "
            "Switch to a different model level, or wait for credits to "
            "reset. " + msg
        )

    # Model not routable — e.g. OpenRouter 404 when no provider serves the
    # model at the configured price ceiling. llmio's provider failover
    # already retried the turn on the other provider slot before this
    # surfaced.
    if _is_model_tier_not_found(exc):
        return (
            "The requested model is not available "
            "(HTTP 404 — it could not be routed at the configured "
            "price ceiling), and the automatic provider failover could "
            "not serve the turn either. " + msg
        )

    # ProcessError from claude_agent_sdk carries exit_code and stderr —
    # surface those so the operator can diagnose without log-diving.
    exit_code = getattr(exc, "exit_code", None)
    if exit_code is not None:
        stderr = getattr(exc, "stderr", None)
        parts = [f"Claude CLI process exited with code {exit_code}"]
        if stderr:
            stderr_text = str(stderr).strip()
            if stderr_text:
                parts.append(f"stderr: {_truncate(stderr_text, 500)}")
        parts.append(msg)
        return "\n".join(parts)

    # For any other exception, include the type name so the message is
    # never just an opaque SDK string — the operator can distinguish a
    # TimeoutError from a RuntimeError at a glance.
    if exc_type_name not in msg:
        return f"[{exc_type_name}] {msg}"
    return msg


def _truncate(text: str, max_len: int) -> str:
    """Truncate *text* to *max_len* chars, appending ``"..."`` when cut."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# Reply sentinel a periodic subsession uses to report "nothing changed".
_NO_CHANGE_SENTINEL = "NO_CHANGE"

# Reply sentinel a periodic subsession uses to report that the monitored
# ticket is queued (waiting for implementation / in a non-terminal
# pipeline stage) — the monitor should enter event-driven wait instead of
# burning no-change quota.
_QUEUED_SENTINEL = "QUEUED"


# Prompt fragment prepended when a user_chat / task subsession is retried
# after a failure.  The agent sees the original error so it can diagnose
# and self-correct (e.g. re-build context that was lost).
_RETRY_PROMPT_TEMPLATE = (
    "[System note: this subsession is being retried after a failure "
    "(attempt {attempt}/{max_retries}). The error was:\n\n{error}\n\n"
    "The subsession has been re-launched from its original instructions. "
    "If the error was caused by lost context (e.g. after a server restart) "
    "you may need to re-fetch any external state you were relying on. "
    "Your original instructions follow below.]\n\n"
)


# Consecutive stale-worker resume attempts before the subsession is closed.


# Phrases that, when they appear at the start of a periodic reply,
# indicate the agent found nothing to report.  Kept broad enough to
# catch common LLM paraphrasing of "nothing changed" without being so
# broad that it swallows real status updates.
_NO_CHANGE_PHRASES: tuple[str, ...] = (
    "NO CHANGE",
    "NO CHANGES",
    "NOTHING CHANGED",
    "NOTHING HAS CHANGED",
    "NO UPDATES",
    "UNCHANGED",
    "NO NEW",
    "EVERYTHING IS THE SAME",
    "ALL QUIET",
    "STATUS UNCHANGED",
    "NO SIGNIFICANT CHANGE",
    "NO MEANINGFUL CHANGE",
)

# Phrases that, when they appear at the start of a periodic reply,
# indicate the agent found the ticket is queued (waiting for
# implementation) — the monitor should switch to event-driven wait.
_QUEUED_PHRASES: tuple[str, ...] = (
    "QUEUED",
    "QUEUED FOR IMPLEMENTATION",
    "WAITING FOR IMPLEMENTATION",
    "IN QUEUE",
    "IMPLEMENTATION QUEUED",
    "AWAITING IMPLEMENTATION",
    "PENDING IMPLEMENTATION",
)


def _format_duration(seconds: float) -> str:
    """Return a human-readable duration string for *seconds*."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes} min"
    hours = int(seconds / 3600)
    minutes = int((seconds % 3600) / 60)
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def _is_no_change(reply: str) -> bool:
    """Whether *reply* is the periodic no-change sentinel or a common paraphrase.

    The LLM sometimes returns a paraphrase instead of the exact sentinel.
    """
    cleaned = reply.strip().upper()
    if cleaned.startswith(_NO_CHANGE_SENTINEL):
        return True
    return cleaned.startswith(_NO_CHANGE_PHRASES)


def _is_queued(reply: str) -> bool:
    """Whether *reply* is the queued sentinel or a common paraphrase.

    The agent uses this when the monitored ticket is waiting for
    implementation — the worker should switch to event-driven wait
    instead of counting this as a no-change run.
    """
    cleaned = reply.strip().upper()
    if cleaned.startswith(_QUEUED_SENTINEL):
        return True
    return cleaned.startswith(_QUEUED_PHRASES)


def _is_duplicate_reply(reply: str, previous: str | None) -> bool:
    """Whether *reply* is identical to the previous run's reply.

    Strips and case-folds before comparing — suppresses repeated verbatim output.
    """
    if previous is None:
        return False
    return reply.strip().casefold() == previous.strip().casefold()


def _ordinal_suffix(n: int) -> str:
    """Return the ordinal suffix for *n*.

    E.g. ``"st"``, ``"nd"``, ``"rd"``, ``"th"``.
    """
    if 11 <= (n % 100) <= 13:
        return "th"
    last = n % 10
    if last == 1:
        return "st"
    if last == 2:
        return "nd"
    if last == 3:
        return "rd"
    return "th"
